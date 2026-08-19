# main_video_store.py — 当前"主视频"指针
#
# 主视频(每次最新录制的那条, 会不断被替换) 和素材库(长期存在、反复复用的补充素材)
# 要分开管理, 但两者物理上都还是 render_uploads/ 里的普通文件 —— 区分只是一个指针:
# 谁被标记为"当前主视频"就是, 换了新的之后旧文件原地不动, 自然就变回普通素材库的
# 一条(不用挪文件/建目录, 换指针一步到位, 也不会产生"降级搬家"的额外磁盘操作)。
import os, json, time, glob, subprocess

import config

STORE_PATH = os.path.join(config.HERE, 'main_video.json')
POSTER_GLOB = os.path.join(config.UPLOAD_DIR, '_main_video_poster_*.jpg')


def get():
    """返回当前主视频 {'path', 'name', 'set_at', 'poster_path'}, 没设置过或指向的文件
    已经不存在返回 None。"""
    if not os.path.exists(STORE_PATH):
        return None
    try:
        with open(STORE_PATH, encoding='utf-8') as f:
            data = json.load(f)
        path = data.get('path')
        if not path or not os.path.exists(path):
            return None
        return data
    except Exception:
        return None


def _clear_posters():
    for f in glob.glob(POSTER_GLOB):
        try: os.remove(f)
        except Exception: pass


def set(path):
    """标记为当前主视频。旧的主视频不需要做任何事 —— 指针一移开它就自动是普通素材了。

    顺带截一张海报帧: <video preload="metadata"> 在很多浏览器里不会自动画出第一帧,
    只有点了播放才有画面, 不给 poster 就是纯黑一块。海报文件名带时间戳(不是固定的
    _main_video_poster.jpg) —— 固定文件名换内容不换 URL, 会被浏览器 Cache-Control
    缓存的旧图顶替(实测出现过, 单靠 query string 版本号加参数在有些缓存路径下也不
    保险), 文件名真的不一样才能保证万无一失拿到新图。"""
    ts = int(time.time() * 1000)
    data = {'path': path, 'name': os.path.basename(path), 'set_at': ts / 1000}
    _clear_posters()
    poster_path = os.path.join(config.UPLOAD_DIR, f'_main_video_poster_{ts}.jpg')
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-ss', '1', '-i', path, '-frames:v', '1', '-q:v', '3', poster_path],
            capture_output=True, timeout=30
        )
        if os.path.exists(poster_path) and os.path.getsize(poster_path) > 0:
            data['poster_path'] = poster_path
    except Exception:
        pass
    with open(STORE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def clear():
    if os.path.exists(STORE_PATH):
        os.remove(STORE_PATH)
    _clear_posters()
