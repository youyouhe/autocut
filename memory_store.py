# memory_store.py — 运行时内存缓存
# 1. analysis_cache/*.json → 启动时全量载入内存 (毫秒级查询)
# 2. ≤50MB 的视频文件 → 按需载入内存 (前端预览/分析零磁盘 IO)
import os, json, hashlib, time, threading

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, 'analysis_cache')
VIDEO_CACHE_DIR = os.path.join(HERE, 'gui_uploads')

MAX_VIDEO_RAM = 50 * 1024 * 1024   # 50MB 以下载入内存
MAX_TOTAL_RAM = 500 * 1024 * 1024 # 总视频内存上限 500MB

_lock = threading.Lock()
_analysis_store = {}      # {path_hash: {result dict}}  — 分析元数据
_video_store = {}         # {path: {bytes, name, size, time}} — 视频字节
_video_ram_used = 0       # 当前视频内存用量


def _path_hash(path):
    return hashlib.md5(path.encode('utf-8')).hexdigest()


# ============================================================ 分析元数据

def load_all_analysis():
    """启动时全量载入 analysis_cache/ 到内存"""
    global _analysis_store
    count = 0
    if not os.path.isdir(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
        return 0
    for f in os.listdir(CACHE_DIR):
        if not f.endswith('.json'):
            continue
        try:
            data = json.load(open(os.path.join(CACHE_DIR, f), encoding='utf-8'))
            key = f.replace('.json', '')
            _analysis_store[key] = data
            count += 1
        except:
            pass
    return count


def get_analysis(path):
    """内存查询分析结果 (O(1) dict lookup)"""
    return _analysis_store.get(_path_hash(path))


def has_analysis(path):
    return _path_hash(path) in _analysis_store


def save_analysis(path, result):
    """写入内存 + 落盘"""
    h = _path_hash(path)
    result = dict(result)
    result['_path'] = path
    result['_time'] = time.time()
    with _lock:
        _analysis_store[h] = result
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        json.dump(result, open(os.path.join(CACHE_DIR, f'{h}.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    except:
        pass


def list_all_analysis():
    """返回所有已缓存的元数据摘要"""
    out = []
    for h, data in _analysis_store.items():
        out.append({
            'hash': h,
            'path': data.get('_path', ''),
            'time': data.get('_time', 0),
            'has_visual': bool(data.get('visual_analysis')),
            'has_audio': bool(data.get('audio', {}).get('full_text')),
            'duration': data.get('meta', {}).get('duration', 0),
        })
    return out


# ============================================================ 视频内存缓存

def maybe_load_video(path):
    """如果视频 ≤50MB，载入内存。返回 True 如果已在/已载入内存。"""
    global _video_ram_used

    # 已在内存
    if path in _video_store:
        return True

    try:
        size = os.path.getsize(path)
    except:
        return False

    if size > MAX_VIDEO_RAM:
        return False  # 超过 50MB 不载入

    # 检查总内存上限
    if _video_ram_used + size > MAX_TOTAL_RAM:
        _evict_oldest_video()

    try:
        with open(path, 'rb') as f:
            data = f.read()
        with _lock:
            _video_store[path] = {
                'data': data,
                'size': size,
                'name': os.path.basename(path),
                'time': time.time(),
            }
            _video_ram_used += size
        return True
    except:
        return False


def get_video_bytes(path):
    """从内存取视频字节 (None = 不在内存)"""
    entry = _video_store.get(path)
    return entry['data'] if entry else None


def get_video_info(path):
    """取视频内存缓存信息"""
    entry = _video_store.get(path)
    if not entry:
        return None
    return {'name': entry['name'], 'size': entry['size'], 'cached': True}


def _evict_oldest_video():
    """淘汰最久未访问的视频，释放内存"""
    global _video_ram_used
    if not _video_store:
        return
    oldest = min(_video_store.items(), key=lambda x: x[1]['time'])
    with _lock:
        if oldest[0] in _video_store:
            freed = _video_store[oldest[0]]['size']
            del _video_store[oldest[0]]
            _video_ram_used -= freed


def video_cache_stats():
    """视频内存缓存统计"""
    return {
        'videos_in_ram': len(_video_store),
        'ram_used_mb': round(_video_ram_used / 1024 / 1024, 1),
        'ram_limit_mb': round(MAX_TOTAL_RAM / 1024 / 1024, 0),
        'max_single_mb': round(MAX_VIDEO_RAM / 1024 / 1024, 0),
    }


# ============================================================ 启动

# 模块加载时自动载入分析缓存
_count = load_all_analysis()
print(f'[memory_store] 载入 {_count} 条分析缓存', flush=True)
