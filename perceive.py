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

# === DeepSeek (聊天 agent 用; 配了 key 则聊天走 DeepSeek, 否则回退 Qwen) ===
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
# 感知分析 (VLM 看画面) 是否改走 DeepSeek. 默认关 (Qwen-VL 稳定).
# 开启需 DeepSeek 视觉模型 (deepseek-v4-flash-vision-exp).
PERCEIVE_USE_DEEPSEEK = os.environ.get("PERCEIVE_USE_DEEPSEEK", "0") == "1"
DEEPSEEK_VISION_MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "deepseek-v4-flash-vision-exp")

ASR_ENDPOINT = os.environ.get("ASR_ENDPOINT", "https://asr.smartbid.site/inference")
ASR_API_KEY = os.environ.get("ASR_API_KEY", "")

# 分析优先级: 默认优先用 ASR(听) 判断视频内容, 只有 ASR 不可用/转录为空时才回退到 VLM(看)
# 省 VLM 调用的 token/时间 —— 大部分素材靠语音已经能判断内容, 画面分析留作补充手段.
PREFER_ASR = os.environ.get("PREFER_ASR", "1") == "1"

# Whisper 系模型在喂入静音/纯噪音/无法识别的音频时会"幻觉"出一些从训练数据里背下来的
# 固定短句, 而不是老实返回空文本 —— 最典型的就是这句俄语字幕组水印。这类音频通常被
# webrtcvad 误判成"有语音"(风声/引擎声/环境噪音的能量包络和人声接近), 导致明明没有
# 真实人声内容也被送去 ASR, 转出这类幻觉句子。命中黑名单就当 ASR 结果不可用, 回退 VLM。
ASR_HALLUCINATION_PATTERNS = [
    "субтитры создавал",       # "Субтитры создавал DimaTorzok" 及变体
    "субтитры делал",
    "amara.org",
    "subtitles by the amara.org community",
    "www.zeoob.com",
]


def _looks_like_asr_hallucination(audio_result):
    """粗略识别 Whisper 类模型在无真实语音时吐出的固定幻觉句子。"""
    if not isinstance(audio_result, dict):
        return False
    text = (audio_result.get('full_text') or '').strip().lower()
    if not text:
        return False
    return any(p in text for p in ASR_HALLUCINATION_PATTERNS)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config


# ============================================================ 基础工具

def ffprobe_meta(video_path):
    """获取视频元数据（时长/分辨率/帧率）.
    手机竖拍视频常以横编码 + rotation side data (90/270) 表示竖屏 —— width/height
    按"显示方向"返回 (90/270 时交换), 并带 rotation 字段记录原始旋转指令."""
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=width,height,duration,r_frame_rate,side_data_list',
           '-show_entries', 'format=duration',
           '-of', 'json', video_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                       errors='replace')
    try:
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
    streams = data.get('streams') or []
    stream = streams[0] if streams else {}
    fmt = data.get('format') or {}
    # 旋转指令: 新版在 side_data_list[].rotation, 旧版在 stream tags rotate;
    # 部分 ffprobe 构建的 JSON 模式不展开 rotation 字段, 再用 csv 模式兜底探测一次
    rotation = 0
    for sd in (stream.get('side_data_list') or []):
        rot = sd.get('rotation')
        if rot is not None:
            rotation = int(rot)
            break
    if not rotation:
        try:
            rotation = int((stream.get('tags') or {}).get('rotate', 0) or 0)
        except (TypeError, ValueError):
            rotation = 0
    if not rotation:
        try:
            r2 = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'side_data=rotation', '-of', 'csv=p=0',
                 '-read_intervals', '%+#1', video_path],
                capture_output=True, text=True, timeout=15, errors='replace')
            for line in (r2.stdout or '').splitlines():
                line = line.strip()
                if line:
                    rotation = abs(int(float(line)))
                    break
        except (TypeError, ValueError):
            rotation = 0
    rotation = rotation % 360
    w = int(stream.get('width', 0) or 0)
    h = int(stream.get('height', 0) or 0)
    if rotation in (90, 270):
        w, h = h, w
    return {
        'duration': float(fmt.get('duration', 0) or stream.get('duration', 0) or 0),
        'width': w,
        'height': h,
        'fps': stream.get('r_frame_rate', '30/1'),
        'rotation': rotation,
    }


def has_audio_stream(video_path):
    """ffprobe 查是否存在音频轨 (读容器元数据, 不解码, 通常 <200ms, 零网络请求)."""
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a',
           '-show_entries', 'stream=index', '-of', 'csv=p=0', video_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors='replace')
        return bool(r.stdout.strip())
    except Exception:
        return True  # 探测失败保守处理: 当作"可能有音频", 交给后面 ASR 结果兜底判断


def audio_likely_has_speech(video_path, duration):
    """判断音轨是否可能含人声, 用来在真正调远程 ASR 之前先排除"没有语言"的视频 ——
    那类视频走一遍 ASR 纯属浪费一次网络往返(上传音频+等转录), 结果注定是空的。
    没有音频轨直接判 False。优先用 webrtcvad 做真正的语音活动检测(能区分人声和环境音/
    音乐, 单纯按分贝判静音区分不了这个); webrtcvad 不可用时退化成静音检测(能拦掉无
    音轨/彻底静音的视频, 但环境音/BGM 会被误判成"可能有语音", 走一次 ASR 兜底)。
    任何检测失败都保守判 True, 照旧走 ASR, 避免误杀真的有语音的内容。"""
    if not has_audio_stream(video_path):
        return False
    if not duration or duration <= 0:
        return True
    try:
        return _vad_has_speech(video_path)
    except ImportError:
        return _silence_detect_has_speech(video_path, duration)
    except Exception:
        # VAD 跑到一半炸了(ffmpeg 抽取 PCM 失败等) —— 别让检测本身的问题拖垮整个分析,
        # 退回静音检测再试一次; 静音检测也失败的话它自己会保守返回 True.
        return _silence_detect_has_speech(video_path, duration)


def _vad_has_speech(video_path, sample_rate=16000, frame_ms=30, speech_ratio_threshold=0.4):
    """webrtcvad: 按 30ms 帧扫描音轨, >= threshold 比例的帧被判定为人声才算有语音.
    实测: mode=3(最挑剔) 下纯环境音(水声/夜间市声)视频约 14% 帧被误判成人声, 真人说话
    的视频约 98%; 阈值设 40%, 留足余量避免把间歇性说话的视频误杀, 同时能拦掉环境音。"""
    import webrtcvad
    cmd = ['ffmpeg', '-v', 'error', '-i', video_path, '-vn',
           '-ac', '1', '-ar', str(sample_rate), '-f', 's16le', '-']
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    pcm = r.stdout
    if not pcm:
        return True
    vad = webrtcvad.Vad(3)  # 0-3, 3=对"是否人声"最挑剔, 最不容易把噪音/音乐误判成人声
    frame_bytes = int(sample_rate * frame_ms / 1000) * 2  # 16-bit PCM = 2 bytes/sample
    total = speech = 0
    for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        total += 1
        try:
            if vad.is_speech(pcm[i:i + frame_bytes], sample_rate):
                speech += 1
        except Exception:
            continue
    if total == 0:
        return True
    return (speech / total) >= speech_ratio_threshold


def _silence_detect_has_speech(video_path, duration):
    """兜底方案 (webrtcvad 不可用时): ffmpeg silencedetect 按分贝判静音, 只能拦掉
    无音轨/彻底静音, 分不清"有环境音但没人声"和"真的有人说话"。"""
    cmd = ['ffmpeg', '-i', video_path, '-vn', '-af', 'silencedetect=noise=-35dB:d=0.3',
           '-f', 'null', '-']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, errors='replace')
    except Exception:
        return True
    silence_total = 0.0
    for line in (r.stderr or '').split('\n'):
        if 'silence_duration:' in line:
            try:
                silence_total += float(line.split('silence_duration:')[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return (silence_total / duration) < 0.95



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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           errors='replace')
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

def vlm_analyze(frames, prompt, max_tokens=None):
    """调 VLM 分析图片. 默认 Qwen-VL; 设置页勾选"感知分析用 DeepSeek"且配了 key 时走 DeepSeek 视觉模型."""
    from openai import OpenAI
    if PERCEIVE_USE_DEEPSEEK and DEEPSEEK_API_KEY:
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        model = DEEPSEEK_VISION_MODEL
    else:
        client = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)
        model = QWEN_MODEL

    content = [{"type": "text", "text": prompt}]
    for f in frames:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{f['b64']}"}
        })

    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        **({'max_tokens': max_tokens} if max_tokens else {})
    )
    msg = r.choices[0].message if r.choices else None
    text = getattr(msg, 'content', None) or ''
    if not text.strip():
        # 思考型模型偶发把内容放进 reasoning_content, 或思考耗尽 token 导致正文为空
        text = getattr(msg, 'reasoning_content', None) or ''
    if not text.strip():
        # 空正文不当成功缓存 (否则前端只剩尺寸元数据, 无描述) —— 重试一次, 仍空则报错
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens
        )
        msg = r.choices[0].message if r.choices else None
        text = getattr(msg, 'content', None) or ''
    if not text.strip():
        raise RuntimeError(f'VLM ({model}) 返回空正文 (output_tokens='
                           f'{getattr(r.usage, "completion_tokens", 0) if r.usage else 0}), 请重试或换模型')
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


def _segments_to_srt(segments):
    """segments [{start,end,text}] → 标准 SRT 文本"""
    def ts(t):
        ms = int(round(t * 1000))
        h = ms // 3600000; ms %= 3600000
        m = ms // 60000; ms %= 60000
        s = ms // 1000; ms %= 1000
        return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f'{ts(seg["start"])} --> {ts(seg["end"])}')
        out.append(seg.get('text', ''))
        out.append('')
    return '\n'.join(out)


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


def _extract_json_field(text, field):
    """从 VLM 返回的 markdown/JSON 混排文本里挖出某个字段, 拿不到就返回 None.
    tags 检索要走这个而不是重新调 VLM, 所以字段名和 vlm_prompt 里要的 JSON key 要对齐。"""
    import re as _re
    if not text:
        return None
    try:
        m = _re.search(r'\{[\s\S]*\}', text)
        if not m:
            return None
        return json.loads(m.group()).get(field)
    except Exception:
        return None


# ============================================================ 核心 API

def perceive_video(video_path, do_asr=True, frame_count=5):
    """让 agent "看懂"一个视频: 画面 + 语音 + 场景 + 元数据.

    分析优先级 (PREFER_ASR, 默认开, Settings 页可关): 先跑 ASR, 转录成功且有实际
    文字内容 → 直接用, 不再花 VLM token 去"看"画面; ASR 未配置/转录失败/静音视频
    (无文字) 时才回退到 VLM 抽帧分析。PREFER_ASR=0 时维持旧行为: VLM+ASR 都跑。"""
    result = {}

    # ① 元数据
    result['meta'] = ffprobe_meta(video_path)
    result['tags'] = []  # 只有跑了 VLM 才会有值; ASR 模式没有画面标签, 检索时退化到全文转录

    # ② 场景检测
    result['scenes'] = detect_scenes_ffmpeg(video_path)

    # ③ ASR 音频转录 (若开启优先级, 提前到 VLM 之前跑, 用来判断是否还需要 VLM)
    # 先本地静音检测排除"没有语言"的视频, 省一次远程 ASR 往返(注定是空转录)
    audio_result = None
    if do_asr:
        if audio_likely_has_speech(video_path, result['meta'].get('duration', 0)):
            audio_result = asr_transcribe(video_path)
        else:
            audio_result = {'full_text': '', 'segments': [], 'skipped': 'no_speech_detected'}
    asr_usable = bool(
        do_asr and isinstance(audio_result, dict)
        and not audio_result.get('error') and audio_result.get('full_text', '').strip()
    )
    if asr_usable and _looks_like_asr_hallucination(audio_result):
        audio_result['hallucination_filtered'] = True
        asr_usable = False

    # ④ VLM 画面分析: PREFER_ASR 且 ASR 已经给出可用文字内容时跳过, 否则(包括 PREFER_ASR=0)照跑
    need_vlm = not (PREFER_ASR and asr_usable)
    if need_vlm:
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
  "text_in_frame": "画面中是否有文字/字幕/水印, 如有则提取",
  "tags": ["5-8个描述画面主体/场景/物体的短关键词, 如'山间','水塘','凉亭','高压线','自然风景'"]
}}"""
            vlm_result = vlm_analyze(frames, vlm_prompt)
            result['visual_analysis'] = vlm_result['text']
            result['tags'] = _extract_json_field(vlm_result['text'], 'tags') or []
            result['vlm_tokens'] = {
                'input': vlm_result['input_tokens'],
                'output': vlm_result['output_tokens']
            }

    result['analysis_mode'] = 'asr' if (PREFER_ASR and asr_usable) else ('vlm' if need_vlm else 'none')

    # ⑤ 挂载 ASR 结果
    if do_asr:
        result['audio'] = audio_result
        # 幻觉句子已判定不可用, 别把它当正经字幕/文案挂出去误导前端和下游剪辑逻辑
        if isinstance(audio_result, dict) and audio_result.get('hallucination_filtered'):
            audio_result['raw_full_text'] = audio_result.get('full_text', '')
            audio_result['full_text'] = ''
            audio_result['segments'] = []
        if isinstance(audio_result, dict) and audio_result.get('segments'):
            result['srt'] = _segments_to_srt(audio_result['segments'])

    return result


def perceive_image(image_path, max_width=1024):
    """让 agent "看懂"一张图片: 没有时长/场景/语音这些视频概念, 直接把整张图扔给 VLM 描述.
    发之前用 ffmpeg 缩到 max_width 等比缩放 —— 原图动辄几千像素几MB, 不缩的话
    传给 VLM 的 base64 payload 又大又贵, 分析质量跟这点分辨率也没关系。"""
    meta = ffprobe_meta(image_path)
    with tempfile.TemporaryDirectory() as tmp:
        resized = os.path.join(tmp, 'resized.jpg')
        cmd = ['ffmpeg', '-y', '-i', image_path,
               '-vf', f"scale='min({max_width},iw)':-1", '-q:v', '3', resized]
        subprocess.run(cmd, capture_output=True, timeout=30)
        src = resized if os.path.exists(resized) else image_path
        with open(src, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()

    prompt = f"""你是一个专业的图片分析师。以下是一张图片（原始尺寸 {meta['width']}x{meta['height']}）。
请分析并返回 JSON:
{{
  "content": "图片主要内容描述",
  "mood": "情绪氛围/风格(如:明快/复古/商务/温馨)",
  "quality": "画质评估(1-10分) + 简短理由",
  "suitable_for": ["适合的用途, 如:视频封面/背景素材/插图/产品图"],
  "text_in_image": "图片中是否有文字/水印, 如有则提取",
  "tags": ["5-8个描述图片主体/场景/物体的短关键词, 如'山间','水塘','凉亭','高压线','自然风景'"]
}}"""
    # 截图类图片文字量大, 默认 1000 tokens 会把 JSON 掐断在 text_in_image 中间,
    # 前端解析不出字段 —— 图片分析放宽到 3000.
    vlm_result = vlm_analyze([{'time': 0, 'b64': b64}], prompt)
    return {
        'meta': {'width': meta['width'], 'height': meta['height']},
        'visual_analysis': vlm_result['text'],
        'tags': _extract_json_field(vlm_result['text'], 'tags') or [],
        'vlm_tokens': {'input': vlm_result['input_tokens'], 'output': vlm_result['output_tokens']},
        'analysis_mode': 'vlm',
    }



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
