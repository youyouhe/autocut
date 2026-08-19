# config.py — 集中配置 (环境变量 / .env 驱动)
# 所有路径/端口/渲染参数的唯一来源, 其他模块一律 from config import ...
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


def _int(name, default):
    try:
        return int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        return default


# ============================================================ 服务
RENDER_SERVER_HOST = os.environ.get('RENDER_SERVER_HOST', '0.0.0.0')
RENDER_SERVER_PORT = _int('RENDER_SERVER_PORT', 9010)
# 内部自调用基址 (chat 工具 → 自身 REST 端点)
API_BASE = os.environ.get('API_BASE', f'http://127.0.0.1:{RENDER_SERVER_PORT}')
# BrowserSkill CLI (发布视频到视频号/抖音/小红书用). 默认装在 ~/.local/bin; 服务进程的 PATH
# 不一定包含它, 显式落绝对路径兜底.
_bsk_default = os.path.join(os.path.expanduser('~'), '.local', 'bin', 'bsk.exe')
BSK_BIN = os.environ.get('BSK_BIN', _bsk_default if os.path.isfile(_bsk_default) else 'bsk')
# CORS 允许来源 (逗号分隔)。前端由本服务同源托管, 仅 Vite dev 需要。留空 = 不添加 CORS 头
CORS_ALLOW_ORIGINS = [o.strip() for o in os.environ.get(
    'CORS_ALLOW_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173').split(',') if o.strip()]

# ============================================================ 剪映 / 系统路径 (Windows)
USERPROFILE = os.environ.get('USERPROFILE') or os.path.expanduser('~')
# 剪映安装目录 (Apps/<version>/JianyingPro.exe)
JY_APP_BASE = os.environ.get('JY_APP_BASE', os.path.join(
    USERPROFILE, 'AppData', 'Local', 'JianyingPro', 'Apps'))
# 剪映草稿根目录
DRAFT_ROOT = os.environ.get('JY_DRAFT_ROOT', os.path.join(
    USERPROFILE, 'AppData', 'Local', 'JianyingPro', 'User Data',
    'Projects', 'com.lveditor.draft'))
# 剪映默认导出目录
VIDEOS_DIR = os.environ.get('VIDEOS_DIR', os.path.join(USERPROFILE, 'Videos'))

# ============================================================ 工作目录
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', os.path.join(HERE, 'render_uploads'))
GUI_UPLOAD_DIR = os.path.join(HERE, 'gui_uploads')
CACHE_DIR = os.environ.get('ANALYSIS_CACHE_DIR', os.path.join(HERE, 'analysis_cache'))
STATIC_DIR = os.path.join(HERE, 'static')
TEMPLATES_DIR = os.path.join(HERE, 'templates')
CALIB_FILE = os.environ.get('CALIB_FILE', os.path.join(HERE, 'calib.json'))

for _d in (UPLOAD_DIR, GUI_UPLOAD_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ============================================================ 渲染
RENDER_TIMEOUT = _int('RENDER_TIMEOUT', 600)          # 单任务渲染子进程超时 (秒)
TASK_TTL = _int('TASK_TTL', 86400)                    # 任务记录保留时长 (秒)
# 隔离桌面列表 (逗号分隔), 每个桌面一个并行渲染 worker
DESKTOP_NAMES = [d.strip() for d in os.environ.get(
    'DESKTOP_NAMES', 'JYRender_0').split(',') if d.strip()]
# 校准坐标预设窗口尺寸 (resize 到此尺寸保证坐标准)
PRESET_W = _int('PRESET_W', 1280)
PRESET_H = _int('PRESET_H', 720)

# ============================================================ ASR
# 'remote' = 调用 ASR_ENDPOINT (默认, 音频上传第三方); 'local' = 本地 faster-whisper
ASR_BACKEND = os.environ.get('ASR_BACKEND', 'remote').lower()
ASR_LOCAL_MODEL = os.environ.get('ASR_LOCAL_MODEL', 'small')  # tiny/base/small/medium
ASR_LOCAL_DEVICE = os.environ.get('ASR_LOCAL_DEVICE', 'cpu')

# ============================================================ 自动感知
# 素材落盘后是否自动后台调 VLM 分析 (消耗 token, 默认关闭)
AUTO_PERCEIVE = os.environ.get('AUTO_PERCEIVE', '0') == '1'

# ============================================================ 安全
# /api/video/serve 等文件下发接口允许访问的根目录
ALLOWED_SERVE_DIRS = [UPLOAD_DIR, GUI_UPLOAD_DIR, VIDEOS_DIR, DRAFT_ROOT, CACHE_DIR]


def is_within(base, path):
    """path (realpath 后) 是否在 base 目录内. 防路径穿越."""
    try:
        base_r = os.path.realpath(base)
        path_r = os.path.realpath(path)
        return path_r == base_r or path_r.startswith(base_r + os.sep)
    except (OSError, ValueError):
        return False


def is_allowed_path(path):
    """文件服务白名单校验"""
    if not path:
        return False
    return any(is_within(d, path) for d in ALLOWED_SERVE_DIRS)


def safe_folder_name(name):
    """校验草稿文件夹名 (防 ../ 穿越). 合法返回 True"""
    if not name:
        return False
    if name in ('.', '..'):
        return False
    if '/' in name or '\\' in name:
        return False
    return os.path.basename(name) == name


def safe_zip_extract(zf, dest_dir):
    """zip-slip 防护的 extractall. 拒绝绝对路径/.. 成员."""
    dest_r = os.path.realpath(dest_dir)
    for info in zf.infolist():
        member = info.filename
        if member.startswith('/') or member.startswith('\\'):
            raise ValueError(f'unsafe zip member (absolute): {member}')
        target = os.path.realpath(os.path.join(dest_r, member))
        if target != dest_r and not target.startswith(dest_r + os.sep):
            raise ValueError(f'unsafe zip member (traversal): {member}')
    zf.extractall(dest_dir)


def find_jianying_exe():
    """找最新版本的 JianyingPro.exe. 返回路径或 None"""
    try:
        for d in sorted(os.listdir(JY_APP_BASE), reverse=True):
            p = os.path.join(JY_APP_BASE, d, 'JianyingPro.exe')
            if os.path.exists(p):
                return p
    except OSError:
        pass
    return None
