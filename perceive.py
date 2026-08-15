# perceive.py — 视频感知模块
# 集成 VLM(Qwen3.7-Plus) + ASR(自建) + 场景检测(FFmpeg)
# 让 agent 能"看懂"视频内容 + "听懂"音频 + "检查"渲染结果
import os, sys, json, base64, subprocess, tempfile, time

try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

# 尝试加载 .env 配置文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# === 配置 ===
QWEN_API_KEY = os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-plus")

ASR_ENDPOINT = os.environ.get("ASR_ENDPOINT", "https://asr.smartbid.site/inference")
ASR_API_KEY = os.environ.get("ASR_API_KEY", "")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config


# ============================================================ 基础工具

def ffprobe_meta(video_path):
    """获取视频元数据（时长/分辨率/帧率）"""
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=width,height,duration,r_frame_rate',
           '-show_entries', 'format=duration',
           '-of', 'json', video_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
    streams = data.get('streams') or []
    stream = streams[0] if streams else {}
    fmt = data.get('format') or {}
    return {
        'duration': float(fmt.get('duration', 0) or stream.get('duration', 0) or 0),
        'width': int(stream.get('width', 0) or 0),
        'height': int(stream.get('height', 0) or 0),
        'fps': stream.get('r_frame_rate', '30/1'),
    }


def extract_frames(video_path, count=5, max_width=640):
    """均匀抽帧，返回 base64 列表"""
    meta = ffprobe_meta(video_path)
    duration = meta['duration']
    if duration <= 0:
        duration = 10
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            t = duration * (i + 0.5) / count  # 均匀分布
            frame_path = os.path.join(tmp, f'frame_{i}.jpg')
            cmd = ['ffmpeg', '-y', '-ss', str(t), '-i', video_path,
                   '-vframes', '1', '-vf', f'scale={max_width}:-1',
                   '-q:v', '3', frame_path]
            subprocess.run(cmd, capture_output=True, timeout=15)
            if os.path.exists(frame_path):
                with open(frame_path, 'rb') as f:
                    b64 = base64.b64encode(f.read()).decode()
                    frames.append({'time': round(t, 1), 'b64': b64})
    return frames, meta


def detect_scenes_ffmpeg(video_path, threshold=0.3):
    """用 FFmpeg 检测场景切换（简单版，返回时间点列表）"""
    cmd = ['ffmpeg', '-i', video_path, '-filter:v',
           f'select=gt(scene\\,{threshold}),showinfo',
           '-f', 'null', '-']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    scenes = []
    stderr = r.stderr or ''
    for line in stderr.split('\n'):
        if 'pts_time:' in line:
            try:
                t = float(line.split('pts_time:')[1].strip().split()[0])
                scenes.append(t)
            except (ValueError, IndexError):
                pass
    return scenes


# ============================================================ VLM

def vlm_analyze(frames, prompt, max_tokens=1000):
    """调 Qwen3.7-Plus 分析图片"""
    from openai import OpenAI
    client = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)

    content = [{"type": "text", "text": prompt}]
    for f in frames:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{f['b64']}"}
        })

    r = client.chat.completions.create(
        model=QWEN_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens
    )
    text = r.choices[0].message.content if r.choices else ''
    if text is None:
        text = ''
    usage = r.usage
    return {
        'text': text,
        'input_tokens': getattr(usage, 'prompt_tokens', 0) if usage else 0,
        'output_tokens': getattr(usage, 'completion_tokens', 0) if usage else 0,
    }


# ============================================================ ASR

def asr_transcribe(video_path):
    """转录音频，返回词级/句级时间戳. 按 config.ASR_BACKEND 选择 remote/local."""
    if config.ASR_BACKEND == 'local':
        return asr_transcribe_local(video_path)
    return asr_transcribe_remote(video_path)


def asr_transcribe_remote(video_path):
    """调自建 ASR 转录音频，返回词级/句级时间戳"""
    import requests
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
        tmp_mp3 = tmp.name
    try:
        # 提取音频
        subprocess.run(['ffmpeg', '-y', '-i', video_path, '-vn',
                        '-acodec', 'libmp3lame', '-q:a', '2', tmp_mp3],
                       capture_output=True, timeout=120)
        if not os.path.exists(tmp_mp3) or os.path.getsize(tmp_mp3) == 0:
            return None

        # 调 ASR
        with open(tmp_mp3, 'rb') as f:
            resp = requests.post(
                ASR_ENDPOINT,
                headers={"X-API-Key": ASR_API_KEY},
                files={"file": ("audio.mp3", f, "audio/mpeg")},
                timeout=600
            )
        if resp.status_code != 200:
            return {'error': f'ASR {resp.status_code}: {resp.text[:200]}'}

        # 解析响应
        return _parse_asr_response(resp.text)
    except Exception as e:
        return {'error': str(e)}
    finally:
        try: os.unlink(tmp_mp3)
        except: pass


def asr_transcribe_local(video_path):
    """本地 faster-whisper 转录 (离线, 音频不出本机). 需 pip install faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {'error': 'ASR_BACKEND=local 需要 faster-whisper: pip install faster-whisper'}

    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
        tmp_wav = tmp.name
    try:
        subprocess.run(['ffmpeg', '-y', '-i', video_path, '-vn',
                        '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', tmp_wav],
                       capture_output=True, timeout=120)
        if not os.path.exists(tmp_wav) or os.path.getsize(tmp_wav) == 0:
            return {'error': 'ffmpeg 提取音频失败'}

        model = WhisperModel(config.ASR_LOCAL_MODEL, device=config.ASR_LOCAL_DEVICE,
                             compute_type='int8')
        segments_iter, info = model.transcribe(tmp_wav, language='zh',
                                               vad_filter=True, beam_size=5)
        segments = [{'start': round(s.start, 3), 'end': round(s.end, 3),
                     'text': s.text.strip()} for s in segments_iter]
        full_text = ' '.join(s['text'] for s in segments)
        return {'segments': segments, 'full_text': full_text,
                'asr_model': f'faster-whisper/{config.ASR_LOCAL_MODEL}'}
    except Exception as e:
        return {'error': str(e)}
    finally:
        try: os.unlink(tmp_wav)
        except: pass


def _parse_srt(srt_text):
    """把 SRT 格式文本解析为 segments 列表"""
    import re
    segments = []
    blocks = re.split(r'\n\s*\n', srt_text.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        # 找时间行: 00:00:01,070 --> 00:00:07,589
        time_line = None
        text_lines = []
        for line in lines:
            if '-->' in line:
                time_line = line
            elif not line.strip().isdigit() and line.strip():
                text_lines.append(line.strip())

        if not time_line or not text_lines:
            continue

        m = re.match(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', time_line)
        if m:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
            start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
            end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
            text = ' '.join(text_lines)
            segments.append({'start': start, 'end': end, 'text': text})
    return segments


def _parse_asr_response(raw):
    """解析 ASR 响应为 segments 列表（支持 JSON / SRT / 包装格式）"""
    if raw is None:
        return {'segments': [], 'full_text': ''}
    raw = str(raw)

    # 尝试 JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 纯文本 / SRT → 直接解析
        segs = _parse_srt(raw)
        full = ' '.join(s['text'] for s in segs) if segs else raw.strip()
        return {'segments': segs, 'full_text': full}

    # 剥包装 {"code":0,"data":"<SRT字符串>"}
    if isinstance(data, dict) and 'data' in data:
        inner = data['data']
        if isinstance(inner, str):
            # data 可能是 SRT 文本 或 JSON 字符串
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                # 是 SRT 文本
                segs = _parse_srt(inner)
                full = ' '.join(s['text'] for s in segs) if segs else inner.strip()
                return {'segments': segs, 'full_text': full}
        data = inner

    # JSON 列表格式
    segments = []
    if isinstance(data, list):
        for seg in data:
            if isinstance(seg, dict):
                start = float(seg.get('start') or seg.get('start_time') or 0)
                end = float(seg.get('end') or seg.get('end_time') or 0)
                text = ''
                for k in ('text', 'transcript', 'subtitle'):
                    if seg.get(k):
                        text = seg[k].strip(); break
                if text:
                    segments.append({'start': start, 'end': end, 'text': text})
    elif isinstance(data, dict):
        for key in ('segments', 'chunks', 'utterances'):
            if isinstance(data.get(key), list):
                return _parse_asr_response(json.dumps(data[key]))
        text = data.get('text') or data.get('transcript') or ''
        if text:
            segments.append({'start': 0, 'end': 0, 'text': text})

    full_text = ' '.join(s['text'] for s in segments)
    return {'segments': segments, 'full_text': full_text}


# ============================================================ 核心 API

def perceive_video(video_path, do_asr=True, frame_count=5):
    """让 agent "看懂"一个视频: 画面 + 语音 + 场景 + 元数据"""
    result = {}

    # ① 元数据
    result['meta'] = ffprobe_meta(video_path)

    # ② 场景检测
    result['scenes'] = detect_scenes_ffmpeg(video_path)

    # ③ 抽帧 + VLM 画面分析
    frames, _ = extract_frames(video_path, count=frame_count)
    if frames:
        vlm_prompt = f"""你是一个专业的视频分析师。以下是从一个视频（时长{result['meta']['duration']:.1f}秒，{result['meta']['width']}x{result['meta']['height']}）中抽取的{len(frames)}个关键帧。
检测到{len(result['scenes'])}个场景切换。

请分析并返回 JSON:
{{
  "content": "视频主要内容描述",
  "mood": "情绪氛围(如:欢快/悲伤/紧张/平静/浪漫)",
  "quality": "画面质量评估(1-10分) + 简短理由",
  "highlights": ["最精彩的时间区间描述, 如'3-7秒的海浪特写'"],
  "suitable_for": ["适合的用途, 如:产品展示/背景素材/教程/情感"],
  "text_in_frame": "画面中是否有文字/字幕/水印, 如有则提取"
}}"""
        vlm_result = vlm_analyze(frames, vlm_prompt)
        result['visual_analysis'] = vlm_result['text']
        result['vlm_tokens'] = {
            'input': vlm_result['input_tokens'],
            'output': vlm_result['output_tokens']
        }

    # ④ ASR 音频转录
    if do_asr:
        result['audio'] = asr_transcribe(video_path)

    return result


def perceive_result(mp4_path, expectations=None):
    """渲染质检: 让 agent "看一眼" 结果对不对"""
    meta = ffprobe_meta(mp4_path)
    frames, _ = extract_frames(mp4_path, count=8)

    expect_str = json.dumps(expectations, ensure_ascii=False) if expectations else "无特殊要求"

    vlm_prompt = f"""你是一个视频质检员。以下是渲染后的视频关键帧（{meta['duration']:.1f}秒）。
期望: {expect_str}

请检查并返回 JSON:
{{
  "quality_score": 1-10,
  "issues": ["发现的问题, 如:黑屏/文字遮挡/画面模糊"],
  "duration_ok": true/false,
  "suggestions": ["改进建议"]
}}"""

    vlm_result = vlm_analyze(frames, vlm_prompt)
    return {
        'meta': meta,
        'quality': vlm_result['text'],
        'tokens': {'input': vlm_result['input_tokens'], 'output': vlm_result['output_tokens']}
    }


# ============================================================ 测试

if __name__ == '__main__':
    import sys
    video = sys.argv[1] if len(sys.argv) > 1 else None
    if not video:
        # 找 Videos 最新 mp4
        import config
        VIDEOS = config.VIDEOS_DIR
        mp4s = sorted([f for f in os.listdir(VIDEOS) if f.endswith('.mp4')],
                      key=lambda f: os.path.getmtime(os.path.join(VIDEOS, f)), reverse=True)
        if mp4s:
            video = os.path.join(VIDEOS, mp4s[0])
            print('使用最新 mp4:', mp4s[0])
    if not video or not os.path.exists(video):
        print('用法: python perceive.py <video_path>'); sys.exit(1)

    print('\n=== perceive_video 测试 ===')
    t0 = time.time()
    result = perceive_video(video, do_asr=True)
    dt = time.time() - t0
    print(f'耗时: {dt:.1f}s')
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
