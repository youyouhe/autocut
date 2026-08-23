# ffmpeg_util.py — ffmpeg 路径解析 (独立于 web 层)
#
# 从 render_server.py 抽出, 让 store 模块 (main_video_store 等) 不再依赖 web 层。
# 解析优先级: settings FFMPEG_PATH > 系统候选目录 > PATH (which).
# 跨平台: Windows 走 .exe 候选 + which; Linux 走 which('ffmpeg').
import os
import sys
import threading

_lock = threading.Lock()
_FFMPEG_RESOLVED = None  # None=未解析, 否则=解析结果 ('' 表示未找到走 PATH 兜底)


def _default_candidates():
    """系统级默认候选目录. Windows 走常见安装位置; 其他平台靠 PATH."""
    if sys.platform == 'win32':
        return [
            r'C:\ffmpeg\bin',
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'ffmpeg', 'bin'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ffmpeg', 'bin'),
        ]
    return ['/usr/bin', '/usr/local/bin', '/opt/ffmpeg/bin']


def resolve_ffmpeg(refresh=False):
    """返回 ffmpeg 可执行文件绝对路径; 找不到时返回 'ffmpeg' (交给系统 PATH).
    refresh=True 强制重新解析 (settings 改了 FFMPEG_PATH 后调用)."""
    global _FFMPEG_RESOLVED
    with _lock:
        if _FFMPEG_RESOLVED is not None and not refresh:
            return _FFMPEG_RESOLVED

        from shutil import which

        # 1. settings 里配置的 FFMPEG_PATH
        try:
            import settings_store
            configured = settings_store.effective_value('FFMPEG_PATH').strip()
        except Exception:
            configured = os.environ.get('FFMPEG_PATH', '').strip()

        exe_suffix = '.exe' if sys.platform == 'win32' else ''
        resolved = ''
        candidates = []
        if configured:
            if configured.lower().endswith('.exe') and os.path.isfile(configured):
                resolved = configured
                candidates.append(os.path.dirname(configured))
            elif os.path.isdir(configured):
                candidates.append(configured)

        # 2. 系统级默认目录兜底
        candidates.extend(_default_candidates())

        # 3. 沿候选目录找 ffmpeg; 同时信任系统 PATH
        for d in candidates:
            exe = os.path.join(d, 'ffmpeg' + exe_suffix)
            if os.path.isfile(exe):
                resolved = resolved or exe
                if d not in os.environ.get('PATH', ''):
                    os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
                break
        if not resolved:
            resolved = which('ffmpeg') or 'ffmpeg'

        _FFMPEG_RESOLVED = resolved
        return resolved


def ensure_resolved():
    """启动时调用: 解析一次 + 把目录补进 PATH."""
    try:
        resolve_ffmpeg()
    except Exception as e:
        print(f'[ffmpeg_util] resolve 失败: {e}', flush=True)
