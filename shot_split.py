# shot_split.py — 视频分镜拆分 (GPU 镜头边界检测 + 按镜头切独立小视频/关键帧)
#
# 镜头边界检测用预训练 CNN(resnet18, ImageNet 权重) 提取每帧语义特征, 相邻帧特征的余弦
# 距离出现统计显著跳变的地方判定为切镜——比单纯比较像素/色彩直方图(PySceneDetect 那套)
# 更能扛住画面内的运动/光线变化, 只在真正"内容变了"时才触发; 特征提取是批量矩阵运算,
# 扔 GPU 上跑, 真正用上这台机器的显卡, 不是挂个名。
# 权重来自 torchvision 官方 CDN 首次调用自动下载(~45MB), 不用像 TransNetV2 那样手动去
# 网盘/GDrive 找模型文件、还要装老版 TensorFlow 才能转出可用的 PyTorch 权重。
import os, json, subprocess, tempfile

import config

SHOTS_ROOT = os.path.join(config.UPLOAD_DIR, '_shots')

_model = None
_device = None
_transform = None


def _load_model():
    """懒加载特征提取器, 全局只加载一次。有 GPU 就上 GPU, 没有就退化 CPU 照常跑(不报错,
    只是慢), 方便这套代码在没显卡的开发机上也能用。"""
    global _model, _device, _transform
    if _model is not None:
        return _model, _device, _transform

    import torch
    from torchvision.models import resnet18, ResNet18_Weights

    _device = 'cuda' if torch.cuda.is_available() else 'cpu'
    weights = ResNet18_Weights.DEFAULT
    net = resnet18(weights=weights)
    net.fc = torch.nn.Identity()   # 只要 512 维池化特征, 不要 ImageNet 分类头
    net.eval().to(_device)
    _model = net
    _transform = weights.transforms()   # 官方配套预处理(resize/crop/归一化), 不用自己拼
    return _model, _device, _transform


def _probe_duration(video_path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', video_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors='replace')
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _extract_sample_frames(video_path, sample_fps, tmp_dir):
    """按固定帧率(默认 5fps)抽帧到临时目录, 而不是逐帧扫描原始 24-30fps —— 镜头切换检测
    不需要那么密, 5fps 已经能把边界定位到 0.2 秒内, 换来好几倍的抽帧+推理速度。
    返回按时间顺序排好的帧文件路径列表。"""
    pattern = os.path.join(tmp_dir, 'f_%06d.jpg')
    subprocess.run(
        ['ffmpeg', '-y', '-i', video_path, '-vf', f'fps={sample_fps}', '-q:v', '4', pattern],
        capture_output=True, timeout=180
    )
    files = sorted(f for f in os.listdir(tmp_dir) if f.startswith('f_') and f.endswith('.jpg'))
    return [os.path.join(tmp_dir, f) for f in files]


def _merge_short_shots(shots, min_shot_sec):
    """把时长不足 min_shot_sec 的碎片镜头并进相邻镜头(通常是开头/结尾的过场帧):
    - 非开头的短镜头并进前一个;
    - 开头仍太短的再并进第二个(把第二个的 start 拉回 0)。
    重新编号后返回。"""
    if min_shot_sec <= 0 or len(shots) <= 1:
        return shots
    out = []
    for s in shots:
        if out and s['duration'] < min_shot_sec:
            prev = out[-1]
            prev['end'] = s['end']
            prev['duration'] = round(s['end'] - prev['start'], 3)
        else:
            out.append(dict(s))
    while len(out) >= 2 and out[0]['duration'] < min_shot_sec:
        second = out[1]
        second['start'] = out[0]['start']
        second['duration'] = round(second['end'] - second['start'], 3)
        out.pop(0)
    for i, s in enumerate(out):
        s['index'] = i
    return out


def detect_shots(video_path, sample_fps=5, min_scene_len_sec=0.6, z_thresh=2.0, min_abs_dist=0.10, min_shot_sec=1.0):
    """GPU CNN 特征检测镜头边界。返回 [{index, start, end, duration}] (秒, 相对原视频)。

    resnet18 语义特征的余弦距离尺度很小: 同一镜头内的运动/光线变化通常 <0.08,
    真正的画面内容切换(含渐变转场)约 0.12-0.20。所以 min_abs_dist 用一个 0.10
    的绝对下限挡住纯噪声, 再配合 z_thresh 用每段视频自身分布的 z-score 做自适应
    判定 —— 不要把它设成 0.25 这种按原始像素距离拍的阈值, 那会直接卡死所有检测。
    min_shot_sec: 拆完后时长不足该值的碎片镜头并进相邻镜头(如开头 0.4s 的过场)。"""
    import torch
    import numpy as np
    from PIL import Image

    total_duration = _probe_duration(video_path)
    model, device, transform = _load_model()

    with tempfile.TemporaryDirectory() as tmp:
        frame_files = _extract_sample_frames(video_path, sample_fps, tmp)
        if len(frame_files) < 2:
            dur = round(total_duration, 3) or 0.0
            return [{'index': 0, 'start': 0.0, 'end': dur, 'duration': dur}]

        embeds = []
        batch_size = 32
        with torch.no_grad():
            for i in range(0, len(frame_files), batch_size):
                batch = frame_files[i:i + batch_size]
                imgs = torch.stack([transform(Image.open(f).convert('RGB')) for f in batch]).to(device)
                feats = model(imgs)
                feats = torch.nn.functional.normalize(feats, dim=1)   # 归一化后点乘即余弦相似度
                embeds.append(feats.cpu())
        embeds = torch.cat(embeds, dim=0)

    sims = (embeds[:-1] * embeds[1:]).sum(dim=1).numpy()
    dists = 1 - sims

    mean, std = float(dists.mean()), float(dists.std() or 1e-6)
    thresh = max(min_abs_dist, mean + z_thresh * std)
    boundary_frame_idxs = [i + 1 for i, d in enumerate(dists) if d > thresh]

    interval = 1.0 / sample_fps
    # 用 round 而不是 int: 0.6/0.2 浮点结果是 2.999...，int 会截成 2，
    # 导致同一处渐变转场被拆成两个相距 0.2~0.4s 的假边界。
    min_gap_frames = max(1, round(min_scene_len_sec / interval))
    merged = []
    for b in boundary_frame_idxs:
        if merged and b - merged[-1] < min_gap_frames:
            continue
        merged.append(b)

    total_frames = len(frame_files)
    cut_frames = [0] + merged + [total_frames]
    shots = []
    for i in range(len(cut_frames) - 1):
        start = round(cut_frames[i] * interval, 3)
        is_last = (i == len(cut_frames) - 2)
        end = round(total_duration, 3) if is_last and total_duration else round(cut_frames[i + 1] * interval, 3)
        shots.append({'index': i, 'start': start, 'end': end, 'duration': round(end - start, 3)})
    return _merge_short_shots(shots, min_shot_sec)


def _shot_dir(video_path):
    base = os.path.splitext(os.path.basename(video_path))[0]
    return os.path.join(SHOTS_ROOT, base), base


def get_cached_shots(video_path):
    """只读缓存 manifest, 不触发检测/切分。没切过返回 None。"""
    shot_dir, _ = _shot_dir(video_path)
    manifest_path = os.path.join(shot_dir, 'shots.json')
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, encoding='utf-8') as f:
        return json.load(f)


def _cleanup_shots(base, shot_dir):
    """重拆前清掉该素材上次切出的镜头片段(资产目录顶层) + 缩略图, 避免旧片段残留。"""
    try:
        for f in os.listdir(config.UPLOAD_DIR):
            if f.startswith(base + '_shot') and f.endswith('.mp4'):
                try:
                    os.remove(os.path.join(config.UPLOAD_DIR, f))
                except Exception:
                    pass
    except Exception:
        pass
    if os.path.isdir(shot_dir):
        for f in os.listdir(shot_dir):
            try:
                os.remove(os.path.join(shot_dir, f))
            except Exception:
                pass


def split_shots(video_path, force=False, sample_fps=5, min_scene_len_sec=0.6, min_shot_sec=1.0):
    """检测镜头边界, 并实际切出每个镜头的独立视频文件 + 中点关键帧, 结果缓存到
    shots.json (镜头切分对大视频较慢, 非 force 时命中缓存直接返回, 不重跑检测)。

    切出来的镜头视频直接写到资产目录(UPLOAD_DIR)顶层, 命名 <原文件名>_shot000.mp4,
    这样它自动成为一条普通素材(可再分析/检索/进草稿), 不用用户手动导入。"""
    if not force:
        cached = get_cached_shots(video_path)
        if cached:
            return cached

    shot_dir, base = _shot_dir(video_path)
    os.makedirs(shot_dir, exist_ok=True)
    if force:
        _cleanup_shots(base, shot_dir)

    shots = detect_shots(video_path, sample_fps=sample_fps, min_scene_len_sec=min_scene_len_sec, min_shot_sec=min_shot_sec)

    for shot in shots:
        idx = shot['index']

        # 镜头视频直接落到资产目录顶层 -> 自动成为素材 (-ss/-to 放 -i 之后走精确解码, 保证边界准)
        clip_path = os.path.join(config.UPLOAD_DIR, f'{base}_shot{idx:03d}.mp4')
        subprocess.run(
            ['ffmpeg', '-y', '-i', video_path, '-ss', str(shot['start']), '-to', str(shot['end']),
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-c:a', 'aac', '-b:a', '128k', clip_path],
            capture_output=True, timeout=120
        )
        shot['clip_path'] = clip_path if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0 else None

        # 关键帧只是给人看的缩略图, 放在 _shots/<base>/ 下, 不进资产列表
        mid = shot['start'] + shot['duration'] / 2
        keyframe_path = os.path.join(shot_dir, f'{base}_shot{idx:03d}.jpg')
        subprocess.run(
            ['ffmpeg', '-y', '-ss', str(mid), '-i', video_path, '-frames:v', '1', '-q:v', '3', keyframe_path],
            capture_output=True, timeout=30
        )
        shot['keyframe_path'] = keyframe_path if os.path.exists(keyframe_path) else None

    manifest_path = os.path.join(shot_dir, 'shots.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(shots, f, ensure_ascii=False)
    return shots
