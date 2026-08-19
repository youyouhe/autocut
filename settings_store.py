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
    {'key': 'ASR_ENDPOINT', 'label': 'ASR Endpoint', 'secret': False, 'group': 'asr',
     'default': 'https://asr.smartbid.site/inference'},
    {'key': 'ASR_API_KEY', 'label': 'ASR API Key', 'secret': True, 'group': 'asr'},
    {'key': 'PREFER_ASR', 'label': '优先使用 ASR 判断视频内容 (ASR 不可用/静音时才用 VLM 看画面)',
     'secret': False, 'group': 'analysis', 'type': 'bool', 'default': '1'},
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
        if 'ASR_ENDPOINT' in updates: perceive.ASR_ENDPOINT = updates['ASR_ENDPOINT']
        if 'ASR_API_KEY' in updates: perceive.ASR_API_KEY = updates['ASR_API_KEY']
        if 'PREFER_ASR' in updates: perceive.PREFER_ASR = (updates['PREFER_ASR'] == '1')
    config = sys.modules.get('config')
    if config:
        if 'ASR_ENDPOINT' in updates: config.ASR_ENDPOINT = updates.get('ASR_ENDPOINT', getattr(config, 'ASR_ENDPOINT', None))


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
