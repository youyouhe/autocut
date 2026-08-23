# user_render_store.py — per-user 自定义 render 节点配置
#
# 每个用户可配置自己的 render_service (URL + X-Render-Token): 渲染时优先走自己的 CapCut,
# 提交失败或未配置时回退公共节点 (config.RENDER_SERVICE_URL / config.RENDER_SERVICE_TOKEN)。
#
# 仿 main_video_store.py 的 per-user JSON 模式 (每个用户一个 json, 按用户懒创建目录).
# 与 settings_store.py 是不同关注点 —— 后者是 process-global、admin-only、写 .env;
# 这里是 per-user、用户自助、写独立 json, 故不合并。
import os, json

import config

STORE_DIR = config.USER_RENDER_DIR


def _store_path(user_id):
    """per-user 配置文件路径, 按需创建用户子目录。"""
    d = os.path.join(STORE_DIR, user_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'render_node.json')


def get(user_id):
    """返回用户配置的 render 节点 {'url','token'}, 未配置返回 None。"""
    if not user_id:
        return None
    sp = _store_path(user_id)
    if not os.path.exists(sp):
        return None
    try:
        with open(sp, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get('url'):
            return None
        return {'url': data.get('url', ''), 'token': data.get('token', '')}
    except Exception:
        return None


def save(user_id, url, token):
    """保存用户 render 节点配置。

    url 始终覆盖 (空串 = 清空 url, 等价于 clear); token 为空串时保留已存 token
    (与 settings_store「留空则不修改」一致, 避免脱敏回填把真值擦掉)。"""
    if not user_id:
        return None
    sp = _store_path(user_id)
    # token 留空 = 不修改 (保留已存值)
    if not token:
        existing = get(user_id)
        token = existing['token'] if existing else ''
    url = (url or '').strip().rstrip('/')
    if not url:
        # url 清空 = 恢复走公共节点
        clear(user_id)
        return None
    data = {'url': url, 'token': token or ''}
    with open(sp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def get_effective(user_id):
    """返回 (url, token): 用户已配置则用用户的, 否则回退公共节点配置。

    返回的 token 永远是真实值 (供转发头用), 不脱敏 —— 脱敏只在 web GET 接口做。"""
    cfg = get(user_id)
    if cfg and cfg.get('url'):
        return cfg['url'], cfg.get('token', '')
    # 未配置 / 配置无效 → 公共节点兜底
    return config.RENDER_SERVICE_URL, config.RENDER_SERVICE_TOKEN


def clear(user_id):
    """清空用户配置 (恢复走公共节点)。"""
    if not user_id:
        return
    sp = _store_path(user_id)
    if os.path.exists(sp):
        try:
            os.remove(sp)
        except Exception:
            pass
