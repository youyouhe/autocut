# memory_store.py — 运行时内存缓存
# 1. 素材分析结果 → asset_store.py 落 SQLite (长期管理 + SQL 检索)
# 2. ≤50MB 的视频文件 → 按需载入内存 (前端预览/分析零磁盘 IO)
import os, json, hashlib, time, threading

import config
import asset_store

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = config.CACHE_DIR
VIDEO_CACHE_DIR = config.GUI_UPLOAD_DIR

MAX_VIDEO_RAM = 50 * 1024 * 1024   # 50MB 以下载入内存
MAX_TOTAL_RAM = 500 * 1024 * 1024 # 总视频内存上限 500MB

_lock = threading.Lock()
_video_store = {}         # {path: {bytes, name, size, time}} — 视频字节
_video_ram_used = 0       # 当前视频内存用量


def _path_hash(path):
    return hashlib.md5(path.encode('utf-8')).hexdigest()


# ============================================================ 分析元数据 (SQLite)

def load_all_analysis():
    """启动时返回已入库的分析记录数 (数据在 SQLite, 无需全量载内存)。"""
    return asset_store.count()


def get_analysis(path):
    """按路径查分析结果 (SQLite 主键查询)。"""
    return asset_store.get(path)


def has_analysis(path):
    return asset_store.has(path)


def save_analysis(path, result):
    """写入 SQLite (asset_store.upsert)。"""
    asset_store.upsert(path, result)


def invalidate_analysis(path):
    """文件内容变了 (如去除音轨) 时清掉旧分析记录, 否则库里的老结果
    (可能包含已经不存在的音轨对应的 ASR 转录) 会被继续当作最新结果返回。"""
    global _video_ram_used
    asset_store.delete(path)
    with _lock:
        old = _video_store.pop(path, None)
        if old:
            _video_ram_used -= old['size']


def list_all_analysis():
    """返回所有已入库的分析元数据摘要 (兼容旧字段)。"""
    out = []
    for a in asset_store.list_all():
        out.append({
            'hash': _path_hash(a['path']),
            'path': a['path'],
            'time': a.get('time', 0),
            'has_visual': a.get('has_visual', False),
            'has_audio': a.get('has_audio', False),
            'duration': a.get('duration', 0),
        })
    return out


# ============================================================ 视频内存缓存

def maybe_load_video(path):
    """如果视频 ≤50MB，载入内存。返回 True 如果已在/已载入内存。"""
    global _video_ram_used

    # 已在内存 (读检查加锁, 避免与 evict 竞态)
    with _lock:
        if path in _video_store:
            return True

    try:
        size = os.path.getsize(path)
    except OSError:
        return False

    if size > MAX_VIDEO_RAM:
        return False  # 超过 50MB 不载入

    # 磁盘 IO 放锁外 (耗时), 读完再入锁写
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return False

    with _lock:
        # double-check: 可能其他线程已载入
        if path in _video_store:
            return True
        # 腾出空间 (可能需淘汰多个)
        while _video_ram_used + size > MAX_TOTAL_RAM and _video_store:
            _evict_oldest_locked()
        if _video_ram_used + size > MAX_TOTAL_RAM:
            return False
        _video_store[path] = {
            'data': data,
            'size': size,
            'name': os.path.basename(path),
            'time': time.time(),
        }
        _video_ram_used += size
    return True


def get_video_bytes(path):
    """从内存取视频字节 (None = 不在内存)"""
    with _lock:
        entry = _video_store.get(path)
        if entry:
            entry['time'] = time.time()  # LRU 访问刷新
        return entry['data'] if entry else None


def get_video_info(path):
    """取视频内存缓存信息"""
    with _lock:
        entry = _video_store.get(path)
        if not entry:
            return None
        return {'name': entry['name'], 'size': entry['size'], 'cached': True}


def _evict_oldest_locked():
    """淘汰最久未访问的视频 (调用方须持有 _lock)"""
    global _video_ram_used
    if not _video_store:
        return
    oldest_key = min(_video_store.items(), key=lambda x: x[1]['time'])[0]
    freed = _video_store[oldest_key]['size']
    del _video_store[oldest_key]
    _video_ram_used -= freed


def video_cache_stats():
    """视频内存缓存统计"""
    with _lock:
        return {
            'videos_in_ram': len(_video_store),
            'ram_used_mb': round(_video_ram_used / 1024 / 1024, 1),
            'ram_limit_mb': round(MAX_TOTAL_RAM / 1024 / 1024, 0),
            'max_single_mb': round(MAX_VIDEO_RAM / 1024 / 1024, 0),
        }


# ============================================================ 启动

# 模块加载时报告已入库分析记录数
_count = load_all_analysis()
print(f'[memory_store] SQLite 已入库 {_count} 条分析记录', flush=True)
