# ffmpeg_renderer.py — draft_content.json → ffmpeg 直渲 (方案A 分层之"全部可行"层)
#
# 绕开剪映 GUI: 主视频轨(裁剪/变速/音量) + 图片叠加轨(时间窗/scale/transform) +
# 文本轨(字幕烧录, drawtext) + 音频轨(amix)。确定性成功, 秒级完成, 无需 Windows/剪映。
#
# 分层策略 (render_by_draft_id 调用):
#   analyze_draft_for_ffmpeg() 检测草稿元素:
#     - 全部可行 → render_draft_ffmpeg() 本地直渲
#     - 含可近似元素 (小众转场/滤镜/动画) → 直渲并标注 approximate (暂硬切/忽略)
#     - 含不可行元素 (场景/人物特效/花字/气泡/贴纸) → 返回原因, 回退剪映 GUI
#
# 坐标系 (剪映 Clip_settings): transform_x/y 归一化, 0=居中, 正=右/上.
#   x_center = W*(1+tx)/2, y_center = H*(1-ty)/2
# 图片 scale: 剪映添加图片默认 fit 画布(contain), scale_x/y 在此基础上倍乘.
import json
import math
import os
import subprocess
import tempfile

from ffmpeg_util import resolve_ffmpeg


# ============================================================ 元素检测

# 剪映场景/人物特效、花字、气泡、贴纸 —— ffmpeg 不可行, 必须回退 GUI
_FALLBACK_MATERIAL_KEYS = ('video_effects', 'stickers')
_FALLBACK_TRACK_TYPES = ('effect', 'sticker')


def analyze_draft_for_ffmpeg(content):
    """检测草稿元素是否可 ffmpeg 直渲.
    返回 (mode, reasons): mode ∈ 'direct'(全部可行) / 'approximate'(含可忽略元素) / 'fallback'(必须 GUI).
    reasons: 需要回退/近似的具体元素列表."""
    reasons_fallback = []
    reasons_approx = []
    mats = content.get('materials') or {}
    tracks = content.get('tracks') or []

    for key in _FALLBACK_MATERIAL_KEYS:
        if mats.get(key):
            names = [str(m.get('name') or m.get('effect_type') or '?') for m in mats[key][:5] if isinstance(m, dict)]
            reasons_fallback.append(f'{key}: {names}')

    for t in tracks:
        ttype = t.get('type')
        if ttype in _FALLBACK_TRACK_TYPES:
            reasons_fallback.append(f'{ttype} 轨道: {t.get("name")}')
        # 转场/动画/蒙版 → 近似 (v1 忽略, 硬切/无动画)
        for s in (t.get('segments') or []):
            extra = s.get('extra_material_refs') or []
            if extra:
                reasons_approx.append(f'{t.get("name")} 段含动画/转场/特效引用 x{len(extra)} (v1 忽略)')

    if reasons_fallback:
        return 'fallback', reasons_fallback + reasons_approx
    if reasons_approx:
        return 'approximate', reasons_approx
    return 'direct', []


# ============================================================ 工具

def _resolve_material_path(mat, draft_dir):
    """素材文件定位: path > remote_url > 草稿 assets/ 递归找."""
    name = mat.get('material_name') or mat.get('name') or ''
    for cand in ((mat.get('path') or ''), (mat.get('media_path') or ''), (mat.get('remote_url') or '')):
        cand = cand.replace('\\', '/').strip()
        if cand and os.path.isfile(cand):
            return cand
    if name and draft_dir:
        for root, _dirs, files in os.walk(os.path.join(draft_dir, 'assets')):
            if name in files:
                return os.path.join(root, name)
    return None


def _probe_size(path):
    """ffprobe 视频/图片宽高+时长(秒). 失败返回 (0,0,0)."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height', '-show_entries', 'format=duration',
                            '-of', 'json', path],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
        st = (d.get('streams') or [{}])[0]
        dur = float((d.get('format') or {}).get('duration') or 0)
        return int(st.get('width', 0)), int(st.get('height', 0)), dur
    except Exception:
        return 0, 0, 0


def _ass_escape(text):
    return (text or '').replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}').replace('\n', '\\N')


def _font_px(font_size, canvas_h):
    """剪映字号 → 像素. px ≈ font_size * H / 240 (5→40px@1920, 9→72px, 常见字幕观感)."""
    return max(12, int(round(font_size * canvas_h / 240.0)))


# ============================================================ 主渲染

def render_draft_ffmpeg(draft_dir, out_path, content=None):
    """草稿目录 → mp4 (ffmpeg 直渲). 返回 (ok, info|error)."""
    cp = os.path.join(draft_dir, 'draft_content.json')
    if content is None:
        content = json.load(open(cp, encoding='utf-8'))
    canvas = content.get('canvas_config') or {}
    W = int(canvas.get('width') or 1080)
    H = int(canvas.get('height') or 1920)
    fps = int(content.get('fps') or 30)
    duration = (content.get('duration') or 0) / 1e6

    mats = content.get('materials') or {}
    vids = {m.get('id'): m for m in (mats.get('videos') or []) if isinstance(m, dict)}
    auds = {m.get('id'): m for m in (mats.get('audios') or []) if isinstance(m, dict)}
    txts = {m.get('id'): m for m in (mats.get('texts') or []) if isinstance(m, dict)}

    video_tracks = []
    image_segs = []
    audio_segs = []
    text_segs = []
    for t in (content.get('tracks') or []):
        ttype = t.get('type')
        if ttype == 'text':
            for s in (t.get('segments') or []):
                text_segs.append((t, s))
            continue
        if ttype != 'video':
            continue
        # 主视频轨: 含真实视频素材段; 图片轨: photo 素材段
        for s in (t.get('segments') or []):
            mid = s.get('material_id')
            m = vids.get(mid)
            if not m:
                continue
            if m.get('material_type') == 'photo' or m.get('type') == 'photo':
                image_segs.append((t, s, m))
            else:
                video_tracks.append((t, s, m))
    # 音频轨
    for t in (content.get('tracks') or []):
        if t.get('type') != 'audio':
            continue
        for s in (t.get('segments') or []):
            m = auds.get(s.get('material_id'))
            if m:
                audio_segs.append((t, s, m))

    if not video_tracks:
        return False, '草稿没有主视频段 (ffmpeg 直渲需要至少一段视频素材)'

    # ---- 输入列表 ----
    inputs = []          # 每元素: {path, seg, mat, kind}
    for _t, s, m in video_tracks:
        p = _resolve_material_path(m, draft_dir)
        if not p:
            return False, f"主视频素材文件找不到: {m.get('material_name')}"
        inputs.append({'kind': 'video', 'path': p, 'seg': s, 'mat': m})
    for _t, s, m in image_segs:
        p = _resolve_material_path(m, draft_dir)
        if p:
            inputs.append({'kind': 'image', 'path': p, 'seg': s, 'mat': m})
    for _t, s, m in audio_segs:
        p = _resolve_material_path(m, draft_dir)
        if p:
            inputs.append({'kind': 'audio', 'path': p, 'seg': s, 'mat': m})

    # ---- filter graph ----
    cmd = [resolve_ffmpeg(), '-y']
    for it in inputs:
        cmd += ['-i', it['path']]

    fc = []
    vlabels = []     # [(label, seg)] 视频段 (concat 用)
    alabels = []     # [(label, seg, mat, kind)] 音频段
    for idx, it in enumerate(inputs):
        seg = it['seg']
        sr = seg.get('source_timerange') or {}
        s_start = (sr.get('start') or 0) / 1e6
        s_dur = (sr.get('duration') or 0) / 1e6
        speed = ((seg.get('speed') or {}).get('speed') if isinstance(seg.get('speed'), dict) else None) or 1.0
        if it['kind'] in ('video', 'image'):
            f = f'[{idx}:v]'
            if it['kind'] == 'image':
                f += 'loop=loop=-1:size=1,'
            f += f'trim=start={s_start:.3f}'
            if s_dur > 0:
                f += f':duration={s_dur:.3f}'
            f += ',setpts=PTS-STARTPTS'
            if it['kind'] == 'video':
                if speed != 1.0:
                    f += f',setpts=PTS/{speed}'
                # 主视频: 缩放铺满画布 (cover+居中裁剪), 保证输出尺寸一致
                f += (f',scale={W}:{H}:force_original_aspect_ratio=increase,'
                      f'crop={W}:{H},setsar=1,fps={fps}')
                f += f'[v{idx}]'
                vlabels.append((f'[v{idx}]', seg))
            else:
                # 图片: fit 画布后按 clip scale 倍乘
                clip = seg.get('clip') or {}
                sx = float((clip.get('scale') or {}).get('x', 1) or 1)
                sy = float((clip.get('scale') or {}).get('y', 1) or 1)
                f += (f',scale=w={W}:h={H}:force_original_aspect_ratio=decrease,'
                      f'scale=w=iw*{sx}:h=ih*{sy},setsar=1,fps={fps}[v{idx}]')
                vlabels.append((f'[v{idx}]', seg, it))
            fc.append(f)   # 输入处理链必须进 graph (漏了这行: [v0] 从未定义, 全图崩)
        else:
            f = f'[{idx}:a]atrim=start={s_start:.3f}'
            if s_dur > 0:
                f += f':duration={s_dur:.3f}'
            f += ',asetpts=PTS-STARTPTS'
            if speed != 1.0:
                f += f',atempo={speed}'
            vol = seg.get('volume')
            if vol is not None and float(vol) != 1.0:
                f += f',volume={float(vol)}'
            f += f'[a{idx}]'
            alabels.append((f'[a{idx}]', seg, it['mat'], it['kind']))
            fc.append(f)

    # 主视频 concat (多段拼接; 段间直接按 target 顺序)
    vlabels.sort(key=lambda x: (x[1].get('target_timerange') or {}).get('start', 0))
    if len(vlabels) == 1 and vlabels[0][1].get('material_id') == inputs[0]['seg'].get('material_id') and vlabels[0][0].startswith('[v0'):
        base = vlabels[0][0]
    else:
        # 仅取主视频段 (kind=video 的那些), 图片段不 concat
        mains = [v for v in vlabels if len(v) == 2 or (len(v) == 3 and v[2].get('kind') == 'video')]
        mains = [v for v in vlabels if v[0] in [f'[v{i}]' for i, it in enumerate(inputs) if it['kind'] == 'video']]
        if len(mains) > 1:
            cat_in = ''.join(v[0] for v in mains) + f'concat=n={len(mains)}:v=1:a=0[vcat]'
            fc.append(cat_in)
            base = '[vcat]'
        else:
            base = mains[0][0] if mains else vlabels[0][0]

    # 图片 overlay (按 target 时间窗, transform 定位)
    cur = base
    img_count = 0
    for idx, it in enumerate(inputs):
        if it['kind'] != 'image':
            continue
        seg = it['seg']
        tr = seg.get('target_timerange') or {}
        t_start = (tr.get('start') or 0) / 1e6
        t_dur = (tr.get('duration') or 3000) / 1e6
        t_end = t_start + t_dur
        clip = seg.get('clip') or {}
        tx = float((clip.get('transform') or {}).get('x', 0) or 0)
        ty = float((clip.get('transform') or {}).get('y', 0) or 0)
        alpha = float(clip.get('alpha', 1) or 1)
        iw, ih, _ = _probe_size(it['path'])
        # overlay 位置: 中心对齐 + transform 偏移
        ox = f'(W*(1+{tx})/2)-(overlay_w/2)'
        oy = f'(H*(1-({ty}))/2)-(overlay_h/2)'
        lbl = f'[v{idx}]'
        if alpha < 1.0:
            fc.append(f'{lbl}colorchannelmixer=aa={alpha}[v{idx}a]')
            lbl = f'[v{idx}a]'
        nxt = f'[ov{img_count}]'
        fc.append(f'{cur}{lbl}overlay=x={ox}:y={oy}:enable=\'between(t,{t_start:.3f},{t_end:.3f})\'{nxt}')
        cur = nxt
        img_count += 1

    # 字幕: 生成 ASS 文件用 subtitles 滤镜烧录 (libass 自动换行, drawtext 不换行会溢出画布)
    if text_segs:
        ass_path = _build_ass(text_segs, txts, W, H)
        fc.append(f"{cur}subtitles='{ass_path}'[vsub]")
        cur = '[vsub]'

    fc.append(f'{cur}format=yuv420p[vout]')

    # ---- 音频: 主视频音频 + 音频轨混音 ----
    a_out = None
    main_a = '[0:a]' if inputs and inputs[0]['kind'] == 'video' else None
    if alabels:
        mix_in = (main_a or '') + ''.join(l for l, _s, _m, _k in alabels)
        n = (1 if main_a else 0) + len(alabels)
        fc.append(f'{mix_in}amix=inputs={n}:duration=first:dropout_transition=0[aout]')
        a_out = '[aout]'
    elif main_a is not None:
        a_out = '0:a'

    filtergraph = ';'.join(fc)
    cmd += ['-filter_complex', filtergraph, '-map', '[vout]']
    if a_out:
        cmd += ['-map', a_out, '-c:a', 'aac', '-b:a', '160k']
    if duration > 0:
        cmd += ['-t', f'{duration:.3f}']
    # 编码器: 默认 NVENC 硬件编码 (有 N 卡时, 比 libx264 快 5-10 倍);
    # FFMPEG_VIDEO_CODEC 可覆盖 (h264_nvenc/hevc_nvenc/libx264); 无 NVENC 自动回退 libx264.
    vcodec = _pick_video_codec()
    if vcodec == 'libx264':
        cmd += ['-c:v', 'libx264', '-preset', 'medium', '-crf', '20']
    else:
        # NVENC: 老版 (ffmpeg<5) 无 p1-p7 命名预设, 用 hq; 新版可 FFMPEG_NVENC_PRESET=p4 覆盖
        preset = os.environ.get('FFMPEG_NVENC_PRESET', 'hq')
        cmd += ['-c:v', vcodec, '-preset', preset, '-rc', 'vbr', '-cq', '22', '-b:v', '0']
    cmd += ['-pix_fmt', 'yuv420p', '-movflags', '+faststart', out_path]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return False, 'ffmpeg 失败: ' + (r.stderr or '')[-400:]
    return True, {'out': out_path, 'segments': len(inputs), 'images': img_count,
                  'cmd_preview': ' '.join(cmd)[:300]}




def _ass_time(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def _build_ass(text_segs, txts, W, H):
    """从草稿文本段生成 ASS 字幕文件 (PlayRes=画布, 自动换行, 位置按 transform_y)."""
    # 字号映射: 剪映 15 ≈ 底部大标题观感. px = font_size * H / 320 (15→90px@1920 偏大,
    # 实测 15 是"加大后"的字号; 常规 5 → 30px). 可按观感再调.
    def fs_px(fs):
        return max(14, int(round(float(fs) * H / 320.0)))

    def ass_color(hex_color):
        c = (hex_color or '#FFFFFF').lstrip('#')
        if len(c) == 6:
            r, g, b = c[0:2], c[2:4], c[4:6]
            return f'&H00{b}{g}{r}'
        return '&H00FFFFFF'

    events = []
    for _t, s in text_segs:
        m = txts.get(s.get('material_id')) or {}
        tr = s.get('target_timerange') or {}
        t_start = (tr.get('start') or 0) / 1e6
        t_end = t_start + ((tr.get('duration') or 0) / 1e6)
        try:
            content_obj = m.get('content')
            text = json.loads(content_obj).get('text', '') if isinstance(content_obj, str) else (content_obj or {}).get('text', '')
        except Exception:
            text = ''
        if not text.strip():
            continue
        fs = m.get('font_size') or 5
        color = m.get('font_color') or '#FFFFFF'
        clip = s.get('clip') or {}
        ty = float((clip.get('transform') or {}).get('y', -0.8) or -0.8)
        px = fs_px(fs)
        if ty <= 0:
            align, margin_v = 2, int(H * (1 + ty) / 2)   # 底部对齐: 距底边
        else:
            align, margin_v = 8, int(H * (1 - ty) / 2)   # 顶部对齐: 距顶边
        events.append(
            f'Dialogue: 0,{_ass_time(t_start)},{_ass_time(t_end)},Default,,0,0,{margin_v},,'
            f'{{\\an{align}\\fs{px}\\bord2\\shad0\\1c{ass_color(color)}}}'
            f'{_ass_escape(text)}')

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,2,60,60,MARGINV_PLACEHOLDER,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    body = header.replace('MARGINV_PLACEHOLDER', '40') + '\n'.join(events)
    import tempfile as _tf
    fd, path = _tf.mkstemp(suffix='.ass')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(body)
    return path


def _pick_video_codec():
    """选视频编码器: FFMPEG_VIDEO_CODEC 环境变量优先; 否则有 NVENC 用 h264_nvenc, 无则 libx264."""
    forced = os.environ.get('FFMPEG_VIDEO_CODEC', '').strip()
    if forced:
        return forced
    try:
        r = subprocess.run([resolve_ffmpeg(), '-hide_banner', '-encoders'],
                           capture_output=True, text=True, timeout=10)
        if 'h264_nvenc' in (r.stdout or ''):
            # 再确认有 N 卡 (encoder 列出不代表可用)
            try:
                import ctypes
                subprocess.run(['nvidia-smi', '-L'], capture_output=True, timeout=5)
                return 'h264_nvenc'
            except Exception:
                pass
    except Exception:
        pass
    return 'libx264'


def _find_font():
    """找可用的中文字体 (drawtext fontfile)."""
    for p in ('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
              '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'):
        if os.path.isfile(p):
            return p
    # fc-list 兜底
    try:
        r = subprocess.run(['fc-list', ':lang=zh', 'file'], capture_output=True, text=True, timeout=5)
        first = (r.stdout or '').splitlines()
        if first:
            return first[0].split(':')[0].strip()
    except Exception:
        pass
    return '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
