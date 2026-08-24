# settings_store.py — 运行期配置读写 (LLM/ASR 密钥等), 供前端 Settings 页面用
# 落盘到 .env (行级更新, 不打乱既有注释/顺序); 写入的同时热更新 os.environ 与
# 已 import 的模块全局变量 (perceive.py/config.py), 使改动无需重启进程立即生效.
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, '.env')

# (key, label, secret, default) — secret 字段展示时脱敏, 保存时允许留空(=不改动)
SETTINGS_SCHEMA = [
    {'key': 'QWEN_API_KEY', 'label': 'Qwen API Key', 'secret': True, 'group': 'llm'},
    {'key': 'QWEN_BASE_URL', 'label': 'Qwen Base URL', 'secret': False, 'group': 'llm',
     'default': 'https://dashscope.aliyuncs.com/compatible-mode/v1'},
    {'key': 'QWEN_MODEL', 'label': 'Qwen Model', 'secret': False, 'group': 'llm', 'default': 'qwen3.7-plus'},
    # DeepSeek (聊天 agent 优先使用; 配了 Key 即生效, 未配回退 Qwen). 独立分组.
    {'key': 'DEEPSEEK_API_KEY', 'label': 'DeepSeek API Key (聊天 agent, 留空则用 Qwen)',
     'secret': True, 'group': 'deepseek'},
    {'key': 'DEEPSEEK_BASE_URL', 'label': 'DeepSeek Base URL', 'secret': False, 'group': 'deepseek',
     'default': 'https://api.deepseek.com'},
    {'key': 'DEEPSEEK_MODEL', 'label': 'DeepSeek Model (聊天)', 'secret': False, 'group': 'deepseek',
     'default': 'deepseek-v4-flash'},
    {'key': 'ASR_ENDPOINT', 'label': 'ASR Endpoint', 'secret': False, 'group': 'asr',
     'default': 'https://asr.smartbid.site/inference'},
    {'key': 'ASR_API_KEY', 'label': 'ASR API Key', 'secret': True, 'group': 'asr'},
    {'key': 'PREFER_ASR', 'label': '优先使用 ASR 判断视频内容 (ASR 不可用/静音时才用 VLM 看画面)',
     'secret': False, 'group': 'analysis', 'type': 'bool', 'default': '1'},
    # 感知分析 (VLM) 改走 DeepSeek 视觉模型 (需已配 DeepSeek Key)
    {'key': 'PERCEIVE_USE_DEEPSEEK', 'label': '感知分析用 DeepSeek (Qwen 关闭/额度用尽时勾选; 需 DeepSeek Key)',
     'secret': False, 'group': 'analysis', 'type': 'bool', 'default': '0'},
    # FFmpeg 可执行文件路径. pythonw 后台启动不继承终端 PATH, 若 ffmpeg 不在系统 PATH 里,
    # 所有 subprocess 调用(去音/封面/分镜/感知抽帧)都会 WinError 2. 留空 = 走 PATH 自动查找.
    {'key': 'FFMPEG_PATH', 'label': 'FFmpeg 可执行文件路径 (留空则用系统 PATH)',
     'secret': False, 'group': 'tools', 'default': ''},
    # LocalSend 多播出口 IP. 多网卡/VPN 环境自动探测可能选错, 显式指定局域网 IP
    {'key': 'LOCALSEND_IF_IP', 'label': 'LocalSend 绑定 IP (留空自动探测局域网网卡)',
     'secret': False, 'group': 'tools', 'default': ''},
    # 渲染引擎选择: ffmpeg 本地直渲(快/确定性) vs jianying 剪映节点(特效保真)
    {'key': 'RENDER_ENGINE_PREFER', 'label': '渲染引擎优先',
     'secret': False, 'group': 'tools', 'default': 'ffmpeg',
     'type': 'select', 'options': [
         {'value': 'ffmpeg', 'label': 'ffmpeg 本地直渲 (快, 常规草稿推荐)'},
         {'value': 'jianying', 'label': 'jianying 剪映节点 (特效保真)'}]},
]

_KEYS = [s['key'] for s in SETTINGS_SCHEMA]
_BOOL_KEYS = {s['key'] for s in SETTINGS_SCHEMA if s.get('type') == 'bool'}
_SCHEMA_BY_KEY = {s['key']: s for s in SETTINGS_SCHEMA}


def effective_value(key):
    """某个 key 当前真实生效值 (os.environ 优先, 否则 schema default; 不脱敏).
    供内部使用(如测试连接), 别直接暴露给前端 —— secret 字段是明文."""
    s = _SCHEMA_BY_KEY.get(key)
    if not s:
        return ''
    return os.environ.get(key, '') or s.get('default', '')


def _mask(v):
    if not v:
        return ''
    if len(v) <= 4:
        return '*' * len(v)
    return '*' * (len(v) - 4) + v[-4:]


def get_settings():
    """当前生效值 (进程 os.environ), secret 字段脱敏返回 + configured 标记."""
    out = []
    for s in SETTINGS_SCHEMA:
        val = os.environ.get(s['key'], '') or s.get('default', '')
        is_bool = s.get('type') == 'bool'
        out.append({
            'key': s['key'], 'label': s['label'], 'secret': s['secret'], 'group': s['group'],
            'type': s.get('type', 'secret' if s['secret'] else 'text'),
            'options': s.get('options'),
            'value': (val == '1') if is_bool else (_mask(val) if s['secret'] else val),
            'configured': bool(val),
        })
    return out


def _read_env_lines():
    if not os.path.exists(ENV_PATH):
        return []
    with open(ENV_PATH, encoding='utf-8') as f:
        return f.readlines()


def _write_env_lines(lines):
    with open(ENV_PATH, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def save_settings(values):
    """更新 .env (行级替换/追加) + 热更新 os.environ 与已加载模块的全局变量.
    values 里值为空字符串的 secret 字段视为'不修改', 跳过(避免前端脱敏回显覆盖真值)."""
    updates = {}
    for s in SETTINGS_SCHEMA:
        k = s['key']
        if k not in values:
            continue
        v = values[k]
        if v is None:
            continue
        if k in _BOOL_KEYS:
            updates[k] = '1' if v in (True, '1', 'true', 'True', 1) else '0'
            continue
        v = str(v).strip()
        if s['secret'] and v == '':
            continue  # 留空 = 不改动密钥
        updates[k] = v

    if not updates:
        return get_settings()

    lines = _read_env_lines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r'^([A-Z_][A-Z0-9_]*)=', line)
        if m and m.group(1) in updates:
            k = m.group(1)
            lines[i] = f'{k}={updates[k]}\n'
            seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f'{k}={v}\n')
    _write_env_lines(lines)

    for k, v in updates.items():
        os.environ[k] = v
    _hot_patch(updates)
    return get_settings()


def _hot_patch(updates):
    """把新值同步进已 import 的模块全局变量, 免得改完还要重启进程才生效."""
    perceive = sys.modules.get('perceive')
    if perceive:
        if 'QWEN_API_KEY' in updates: perceive.QWEN_API_KEY = updates['QWEN_API_KEY']
        if 'QWEN_BASE_URL' in updates: perceive.QWEN_BASE_URL = updates['QWEN_BASE_URL']
        if 'QWEN_MODEL' in updates: perceive.QWEN_MODEL = updates['QWEN_MODEL']
        if 'DEEPSEEK_API_KEY' in updates: perceive.DEEPSEEK_API_KEY = updates['DEEPSEEK_API_KEY']
        if 'DEEPSEEK_BASE_URL' in updates: perceive.DEEPSEEK_BASE_URL = updates['DEEPSEEK_BASE_URL']
        if 'DEEPSEEK_MODEL' in updates: perceive.DEEPSEEK_MODEL = updates['DEEPSEEK_MODEL']
        if 'PERCEIVE_USE_DEEPSEEK' in updates:
            perceive.PERCEIVE_USE_DEEPSEEK = (updates['PERCEIVE_USE_DEEPSEEK'] == '1')
        if 'DEEPSEEK_VISION_MODEL' in updates:
            perceive.DEEPSEEK_VISION_MODEL = updates['DEEPSEEK_VISION_MODEL']
        if 'ASR_ENDPOINT' in updates: perceive.ASR_ENDPOINT = updates['ASR_ENDPOINT']
        if 'ASR_API_KEY' in updates: perceive.ASR_API_KEY = updates['ASR_API_KEY']
        if 'PREFER_ASR' in updates: perceive.PREFER_ASR = (updates['PREFER_ASR'] == '1')
    cfg = sys.modules.get('config')
    if cfg and 'RENDER_ENGINE_PREFER' in updates:
        cfg.RENDER_ENGINE_PREFER = updates['RENDER_ENGINE_PREFER'].lower()
    config = sys.modules.get('config')
    if config:
        if 'ASR_ENDPOINT' in updates: config.ASR_ENDPOINT = updates.get('ASR_ENDPOINT', getattr(config, 'ASR_ENDPOINT', None))

    # FFmpeg 路径改动: 刷新 render_server 的 ffmpeg 解析缓存, 让后续去音/封面/分镜等
    # 调用立即用上新路径 (无需重启). 用 sys.modules 取已加载模块, 绝不用 `import` ——
    # 首次请求后 Flask 已锁定路由表, 再 import 会重跑模块顶层 @app.route 装饰器并抛
    # AssertionError "route can no longer be called".
    if 'FFMPEG_PATH' in updates:
        rs = sys.modules.get('render_server')
        if rs is not None and hasattr(rs, 'resolve_ffmpeg'):
            rs.resolve_ffmpeg(refresh=True)


def test_llm(api_key, base_url, model):
    """真实发一次最小 chat completion, 验证 key/base_url/model 是否可用."""
    if not api_key:
        return {'ok': False, 'error': '缺少 API Key'}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        r = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': 'ping'}],
            max_tokens=1,
        )
        return {'ok': True, 'model': r.model}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:300]}


def test_ffmpeg(path):
    """验证 ffmpeg 可执行文件能跑起来 (ffmpeg -version), 顺便回填版本号.
    path 为空时走 resolve_ffmpeg 的自动查找 (默认候选目录 / 系统 PATH)."""
    import subprocess
    try:
        # 优先用用户填的路径; 留空则交给 ffmpeg_util.resolve_ffmpeg 自动找
        exe = (path or '').strip()
        if not exe:
            try:
                from ffmpeg_util import resolve_ffmpeg as _resolve
                exe = _resolve()
            except Exception:
                from shutil import which
                exe = which('ffmpeg') or 'ffmpeg'
        if not exe:
            return {'ok': False, 'error': '未找到 ffmpeg (请安装到系统 PATH, 或在上方填入完整路径; Windows 也可放 C:\\ffmpeg\\bin)'}
        # 直接调用户填的路径; 若是目录则补可执行名
        if os.path.isdir(exe):
            exe = os.path.join(exe, 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
        r = subprocess.run([exe, '-version'], capture_output=True, timeout=15)
        if r.returncode != 0:
            return {'ok': False, 'error': f'ffmpeg 退出码 {r.returncode}: {r.stderr.decode("utf-8","ignore")[:300]}'}
        first_line = r.stdout.decode('utf-8', 'ignore').splitlines()[0] if r.stdout else ''
        return {'ok': True, 'model': exe, 'status': 200, 'detail': first_line}
    except FileNotFoundError:
        return {'ok': False, 'error': '找不到该文件 (WinError 2): 路径不对或文件不存在'}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:300]}


def test_asr(endpoint, api_key):
    """发一个极小的空音频请求, 只验证鉴权/连通性 (401/403=key 错误; 其余状态码视为鉴权通过)."""
    if not endpoint:
        return {'ok': False, 'error': '缺少 Endpoint'}
    try:
        import requests
        resp = requests.post(
            endpoint,
            headers={'X-API-Key': api_key or ''},
            files={'file': ('probe.mp3', b'\x00', 'audio/mpeg')},
            timeout=15,
        )
        if resp.status_code in (401, 403):
            return {'ok': False, 'error': f'鉴权失败 (HTTP {resp.status_code})'}
        return {'ok': True, 'status': resp.status_code}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:300]}
