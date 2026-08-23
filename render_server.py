# render_server.py — RESTful 渲染服务
# 接收 zip 草稿 → 真后台渲染 (--desktop) → 返回 mp4
# 融合 VectCutAPI 的编辑端点 (add_video/add_text/...) → 完整生产线
import os, sys, json, zipfile, threading, uuid, time, subprocess, shutil, secrets
import requests

from agents import Runner  # OpenAI Agents SDK (聊天 Agent 运行时)
_CHAT_LOCKS = {}  # conversation_id -> Lock: 同一会话串行, 防并发整体覆盖丢轮次
_CHAT_CANCELS = {}  # conversation_id -> threading.Event: 手动停止进行中的一轮
_CHAT_LOCK_TIMES = {}  # conversation_id -> 锁获取时间: 卡死检测 (10min 强制接管)

HERE = os.path.dirname(os.path.abspath(__file__))
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

# === 确保 ffmpeg 可被找到 (pythonw 启动时不继承交互终端的 PATH) ===
# 解析逻辑统一在 ffmpeg_util (跨平台: Windows 走 .exe 候选目录, Linux 走
# /usr/bin 等候选 + which). 这里保留同名导出, 兼容旧引用.
from ffmpeg_util import resolve_ffmpeg

resolve_ffmpeg()  # 启动时解析一次 + 把目录补进 PATH

import config
from flask import Flask, request, jsonify, send_file, send_from_directory, redirect, session
from werkzeug.utils import secure_filename
from functools import wraps

import task_store
import user_store
import user_render_store

# 融合 VectCutAPI 编辑端点 (create_draft/add_video/add_text/.../save_draft)
VC_DIR = os.path.join(HERE, 'VectCutAPI')
sys.path.insert(0, VC_DIR)
try:
    from capcut_server import app  # 复用 VectCutAPI 的所有编辑路由
    FUSED_VC = True
    print('[fusion] VectCutAPI 编辑端点已融合 (%d routes)' % len(list(app.url_map.iter_rules())))
except Exception as e:
    app = Flask(__name__)  # 独立模式 (仅渲染)
    FUSED_VC = False
    print('[fusion] VectCutAPI import 失败 (%s), 仅渲染模式' % repr(e)[:100])

# === 路径 ===
UPLOAD_DIR = config.UPLOAD_DIR
DRAFT_ROOT = config.DRAFT_ROOT
VIDEOS = config.VIDEOS_DIR

# === Session / 认证 ===
# 生产环境在 .env 设固定 SECRET_KEY; 未设则每次重启随机 (所有会话失效, 仅开发可接受)
app.secret_key = config.SECRET_KEY or secrets.token_hex(32)
# Cookie 安全: 生产环境 (非 localhost) 自动 Secure; SameSite=Lax 防跨站
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=config.RENDER_SERVER_HOST not in ('127.0.0.1', 'localhost', '0.0.0.0'),
    PERMANENT_SESSION_LIFETIME=86400 * 7,  # 7 天
)


def _is_internal_call():
    """内部自调用 (agent 工具 → config.API_BASE HTTP 自调) 旁路: 携带 X-Internal-Token 且匹配。
    这些调用发生在 generate() 里, 已有租户上下文 (通过 _user_id 透传), 无需 session。"""
    tok = request.headers.get('X-Internal-Token', '')
    return bool(tok) and tok == config.INTERNAL_TOKEN


def current_user_id():
    """取当前请求的 user_id: 优先 session['uid']; 其次内部自调用透传的 _user_id; 最后 None。"""
    uid = session.get('uid')
    if uid:
        return uid
    if _is_internal_call():
        # 内部自调用: generate() 在 payload/query 里带 _user_id 透传租户上下文
        uid = (request.json or {}).get('_user_id') if request.is_json else None
        if not uid:
            uid = request.args.get('_user_id') or request.form.get('_user_id')
        return uid
    return None


def current_user():
    """返回当前用户完整 dict (含 is_admin), 未登录返回 None。"""
    uid = current_user_id()
    return user_store.get(uid) if uid else None


def _draft_tenant_prefix(uid):
    """草稿命名空间前缀 (与 VectCutAPI/create_draft.py 的 _user_prefix 一致): u<uid[:8]>_。
    uid=None 返回空串 (legacy 草稿无前缀)。"""
    return ('u' + str(uid)[:8] + '_') if uid else ''


def _draft_owned(folder, uid=None, me=None):
    """判断草稿文件夹名/ID 是否归当前用户所有 (多租户隔离)。
    admin 可见/可操作一切; 无 uid (旧数据/无租户上下文) 放行; 其余按 u<uid8>_ 前缀匹配。
    无前缀的 legacy/剪映原生草稿对普通用户不可见 (仅 admin)。"""
    if me is None:
        me = current_user()
    if me and me.get('is_admin'):
        return True
    if not uid:
        return True
    return (folder or '').startswith(_draft_tenant_prefix(uid))


def login_required(f):
    """要求登录。内部自调用 (X-Internal-Token) 也放行 (已透传 _user_id)。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if current_user_id():
            return f(*args, **kwargs)
        return jsonify({'error': '未登录或会话已失效', 'code': 'UNAUTHORIZED'}), 401
    return wrapper


def admin_required(f):
    """要求 admin。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = current_user()
        if u and u.get('is_admin'):
            return f(*args, **kwargs)
        if u:
            return jsonify({'error': '需要管理员权限', 'code': 'FORBIDDEN'}), 403
        return jsonify({'error': '未登录或会话已失效', 'code': 'UNAUTHORIZED'}), 401
    return wrapper


# === 全局登录门 (兜底所有未单独加 @login_required 的业务路由, 含融合的 capcut 32 路由) ===
# 放行: OPTIONS 预检 / /health / /api/auth/* / 静态 SPA。内部自调用 (X-Internal-Token) 放行。
# 其余一律要求登录。这样 capcut_server 的 32 个草稿编辑端点无需逐个改装饰器即受保护。
_PUBLIC_PREFIXES = ('/health', '/api/auth/', '/static/')


@app.before_request
def _global_login_gate():
    # 预检放行
    if request.method == 'OPTIONS':
        return None
    path = request.path
    # 健康检查 / 认证端点 / 静态资源放行
    if path == '/health' or path.startswith('/api/auth/') or path.startswith('/static/'):
        return None
    # 渲染节点回调 (心跳/事件): 自带 X-Render-Token 认证 (见 _render_node_auth), 不走 session
    if path.startswith('/internal/render-'):
        return None
    # 根路径与无扩展名 catch-all (SPA 入口 / React Router 路径) 放行, 让前端加载
    if path == '/' or (path == '/index.html'):
        return None
    # 带扩展名的静态文件 (js/css/png...) 由 serve_vue catch-all 处理, 放行
    _, ext = os.path.splitext(path)
    if ext and ext.lower() in ('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg',
                               '.ico', '.woff', '.woff2', '.ttf', '.eot', '.map', '.json'):
        return None
    # 内部自调用 (已带 _user_id 透传租户上下文) 放行
    if _is_internal_call():
        return None
    # 其余业务路由要求登录
    if not current_user_id():
        return jsonify({'error': '未登录或会话已失效', 'code': 'UNAUTHORIZED'}), 401
    return None


# 草稿编辑端点 (VectCutAPI 融合路由): 请求体带 draft_id, 做租户归属校验, 防止跨用户改/删草稿。
# 以 404 回绝 (不泄露草稿是否存在)。无 draft_id (新建/无目标草稿) 的请求跳过校验。
_DRAFT_ID_BODY_ROUTES = {
    '/add_video', '/add_audio', '/add_subtitle', '/add_text', '/add_image',
    '/add_video_keyframe', '/add_effect', '/add_sticker',
    '/query_script', '/query_draft', '/delete_segment', '/delete_track',
    '/delete_empty_tracks', '/save_draft', '/generate_draft_url',
}


@app.before_request
def _draft_ownership_gate():
    if request.path not in _DRAFT_ID_BODY_ROUTES:
        return None
    if not request.is_json:
        return None
    try:
        data = request.get_json() or {}
    except Exception:
        return None
    did = data.get('draft_id')
    if not did:
        return None
    uid = current_user_id()
    if not uid:
        return None  # 未认证由登录门兜底
    if not _draft_owned(did, uid):
        return jsonify({'success': False, 'error': 'draft not found', 'code': 'NOT_FOUND'}), 404
    return None


# === 渲染任务转发 (Render 服务独立后) ===
# Render 抽成独立服务 (render_service.py, Win10)。本 web 后端不再本地起 render_driver,
# 而是把 draft_dir 打包成 zip POST 到 config.RENDER_SERVICE_URL/render, 拿到 remote_task_id;
# 状态/下载通过代理远程端点拉回。本地 tasks dict 仅缓存远程状态的影子 (供前端轮询)。
#
# 保留的本地结构 (供 render_status/render_download/render_list 影子缓存用):
tasks = {}
TASK_LOCK = threading.Lock()

# === 渲染节点消息互通 (心跳 + 事件推送接收端) ===
# 节点 (render_service) 每 30s POST /internal/render-heartbeat, 任务状态变化 POST /internal/render-event.
# 失联判定: 节点 120s 无心跳 → 该节点进行中任务标记 error (节点被关掉不再永远 rendering).
_RENDER_NODES = {}          # node_id -> {last_heartbeat, ...snapshot}
_NODE_STALE_SEC = 120


def _render_node_auth():
    """节点回调认证: 配置了 RENDER_SERVICE_TOKEN 时校验 X-Render-Token; 未配置放行(内网)."""
    expected = config.RENDER_SERVICE_TOKEN
    if not expected:
        return True
    import hmac
    return hmac.compare_digest(request.headers.get('X-Render-Token', ''), expected)


@app.route('/internal/render-heartbeat', methods=['POST'])
def internal_render_heartbeat():
    if not _render_node_auth():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    nid = data.get('node_id') or 'unknown'
    data['last_heartbeat'] = time.time()
    data['online'] = True
    _RENDER_NODES[nid] = data
    return jsonify({'ok': True})


@app.route('/internal/render-event', methods=['POST'])
def internal_render_event():
    """节点推送的任务状态变化 → 更新本地影子任务 (不用等前端轮询)."""
    if not _render_node_auth():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    remote_tid = data.get('task_id')
    if not remote_tid:
        return jsonify({'error': 'task_id required'}), 400
    with TASK_LOCK:
        local = None
        for t in tasks.values():
            if t.get('remote_task_id') == remote_tid:
                local = t
                break
    if local is None:
        return jsonify({'ok': False, 'note': 'no shadow task (可能是别的 web 后端的任务)'}), 200
    # 同步状态字段
    with TASK_LOCK:
        for k in ('status', 'error', 'mp4_name', 'progress', 'duration', 'desktop', 'started_at'):
            if data.get(k) is not None:
                local[k] = data[k]
        local['node_event_at'] = time.time()
    _persist(local['task_id'])
    # done → 后台拉取 mp4 到本地 (复用轮询路径的下载逻辑)
    if local.get('status') == 'done' and not local.get('mp4_path'):
        threading.Thread(target=_sync_remote_status, args=(local['task_id'],), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/render-nodes', methods=['GET'])
@login_required
def api_render_nodes():
    """渲染节点状态列表 (心跳注册表 + 派生在线状态)."""
    now = time.time()
    out = []
    for nid, n in _RENDER_NODES.items():
        d = dict(n)
        d['online'] = (now - n.get('last_heartbeat', 0)) < _NODE_STALE_SEC
        d['stale_seconds'] = int(now - n.get('last_heartbeat', 0))
        out.append(d)
    # 也把配置过但从未心跳的公共节点列出来 (标记 offline)
    known = {n.get('base_url', n.get('url', '')) for n in out}
    if config.RENDER_SERVICE_URL not in known:
        out.append({'node_id': config.RENDER_SERVICE_URL, 'online': False,
                    'note': '公共节点 (从未心跳 — 未配置 WEB_BASE_URL 或节点离线)'})
    out.sort(key=lambda x: (not x.get('online'), x.get('node_id', '')))
    return jsonify(out)


def _node_watchdog_loop():
    """每 30s: 心跳超时的节点 → 其进行中影子任务标记 error(失联)."""
    while True:
        time.sleep(30)
        try:
            now = time.time()
            stale_nodes = {nid for nid, n in _RENDER_NODES.items()
                           if now - n.get('last_heartbeat', 0) > _NODE_STALE_SEC}
            if not stale_nodes:
                continue
            with TASK_LOCK:
                affected = []
                for t in tasks.values():
                    if t.get('status') not in ('rendering', 'queued'):
                        continue
                    url = (t.get('render_url') or '').rstrip('/')
                    nid = t.get('render_node_id') or url
                    if nid and (nid in stale_nodes or any(nid.endswith(s.split(':')[-1]) for s in stale_nodes)):
                        t['status'] = 'error'
                        t['error'] = '渲染节点失联 (心跳超时 %ds) — 节点可能被关闭或断网' % _NODE_STALE_SEC
                        affected.append(t['task_id'])
            for tid in affected:
                _persist(tid)
                print('[render-node] 任务 %s 因节点失联标记 error' % tid, flush=True)
        except Exception as e:
            print('[render-node] watchdog 异常: %s' % e, flush=True)


def _new_task(task_id, **fields):
    """创建影子任务记录: 写内存 dict + 落盘 (task_store)."""
    with TASK_LOCK:
        tasks[task_id] = dict(fields)
        tasks[task_id].setdefault('created', time.time())
        tasks[task_id]['task_id'] = task_id
    try:
        task_store.upsert(tasks[task_id])
    except Exception:
        pass
    return tasks[task_id]


def _persist(task_id):
    """落盘影子任务状态 (失败不阻塞)."""
    try:
        with TASK_LOCK:
            t = tasks.get(task_id)
        if t:
            task_store.upsert(t)
    except Exception:
        pass


def _remote_headers(token=None):
    """转发到 render_service 的请求头 (带 X-Render-Token).

    token=None (默认) → 用公共 token config.RENDER_SERVICE_TOKEN (向后兼容现有调用);
    token=''  → 显式不带 token (dev 模式 render_service 不验 token);
    其他字符串 → 用该 token (per-user 自定义节点)。"""
    h = {}
    if token is None:
        token = config.RENDER_SERVICE_TOKEN
    if token:
        h['X-Render-Token'] = token
    return h


def _zip_draft_dir(draft_dir, draft_name):
    """把 draft_dir 打包成自包含 zip (草稿文件夹整体 + assets/* 素材, 防路径穿越成员).
    返回 (zip_path, None) 或 (None, error)."""
    draft_dir = os.path.abspath(draft_dir)
    zip_path = os.path.join(UPLOAD_DIR, draft_name + '.zip')
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(draft_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, draft_dir)
                    z.write(full, arc)
        return zip_path, None
    except Exception as e:
        return None, str(e)


def _submit_to_node(task_id, zip_path, draft_name, uid, url, token):
    """单次向某个 render 节点提交 zip 草稿. 返回 (ok, remote_id|err, status|None).

    ok=True 时第二项为 remote_task_id、第三项为初始状态; ok=False 时第二项为短错误描述
    (供兜底 reason 拼接)、第三项为 None."""
    try:
        with open(zip_path, 'rb') as fh:
            files = {'draft': (draft_name + '.zip', fh, 'application/zip')}
            data = {'draft_name': draft_name}
            if uid:
                data['_user_id'] = uid
            r = requests.post(f'{url}/render',
                              files=files, data=data,
                              headers=_remote_headers(token), timeout=60)
        j = r.json() if r.ok else {}
        if r.ok and j.get('task_id'):
            return True, j['task_id'], j.get('status', 'queued')
        return False, 'rejected %s %s' % (r.status_code, (j.get('error') or '')[:200]), None
    except Exception as e:
        return False, 'unreachable %s' % str(e)[:200], None


def enqueue_render(task_id, draft_dir, draft_name):
    """把 draft_dir 打包 zip, 转发到 render_service; 拿到 remote_task_id 存进本地影子任务。

    per-user 路由: 优先走用户自定义节点 (user_render_store), 提交失败或未配置时回退公共节点。
    提交成功即把用了哪个节点的 url/token 记到任务上 (render_url/render_token), 之后
    _sync_remote_status 据此打对端点拉状态/下载。"""
    uid = None
    with TASK_LOCK:
        uid = tasks.get(task_id, {}).get('user_id')
    zip_path, err = _zip_draft_dir(draft_dir, draft_name)
    if not zip_path:
        with TASK_LOCK:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['error'] = 'zip draft failed: %s' % err
        _persist(task_id)
        return
    try:
        user_url, user_token = user_render_store.get_effective(uid)
        pub_url, pub_token = config.RENDER_SERVICE_URL, config.RENDER_SERVICE_TOKEN
        has_user_node = bool(user_render_store.get(uid))  # 用户真配了自有节点?

        if has_user_node and user_url != pub_url:
            # 先打用户自有节点
            ok, remote_or_err, st = _submit_to_node(task_id, zip_path, draft_name, uid, user_url, user_token)
            if ok:
                remote_id, status = remote_or_err, st
                with TASK_LOCK:
                    tasks[task_id]['status'] = status
                    tasks[task_id]['remote_task_id'] = remote_id
                    tasks[task_id]['render_url'] = user_url
                    tasks[task_id]['render_node_id'] = user_url
                    tasks[task_id]['render_token'] = user_token
                    tasks[task_id]['fallback_reason'] = None
                _persist(task_id)
                print('[render-fwd] task %s -> remote %s @ user-node %s' % (task_id, remote_id, user_url), flush=True)
                return
            user_err = remote_or_err
            # 用户节点失败 → 回退公共节点
            ok2, remote_or_err2, st2 = _submit_to_node(task_id, zip_path, draft_name, uid, pub_url, pub_token)
            if ok2:
                remote_id, status = remote_or_err2, st2
                fallback_reason = '用户节点失败(%s),已回退公共节点' % user_err
                with TASK_LOCK:
                    tasks[task_id]['status'] = status
                    tasks[task_id]['remote_task_id'] = remote_id
                    tasks[task_id]['render_url'] = pub_url
                    tasks[task_id]['render_node_id'] = pub_url
                    tasks[task_id]['render_token'] = pub_token
                    tasks[task_id]['fallback_reason'] = fallback_reason
                _persist(task_id)
                print('[render-fwd] task %s -> remote %s @ public-node (user-node 失败: %s)' % (task_id, remote_id, user_err), flush=True)
                return
            # 两节点全失败
            pub_err = remote_or_err2
            with TASK_LOCK:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = ('render_service 均失败 — 用户节点: %s; 公共节点: %s' % (user_err, pub_err))[:400]
                tasks[task_id]['fallback_reason'] = None
            _persist(task_id)
            return
        else:
            # 未配置自有节点 → 直接走公共
            ok, remote_or_err, st = _submit_to_node(task_id, zip_path, draft_name, uid, pub_url, pub_token)
            if ok:
                remote_id, status = remote_or_err, st
                with TASK_LOCK:
                    tasks[task_id]['status'] = status
                    tasks[task_id]['remote_task_id'] = remote_id
                    tasks[task_id]['render_url'] = pub_url
                    tasks[task_id]['render_node_id'] = pub_url
                    tasks[task_id]['render_token'] = pub_token
                    tasks[task_id]['fallback_reason'] = None
                _persist(task_id)
                print('[render-fwd] task %s -> remote %s @ public-node' % (task_id, remote_id), flush=True)
                return
            with TASK_LOCK:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = 'render_service rejected/unreachable: %s' % remote_or_err
                tasks[task_id]['fallback_reason'] = None
            _persist(task_id)
    finally:
        try:
            os.unlink(zip_path)
        except Exception:
            pass


def _purge_stale_mp4(cur_task):
    """同一草稿 (draft_name) 只保留最新成片: 删除更早任务缓存的本的 mp4 文件.
    只删本地缓存目录 (VIDEOS) 下本系统命名 (rd_*) 的文件, 不碰用户自有视频."""
    try:
        dn = cur_task.get('draft_name')
        if not dn or not cur_task.get('mp4_path'):
            return
        cur_name = os.path.basename(cur_task['mp4_path'])
        cur_created = cur_task.get('created') or 0
        with TASK_LOCK:
            others = [v for v in tasks.values()
                      if v.get('draft_name') == dn and v.get('mp4_path')
                      and v.get('task_id') != cur_task.get('task_id')]
        removed = 0
        for o in others:
            p = o.get('mp4_path') or ''
            base = os.path.basename(p)
            # 更早的任务才删 (created 相同/未知时也删, 保持单一成片)
            if (o.get('created') or 0) > cur_created:
                continue
            if os.path.isfile(p) and base.startswith('rd_') and os.path.dirname(p) == VIDEOS:
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
            o.pop('mp4_path', None)   # 记录里也摘掉, 前端不再指向已删文件
            _persist(o.get('task_id'))
        if removed:
            print('[render] 草稿 %s 清理旧成片 %d 个 (仅保留 %s)' % (dn, removed, cur_name), flush=True)
    except Exception as e:
        print('[render] 清理旧成片失败: %s' % e, flush=True)


def _sync_remote_status(task_id):
    """代理拉取远程任务状态, 回写本地影子任务。done 时触发 mp4 拉取到本地缓存。

    端点路由: 读任务记录上的 render_url/render_token (提交时记下用了哪个节点).
    缺失 (旧任务) → 回退公共节点 config.RENDER_SERVICE_URL + _remote_headers()."""
    with TASK_LOCK:
        t = tasks.get(task_id)
    if not t:
        return None
    remote_id = t.get('remote_task_id')
    if not remote_id:
        return t  # 纯本地/旧任务, 无需同步
    base_url = t.get('render_url') or config.RENDER_SERVICE_URL
    headers = _remote_headers(t.get('render_token'))  # None → 公共 token (旧任务兼容)
    try:
        r = requests.get(f'{base_url}/render/status/{remote_id}',
                         headers=headers, timeout=30)
        if not r.ok:
            return t
        rt = r.json()
    except Exception:
        return t
    with TASK_LOCK:
        t['status'] = rt.get('status', t.get('status'))
        if rt.get('progress'):
            t['progress'] = rt.get('progress')
        if rt.get('duration') is not None:
            t['duration'] = rt.get('duration')
        if rt.get('error'):
            t['error'] = rt.get('error')
        if rt.get('mp4_name'):
            t['mp4_name'] = rt.get('mp4_name')
        # done 且本地尚无 mp4 → 拉取到本地缓存
        if t['status'] == 'done' and not t.get('mp4_path'):
            local_mp4 = os.path.join(VIDEOS, 'rd_' + task_id + '_' + (t.get('mp4_name') or 'output.mp4'))
            try:
                rr = requests.get(f'{base_url}/render/download/{remote_id}',
                                  headers=headers, timeout=300, stream=True)
                if rr.ok:
                    os.makedirs(VIDEOS, exist_ok=True)
                    with open(local_mp4, 'wb') as out:
                        for chunk in rr.iter_content(chunk_size=1 << 20):
                            if chunk:
                                out.write(chunk)
                    t['mp4_path'] = local_mp4
                    # 同一草稿只保留最新成片: 删除该草稿更早任务的 mp4, 避免反复渲染堆满磁盘
                    _purge_stale_mp4(t)
                else:
                    t['error'] = 'download from render_service failed: %s' % rr.status_code
            except Exception as e:
                t['error'] = 'download from render_service failed: %s' % str(e)[:200]
        tasks[task_id] = t
    _persist(task_id)
    return t


def task_cleanup_loop():
    """定期清理过期任务记录 (config.TASK_TTL)"""
    while True:
        time.sleep(3600)
        now = time.time()
        with TASK_LOCK:
            expired = [k for k, v in tasks.items()
                       if now - v.get('created', now) > config.TASK_TTL]
            for tid in expired:
                tasks.pop(tid, None)
        for tid in expired:
            try:
                task_store.delete(tid)
            except Exception:
                pass


def restore_tasks():
    """启动时从 SQLite 恢复任务历史; 未完成 (queued/rendering) 的标记为中断。
    (远程 render_service 各自独立恢复, 本地影子只重建记录不重派任务。)"""
    saved = task_store.load_all()
    with TASK_LOCK:
        for tid, t in saved.items():
            if t.get('status') in ('queued', 'rendering'):
                t['status'] = 'error'
                t['error'] = '服务重启, 任务中断'
                task_store.upsert(t)
            tasks.setdefault(tid, t)
    print('[task_store] 恢复 %d 条历史任务' % len(saved), flush=True)


# 启动: render 池已迁出, 本服务不再起本地 worker (渲染由远程 render_service 完成)


def _find_draft_main_video(draft_dir):
    """在草稿里找主视频文件路径: video 轨道上时长最大的 segment 的素材.
    path 有效用 path, 否则按 remote_url 文件名去上传目录找 (同 inject_draft 的修复逻辑).
    找不到返回 None."""
    cp = os.path.join(draft_dir, 'draft_content.json')
    if not os.path.isfile(cp):
        return None
    try:
        c = json.load(open(cp, encoding='utf-8'))
    except Exception:
        return None
    mats = {m.get('id'): m for m in (c.get('materials', {}).get('videos') or [])}
    best_dur, best_path = 0, None
    for tr in c.get('tracks') or []:
        if tr.get('type') != 'video':
            continue
        for s in tr.get('segments') or []:
            dur = (s.get('target_timerange') or {}).get('duration') or 0
            m = mats.get(s.get('material_id'))
            if not m:
                continue
            p = m.get('path')
            if not (p and os.path.isfile(p)):
                rp = (m.get('remote_url') or '').replace('\\', '/')
                cand = os.path.join(UPLOAD_DIR, os.path.basename(rp)) if rp else ''
                p = cand if (cand and os.path.isfile(cand)) else None
            if p and dur > best_dur:
                best_dur, best_path = dur, p
    return best_path


def _draft_cover_url(draft_dir, folder_name):
    """草稿封面 URL: 优先 JianYing 自带的 draft_cover.jpg; 没有就从主视频 ffmpeg 截 1s 处一帧,
    缓存到 CACHE_DIR/draft_covers/<folder>.jpg (CACHE_DIR 在 serve 白名单里, 按源视频 mtime 失效).
    没有主视频返回 None —— 前端显示黑屏占位 (符合预期)."""
    from urllib.parse import quote
    cand = os.path.join(draft_dir, 'draft_cover.jpg')
    if os.path.isfile(cand):
        return '/api/video/serve?path=' + quote(cand.replace('\\', '/'))
    src = _find_draft_main_video(draft_dir)
    if not src:
        return None
    covers_dir = os.path.join(config.CACHE_DIR, 'draft_covers')
    try:
        os.makedirs(covers_dir, exist_ok=True)
        jpg = os.path.join(covers_dir, folder_name + '.jpg')
        src_mtime = os.path.getmtime(src)
        if not (os.path.isfile(jpg) and os.path.getmtime(jpg) > src_mtime):
            # 短边缩到 480: 横屏源出 480x270, 竖屏源出 ~270x480 —— 竖屏封面保持竖版
            # (前端 object-contain 以高度为基准展示, 竖图不会被裁成扁条)
            vf = "scale='if(gt(iw,ih),480,-2)':'if(gt(iw,ih),-2,480)'"
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', '1', '-i', src,
                            '-frames:v', '1', '-vf', vf, jpg],
                           capture_output=True, timeout=20)
        if os.path.isfile(jpg):
            return '/api/video/serve?path=' + quote(jpg.replace('\\', '/'))
    except Exception:
        pass
    return None


def find_draft_dir(extract_dir):
    """在解压目录找含 draft_content.json 的文件夹"""
    for root, dirs, files in os.walk(extract_dir):
        if 'draft_content.json' in files:
            return root
    return None


@app.route('/render', methods=['POST'])
@login_required
def render():
    """提交 zip 草稿, 异步渲染, 返回 task_id.
    multipart: draft=<zip文件>  (zip 内含 draft_content.json 的草稿文件夹)
    可选 form: draft_name=自定义草稿名
    """
    f = request.files.get('draft')
    if not f:
        return jsonify({'error': "no 'draft' file in multipart"}), 400
    task_id = uuid.uuid4().hex[:8]
    draft_name = (request.form.get('draft_name') or ('up_' + task_id))
    extract_dir = os.path.join(UPLOAD_DIR, draft_name)
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir)
    zip_path = os.path.join(UPLOAD_DIR, draft_name + '.zip')
    f.save(zip_path)
    try:
        with zipfile.ZipFile(zip_path) as z:
            config.safe_zip_extract(z, extract_dir)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'zip extract failed: %s' % e}), 400
    draft_dir = find_draft_dir(extract_dir)
    if not draft_dir:
        return jsonify({'error': 'invalid draft: no draft_content.json in zip'}), 400
    uid = current_user_id()
    _new_task(task_id, status='queued', draft_name=draft_name, draft_dir=draft_dir,
              user_id=uid)
    enqueue_render(task_id, draft_dir, draft_name)
    return jsonify({'task_id': task_id, 'status': 'queued',
                    'poll': '/render/status/%s' % task_id})


@app.route('/render/status/<task_id>', methods=['GET'])
@login_required
def render_status(task_id):
    with TASK_LOCK:
        t = tasks.get(task_id)
    if not t:
        return jsonify({'error': 'unknown task'}), 404
    # 多租户: 只能看自己的任务 (内部自调用透传 _user_id 时放行; 任务无 user_id 视为旧数据放行)
    uid = current_user_id()
    if uid and t.get('user_id') and t.get('user_id') != uid and not _is_internal_call():
        return jsonify({'error': 'unknown task'}), 404
    # 远程转发任务: 拉取最新远程状态回写影子任务
    if t.get('remote_task_id'):
        t = _sync_remote_status(task_id) or t
    # 剔除 render_token (纵深防御: token 不出 web 后端) + draft_dir (内部路径)
    return jsonify({k: v for k, v in (t or {}).items() if k not in ('draft_dir', 'render_token')})


@app.route('/render/download/<task_id>', methods=['GET'])
@login_required
def render_download(task_id):
    with TASK_LOCK:
        t = tasks.get(task_id)
    if not t:
        return jsonify({'error': 'unknown task'}), 404
    uid = current_user_id()
    if uid and t.get('user_id') and t.get('user_id') != uid and not _is_internal_call():
        return jsonify({'error': 'unknown task'}), 404
    # 远程转发任务: done 时本地可能尚无 mp4, 先同步拉取
    if t.get('remote_task_id') and (t.get('status') != 'done' or not t.get('mp4_path')):
        t = _sync_remote_status(task_id) or t
    if t.get('status') != 'done':
        return jsonify({'error': 'not done', 'status': t.get('status')}), 400
    if not t.get('mp4_path') or not os.path.isfile(t.get('mp4_path')):
        return jsonify({'error': 'mp4 not available yet'}), 404
    return send_file(t['mp4_path'], as_attachment=True, download_name=t.get('mp4_name', 'output.mp4'))


@app.route('/render/list', methods=['GET'])
@login_required
def render_list():
    uid = current_user_id()
    me = current_user()
    with TASK_LOCK:
        items = [{'task_id': k, 'status': v.get('status'),
                  'mp4': v.get('mp4_name'), 'draft': v.get('draft_name'),
                  'duration': v.get('duration'), 'error': v.get('error'),
                  'progress': v.get('progress'), 'created': v.get('created'),
                  'fallback_reason': v.get('fallback_reason')}
                 for k, v in tasks.items()
                 # 多租户: 只列自己的任务; 无 user_id 的旧任务只对 admin 可见 (向后兼容,
                 # 避免普通用户看到迁移前全局遗留的渲染记录)
                 if (not v.get('user_id') and me and me.get('is_admin'))
                 or v.get('user_id') == uid]
    return jsonify(items)


@app.route('/render/draft/<draft_id>', methods=['POST'])
@login_required
def render_by_draft_id(draft_id):
    """按草稿渲染. draft_id 可为:
       - 草稿文件夹名 (root_meta_info.json 的 folder, 如 '8月11日' 或 'JYRender_0')
       - 剪映 draft_id (UUID, 如 'D9C6B7ED-...') — 兜底, 仅当存在同名文件夹时.
     查找 DRAFT_ROOT/draft_id 或 VectCutAPI 目录/draft_id."""
    # 多租户: 只能渲染自己的草稿 (admin 不受限; 404 不泄露草稿是否存在)。
    if not _draft_owned(draft_id, current_user_id()):
        return jsonify({'error': 'draft folder not found: %s' % draft_id}), 404
    # 先在剪映草稿目录按文件夹名直接找
    draft_dir = os.path.join(DRAFT_ROOT, draft_id)
    if not os.path.isdir(draft_dir):
        # draft_id 是 UUID 时, 反查 root_meta 拿到文件夹名
        root_meta = os.path.join(DRAFT_ROOT, 'root_meta_info.json')
        if os.path.exists(root_meta):
            try:
                m = json.load(open(root_meta, encoding='utf-8'))
                for d in m.get('all_draft_store', []):
                    if d.get('draft_id', '') == draft_id:
                        fold = d.get('draft_fold_path', '')
                        if fold and os.path.isdir(fold):
                            draft_dir = fold
                        break
            except Exception:
                pass
    # 在 VectCutAPI 目录找
    if not os.path.isdir(draft_dir):
        draft_dir = os.path.join(VC_DIR, draft_id)
    if not os.path.isdir(draft_dir):
        return jsonify({'error': 'draft folder not found: %s' % draft_id}), 404
    # 兼容: VectCutAPI 保存的草稿用 draft_info.json (非 draft_content.json)
    content_json = 'draft_content.json' if os.path.exists(os.path.join(draft_dir, 'draft_content.json')) \
                   else ('draft_info.json' if os.path.exists(os.path.join(draft_dir, 'draft_info.json')) else None)
    if not content_json:
        return jsonify({'error': 'not a valid jianying draft (no draft_content.json / draft_info.json)'}), 400
    uid = current_user_id()
    task_id = uuid.uuid4().hex[:8]
    _new_task(task_id, status='queued', draft_name=draft_id, draft_dir=draft_dir,
              user_id=uid)
    enqueue_render(task_id, draft_dir, draft_id)
    return jsonify({'task_id': task_id, 'status': 'queued',
                    'poll': '/render/status/%s' % task_id})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'render_server', 'videos_dir': VIDEOS})


# ============================================================ 认证路由
@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """登录: 校验用户名密码, 写 session。无注册 (用户由 admin 创建)。"""
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    u = user_store.get_by_username(username)
    if not u or not user_store.verify_password(u['password_hash'], password):
        return jsonify({'error': '用户名或密码错误'}), 401
    session.clear()
    session['uid'] = u['id']
    session.permanent = True
    return jsonify({'user': user_store.public_dict(u)})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    """当前登录用户 (前端启动探测登录态)。"""
    u = current_user()
    if not u:
        return jsonify({'user': None}), 401
    return jsonify({'user': user_store.public_dict(u)})


# ============================================================ Admin 用户管理 (无注册, admin 统一管)
@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_list_users():
    """列出所有用户 (不含密码哈希)。"""
    users = user_store.list_all()
    return jsonify({'users': [user_store.public_dict(u) for u in users]})


@app.route('/api/admin/users', methods=['POST'])
@admin_required
def admin_create_user():
    """新建用户。{username, password, display_name?, is_admin?}"""
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    display_name = (data.get('display_name') or '').strip() or None
    is_admin = bool(data.get('is_admin', False))
    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    try:
        u = user_store.create_user(username, password, is_admin=is_admin, display_name=display_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    return jsonify({'user': user_store.public_dict(u)}), 201


@app.route('/api/admin/users/<user_id>', methods=['DELETE'])
@admin_required
def admin_delete_user(user_id):
    """删除用户。禁止删除自己 (避免锁死)。"""
    me = current_user()
    if me and me['id'] == user_id:
        return jsonify({'error': '不能删除自己'}), 400
    ok = user_store.delete_user(user_id)
    if not ok:
        return jsonify({'error': '用户不存在'}), 404
    return jsonify({'ok': True})


@app.route('/api/admin/users/<user_id>/password', methods=['POST'])
@admin_required
def admin_reset_password(user_id):
    """重置用户密码。{password}"""
    data = request.json or {}
    password = data.get('password') or ''
    if not password:
        return jsonify({'error': '密码不能为空'}), 400
    if not user_store.get(user_id):
        return jsonify({'error': '用户不存在'}), 404
    user_store.update_password(user_id, password)
    return jsonify({'ok': True})


@app.route('/api/admin/users/<user_id>/admin', methods=['POST'])
@admin_required
def admin_toggle_admin(user_id):
    """设置/取消 admin。{is_admin: bool}"""
    data = request.json or {}
    is_admin = bool(data.get('is_admin', False))
    if not user_store.get(user_id):
        return jsonify({'error': '用户不存在'}), 404
    user_store.set_admin(user_id, is_admin)
    return jsonify({'ok': True})


def _resolve_draft_dir(draft_id):
    """把 draft_id (文件夹名 或 剪映 UUID) 解析成磁盘草稿目录.
    与 /render/draft/<id> 同逻辑: 先 DRAFT_ROOT/draft_id, 再反查 root_meta, 再 VectCutAPI/draft_id.
    找不到返回 None."""
    draft_dir = os.path.join(DRAFT_ROOT, draft_id)
    if not os.path.isdir(draft_dir):
        root_meta = os.path.join(DRAFT_ROOT, 'root_meta_info.json')
        if os.path.exists(root_meta):
            try:
                m = json.load(open(root_meta, encoding='utf-8'))
                for d in m.get('all_draft_store', []):
                    if d.get('draft_id', '') == draft_id:
                        fold = d.get('draft_fold_path', '')
                        if fold and os.path.isdir(fold):
                            draft_dir = fold
                        break
            except Exception:
                pass
    if not os.path.isdir(draft_dir):
        draft_dir = os.path.join(VC_DIR, draft_id)
    if not os.path.isdir(draft_dir):
        return None
    return draft_dir


def _warmup_draft(draft_id):
    """把一个"冷草稿"(只存在于磁盘, 不在 VectCutAPI DRAFT_CACHE 里)载入内存缓存, 使其可被编辑
    (delete_segment / delete_track / add_video / add_text / save_draft 等都要求草稿在 DRAFT_CACHE 中)。

    实现: 用 pyJianYingDraft 的 Script_file.load_template(draft_content.json) 把磁盘草稿读成
    Script_file 对象 —— 它会把每条轨道重建进 imported_tracks (import_track 会把可编辑的音视频片段
    还原成 Video_segment/Audio_segment, 不可编辑的退回 ImportedSegment 保证 round-trip 不丢数据),
    materials 进 imported_materials。delete_impl 的 _all_segments 同时遍历 script.tracks 与
    imported_tracks, 故载入后即可定位/删除片段。

    幂等: 草稿已在缓存则直接返回 True。找不到磁盘草稿返回 False。
    """
    if not FUSED_VC:
        return False
    try:
        from draft_cache import DRAFT_CACHE, update_cache
        if draft_id in DRAFT_CACHE:
            return True
        # 找磁盘上的 draft_content.json / draft_info.json
        draft_dir = _resolve_draft_dir(draft_id)
        if not draft_dir:
            return False
        cp = os.path.join(draft_dir, 'draft_content.json')
        if not os.path.isfile(cp):
            cp = os.path.join(draft_dir, 'draft_info.json')
        if not os.path.isfile(cp):
            return False
        import pyJianYingDraft as _draft
        script = _draft.Script_file.load_template(cp)
        update_cache(draft_id, script)
        print('[warmup] 冷草稿已载入缓存: %s (tracks=%d)' % (
            draft_id, len(script.tracks) + len(getattr(script, 'imported_tracks', []) or [])), flush=True)
        return True
    except Exception as e:
        print('[warmup] 载入冷草稿失败 %s: %s' % (draft_id, repr(e)[:200]), flush=True)
        return False


@app.route('/api/draft/timeline/<draft_id>', methods=['GET'])
@login_required
def api_draft_timeline(draft_id):
    """读取草稿 draft_content.json 的原始 JSON (tracks/materials/duration/fps) 供前端只读时间线展示.
    优先用 /query_script 的内存缓存 (含 force_update 元数据刷新); 缓存 miss 时直接读磁盘 json 兜底,
    这样冷草稿 (未在 VectCutAPI 缓存里) 也能查看时间线. 返回 {success, output:<json string>, error}.
    ?force_update=1 时强制刷新缓存草稿的媒体元数据 (与 query_script_impl force_update 一致)."""
    # 多租户: 只能读自己的草稿 (admin 不受限)。
    if not _draft_owned(draft_id, current_user_id()):
        return jsonify({'success': False, 'output': '', 'error': 'draft not found'}), 404
    force_update = request.args.get('force_update') in ('1', 'true', 'True')
    result = {'success': False, 'output': '', 'error': ''}
    # 1. 先试内存缓存 (/query_script 同源逻辑)
    try:
        if FUSED_VC:
            from save_draft_impl import query_script_impl
            script = query_script_impl(draft_id=draft_id, force_update=force_update)
            if script is not None:
                result['success'] = True
                result['output'] = script.dumps()
                result['source'] = 'cache'
                return jsonify(result)
    except Exception as e:
        result['error'] = 'cache query failed: %s; ' % repr(e)[:120]

    # 2. 缓存 miss → 读磁盘 draft_content.json / draft_info.json
    draft_dir = _resolve_draft_dir(draft_id)
    if not draft_dir:
        result['error'] = (result.get('error') or '') + 'draft folder not found: %s' % draft_id
        return jsonify(result), 404
    cp = os.path.join(draft_dir, 'draft_content.json')
    if not os.path.isfile(cp):
        cp = os.path.join(draft_dir, 'draft_info.json')
    if not os.path.isfile(cp):
        result['error'] = (result.get('error') or '') + 'no draft_content.json / draft_info.json in %s' % draft_dir
        return jsonify(result), 404
    try:
        with open(cp, encoding='utf-8') as f:
            raw = f.read()
        # 校验是合法 JSON 再回 (前端要 JSON.parse)
        content = json.loads(raw)

        # 回填 video/audio 素材的真实可 serve 路径:
        # 对话生成的草稿磁盘 json 里 path/media_path 常为空, 真实文件名在 remote_url (纯文件名或绝对路径).
        # 用 ALLOWED_SERVE_DIRS 把它解析成绝对路径并回填到 path, 这样前端 serveUrl 能直接取用, 不再被 path 守卫跳过.
        for mtype in ('videos', 'audios'):
            for m in (content.get('materials', {}).get(mtype) or []):
                p = (m.get('path') or '').strip()
                if p and os.path.isfile(p):
                    continue
                cand = None
                for src in (m.get('path'), m.get('media_path'), m.get('remote_url'), m.get('source_path')):
                    s = (src or '').strip()
                    if not s:
                        continue
                    if os.path.isfile(s):
                        cand = s
                        break
                    # 纯文件名 → 在 serve 允许目录里找
                    base = os.path.basename(s)
                    for d in config.ALLOWED_SERVE_DIRS:
                        full = os.path.join(d, base)
                        if os.path.isfile(full):
                            cand = full
                            break
                    if cand:
                        break
                if cand:
                    m['path'] = os.path.realpath(cand)

        result['success'] = True
        result['output'] = json.dumps(content, ensure_ascii=False)
        result['source'] = 'disk'
        return jsonify(result)
    except Exception as e:
        result['error'] = (result.get('error') or '') + 'read json failed: %s' % repr(e)[:120]
        return jsonify(result), 500


@app.route('/api/draft/<draft_id>/add-asset', methods=['POST'])
@login_required
def api_draft_add_asset(draft_id):
    """手动把一个素材追加到激活草稿 (供 AssetPanel 的"加到草稿"按钮).
    复用 agent 层的路径解析 + 自动接龙逻辑, 落盘后返回. 与聊天 agent execute_tool 行为一致,
    区别在于: 显式校验草稿在 VectCutAPI DRAFT_CACHE (冷草稿 cache-miss 时返回明确错误,
    不走 get_or_create_draft 的静默新建空草稿陷阱).

    请求体: {asset_path, asset_type:'video'|'audio'|'image', start?, end?}
    asset_path 用素材的绝对路径 (Asset.path).
    """
    uid = current_user_id()
    data = request.json or {}
    asset_path = data.get('asset_path', '')
    asset_type = data.get('asset_type', '')
    start = data.get('start')
    end = data.get('end')

    # 多租户: 只能往自己的草稿加素材 (admin 不受限)。
    if not _draft_owned(draft_id, uid):
        return jsonify({'ok': False, 'error': '草稿不存在或无权访问'}), 404

    if not asset_path or asset_type not in ('video', 'audio', 'image'):
        return jsonify({'ok': False, 'error': '参数错误: 需 asset_path 与 asset_type(video/audio/image)'}), 400

    # 1. 校验草稿在 DRAFT_CACHE —— query_script_impl 返回 None 即不在缓存 (冷草稿).
    #    不用 get_or_create_draft (它会静默新建空草稿, 原草稿纹丝不动).
    #    同时拿回存活的 Script_file 对象, 供后续去重判定复用 (避免二次查询).
    try:
        from save_draft_impl import query_script_impl
        script = query_script_impl(draft_id=draft_id, force_update=False)
        if script is None:
            # 冷草稿 (服务重启后缓存丢失): 就地 warmup (从磁盘 draft_content.json 载入),
            # 成功则继续; 不再要求用户手动去 Drafts 重新打开.
            if not _warmup_draft(draft_id):
                return jsonify({'ok': False, 'error': '草稿未在缓存且磁盘上未找到 (可能服务已重启后草稿目录变更); 请在 Drafts 重新打开该草稿或新建草稿'}), 400
            script = query_script_impl(draft_id=draft_id, force_update=False)
            if script is None:
                return jsonify({'ok': False, 'error': '草稿载入缓存失败, 请在 Drafts 重新打开该草稿或新建草稿'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': '草稿缓存校验失败: %s' % repr(e)[:160]}), 500

    # 2. 路径解析 (裸文件名 → 上传目录绝对路径; 已是绝对路径/协议不动).
    url = _resolve_asset_url(asset_path, uid=uid)

    # 2.5 去重判定 —— 手动导入(加到草稿)不允许重复导入同一素材.
    #     判据: 按 url_to_hash(解析后的 url) 拼出 VectCutAPI 内部会生成的 material_name,
    #     若草稿 materials 里已存在同名素材, 拒绝追加并返回 duplicate 标志.
    #     (聊天 agent 的 add_video/add_audio/add_image 不走此路 —— agent 可能有意复用素材做画中画/叠加.)
    try:
        from util import url_to_hash
        _h = url_to_hash(url)
        if asset_type == 'video':
            _would_name = 'video_%s.mp4' % _h
            _existing = {getattr(m, 'material_name', None) for m in script.materials.videos}
        elif asset_type == 'image':
            # 图片在 VectCutAPI 里存为 Video_material(material_type='photo'), 故也在 videos 列表.
            _would_name = 'image_%s.png' % _h
            _existing = {getattr(m, 'material_name', None) for m in script.materials.videos}
        else:  # audio
            _would_name = 'audio_%s.mp3' % _h
            _existing = {getattr(m, 'material_name', None) for m in script.materials.audios}
        if _would_name in _existing:
            return jsonify({'ok': True, 'duplicate': True,
                            'note': '该素材已在草稿中, 已跳过', 'material_name': _would_name})
    except Exception as e:
        # 去重判定本身出错不应阻断导入 (退化到旧行为: 允许重复).
        print('[add-asset] 去重判定异常 (降级为允许导入): %s' % repr(e)[:200], flush=True)

    # 3. 按 asset_type 分派, 镜像 execute_tool 的接龙逻辑.
    try:
        if asset_type == 'video':
            track_name = 'video_main'
            target_start = start if start is not None else _track_end_seconds(draft_id, track_name)
            d = {'draft_id': draft_id, 'video_url': url, 'track_name': track_name, 'target_start': target_start}
            if end is not None: d['end'] = end
            r = _post_internal('add_video', d, user_id=uid)
            if not r.get('success'):
                return jsonify({'ok': False, 'error': r.get('error') or 'add_video 失败'}), 500
            resp = {'ok': True, 'track_name': track_name, 'target_start': target_start}

        elif asset_type == 'audio':
            # 音频不自动接龙 audio 轨 (与 agent 一致), start 默认 0.
            d = {'draft_id': draft_id, 'audio_url': url, 'volume': 0.5}
            if start is not None: d['start'] = start
            if end is not None: d['end'] = end
            r = _post_internal('add_audio', d, user_id=uid)
            if not r.get('success'):
                return jsonify({'ok': False, 'error': r.get('error') or 'add_audio 失败'}), 500
            resp = {'ok': True}

        else:  # image
            track_name = 'image_main'
            img_start = start if start is not None else _track_end_seconds(draft_id, track_name)
            img_end = end if end is not None else img_start + 3
            d = {'draft_id': draft_id, 'image_url': url, 'track_name': track_name, 'start': img_start, 'end': img_end}
            r = _post_internal('add_image', d, user_id=uid)
            if not r.get('success'):
                return jsonify({'ok': False, 'error': r.get('error') or 'add_image 失败'}), 500
            resp = {'ok': True, 'track_name': track_name, 'start': img_start}

        # 4. 落盘 (失败仅记 log, 不阻断 —— 素材已加进内存草稿).
        try:
            sr = _post_internal('save_draft', {'draft_id': draft_id}, user_id=uid)
            if not sr.get('success'):
                print('[add-asset] save_draft 失败 (素材已加入内存草稿, 不阻断): %s' % sr.get('error'), flush=True)
        except Exception as se:
            print('[add-asset] save_draft 异常: %s' % se, flush=True)

        return jsonify(resp)

    except Exception as e:
        return jsonify({'ok': False, 'error': '添加异常: %s' % repr(e)[:200]}), 500


# ============================================================ 前端 API
@app.route('/api/perceive', methods=['POST'])
@login_required
def api_perceive_by_path():
    """按文件路径分析视频。内存缓存优先 (O(1))，无则调 VLM 并缓存。"""
    from memory_store import get_analysis, has_analysis, save_analysis
    uid = current_user_id()
    data = request.json or {}
    path = data.get('path', '')
    force = data.get('force', False)
    if not path or not os.path.exists(path):
        return jsonify({'error': f'文件不存在: {path}'}), 400
    if not config.is_allowed_path(path):
        return jsonify({'error': 'path not allowed'}), 403

    # 内存查询 (O(1) dict)
    if not force:
        cached = get_analysis(path, owner=uid)
        if cached:
            cached['_cached'] = True
            return jsonify(cached)

    # 调 VLM 分析
    try:
        ext = os.path.splitext(path)[1].lower()
        if _ASSET_TYPE_BY_EXT.get(ext) == 'image':
            from perceive import perceive_image
            result = perceive_image(path)
        else:
            from perceive import perceive_video
            result = perceive_video(
                path,
                do_asr=data.get('do_asr', True),
                frame_count=int(data.get('frames', 4))
            )
        result['_cached'] = False
        # 写入内存 + 落盘
        save_analysis(path, result, owner=uid)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/perceive/cached', methods=['GET'])
@login_required
def api_perceive_cached():
    """内存查询 (不读磁盘)"""
    from memory_store import has_analysis, get_analysis
    uid = current_user_id()
    path = request.args.get('path', '')
    if not path:
        return jsonify({'cached': False})
    if has_analysis(path, owner=uid):
        return jsonify({'cached': True, 'result': get_analysis(path, owner=uid)})
    return jsonify({'cached': False})


@app.route('/api/drafts', methods=['GET'])
@login_required
def api_list_drafts():
    """列出剪映草稿目录的所有草稿（仿 CapCut 草稿管理）"""
    import glob
    root_meta = os.path.join(DRAFT_ROOT, 'root_meta_info.json')
    drafts = []
    if os.path.exists(root_meta):
        try:
            m = json.load(open(root_meta, encoding='utf-8'))
            for d in m.get('all_draft_store', []):
                fold = d.get('draft_fold_path', '')
                # 封面: 优先 JianYing 自带 draft_cover.jpg, 没有则从主视频截帧 (见 _draft_cover_url)
                cover_url = _draft_cover_url(fold, os.path.basename(fold)) if os.path.isdir(fold) else None

                drafts.append({
                    'id': d.get('draft_id', ''),
                    'name': d.get('draft_name', ''),
                    'duration': d.get('tm_duration', 0) / 1e6,  # 微秒→秒
                    'created': d.get('tm_draft_create', 0) / 1e6,
                    'modified': d.get('tm_draft_modified', 0) / 1e6,
                    'folder': os.path.basename(fold),
                    'fold_path': fold,
                    'cover_url': cover_url,
                    'type': d.get('draft_type', ''),
                    'json_file': d.get('draft_json_file', ''),
                    'size_bytes': d.get('draft_timeline_materials_size', 0),
                })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # 补充: 对话生成的草稿 (VectCutAPI 目录, 不在 root_meta 里) —— 不列出来用户在
    # Drafts 面板就看不到自己刚生成的草稿, 也点不了 Render.
    seen_folders = {d.get('folder') for d in drafts}
    try:
        for name in sorted(os.listdir(VC_DIR)):
            if name.startswith('template') or name in seen_folders:
                continue
            dd = os.path.join(VC_DIR, name)
            cp = os.path.join(dd, 'draft_content.json')
            if not os.path.isfile(cp):
                continue
            try:
                c = json.load(open(cp, encoding='utf-8'))
            except Exception:
                continue
            mt = os.path.getmtime(cp)
            drafts.append({
                'id': name, 'name': name,
                'duration': (c.get('duration') or 0) / 1e6,
                'created': mt, 'modified': mt,
                'folder': name, 'fold_path': dd,
                'cover_url': _draft_cover_url(dd, name),
                'type': 'chat',
                'json_file': cp, 'size_bytes': 0,
            })
    except Exception:
        pass

    # 多租户: 只返回本用户的草稿 (admin 可见全部; 无前缀 legacy/剪映原生草稿仅 admin 可见)。
    uid = current_user_id()
    drafts = [d for d in drafts if _draft_owned(d.get('folder'), uid)]

    # 附加最新渲染成片: 按 draft_name 匹配 done 任务, 取最近一次的 mp4 (预览/下载入口).
    try:
        _done = {}
        _tasks = task_store.load_all(user_id=uid)
        _tasks = _tasks.values() if isinstance(_tasks, dict) else _tasks
        for t in _tasks:
            dn = t.get('draft_name')
            if not dn or t.get('status') != 'done' or not t.get('mp4_path'):
                continue
            if dn not in _done or (t.get('created') or 0) > (_done[dn].get('created') or 0):
                _done[dn] = t
        for d in drafts:
            t = _done.get(d.get('folder')) or _done.get(d.get('name'))
            if t:
                d['mp4_path'] = t['mp4_path']
                d['mp4_name'] = t.get('mp4_name', '')
                d['mp4_size'] = t.get('mp4_size') or (
                    os.path.getsize(t['mp4_path']) if os.path.isfile(t['mp4_path']) else 0)
    except Exception as e:
        print('[drafts] 附加渲染成片失败: %s' % e, flush=True)

    # 按修改时间倒序
    drafts.sort(key=lambda x: x.get('modified', 0), reverse=True)
    return jsonify(drafts)


@app.route('/api/cover', methods=['GET'])
@login_required
def api_draft_cover():
    """获取草稿封面图"""
    folder = request.args.get('folder', '')
    if not config.safe_folder_name(folder):
        return '', 400
    if not _draft_owned(folder, current_user_id()):
        return '', 404
    cover_path = os.path.join(DRAFT_ROOT, folder, 'draft_cover.jpg')
    if os.path.exists(cover_path):
        return send_file(cover_path, mimetype='image/jpeg')
    return '', 404


@app.route('/api/drafts/<folder>', methods=['DELETE'])
@login_required
def api_delete_draft(folder):
    """删除草稿（文件夹 + root_meta 同步）.
    草稿可能在两处: 剪映 DRAFT_ROOT (原生) 或 VC_DIR (对话生成 dfd_cat_*), 都要能删."""
    if not config.safe_folder_name(folder):
        return jsonify({'error': 'invalid folder name'}), 400
    if not _draft_owned(folder, current_user_id()):
        return jsonify({'error': 'not found'}), 404
    draft_path = os.path.join(DRAFT_ROOT, folder)
    in_draft_root = os.path.isdir(draft_path)
    vc_path = os.path.join(VC_DIR, folder)
    in_vc_dir = os.path.isdir(vc_path)
    if not in_draft_root and not in_vc_dir:
        return jsonify({'error': 'not found'}), 404
    deleted = []
    try:
        if in_draft_root:
            shutil.rmtree(draft_path)
            deleted.append('DRAFT_ROOT')
        if in_vc_dir:
            shutil.rmtree(vc_path)
            deleted.append('VC_DIR')
        # 同步 root_meta (仅 DRAFT_ROOT 的草稿在里面登记)
        root_meta = os.path.join(DRAFT_ROOT, 'root_meta_info.json')
        if os.path.exists(root_meta):
            with open(root_meta, encoding='utf-8') as fh:
                m = json.load(fh)
            m['all_draft_store'] = [d for d in m.get('all_draft_store', []) if os.path.basename(d.get('draft_fold_path', '')) != folder]
            m['draft_ids'] = len(m['all_draft_store'])
            with open(root_meta, 'w', encoding='utf-8') as fh:
                json.dump(m, fh, ensure_ascii=False)
        return jsonify({'ok': True, 'deleted': deleted})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/memory/stats', methods=['GET'])
@admin_required
def api_memory_stats():
    """内存缓存统计"""
    from memory_store import video_cache_stats, list_all_analysis
    stats = video_cache_stats()
    stats['analysis_count'] = len(list_all_analysis())
    return jsonify(stats)


@app.route('/api/memory/analysis', methods=['GET'])
@admin_required
def api_memory_analysis_list():
    """列出所有内存中的分析元数据"""
    from memory_store import list_all_analysis
    return jsonify(list_all_analysis())


@app.route('/api/build-marker', methods=['GET'])
def api_build_marker():
    """诊断用: 验证运行中的进程加载的是不是最新代码."""
    import datetime
    return jsonify({'marker': 'cors-fix-v1', 'pid': os.getpid(),
                    'now': datetime.datetime.now().isoformat()})


@app.route('/api/video/serve', methods=['GET'])
@login_required
def api_video_serve():
    """从内存缓存提供素材字节 (≤50MB 零磁盘 IO; 视频/音频/图片都走这个, 名字是历史遗留).
    仅限白名单目录. mimetype 按真实扩展名猜, 不能固定写 video/mp4 —— 否则 <audio>/<img>
    标签拿到的 Content-Type 是错的, 浏览器不认."""
    from memory_store import maybe_load_video, get_video_bytes
    import mimetypes
    path = request.args.get('path', '')
    if not path or not os.path.exists(path):
        return jsonify({'error': 'not found'}), 404
    if not config.is_allowed_path(path):
        return jsonify({'error': 'path not allowed'}), 403
    mimetype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    # CORS: 发布功能要用 —— 视频号/抖音/小红书的上传页里, 由 bsk 注入的 JS 需要从本服务
    # fetch mp4 再 DataTransfer 塞进 <input type=file> (bsk 无原生文件上传命令), 跨源必带 CORS.
    # 多租户安全: 收窄到配置的允许来源 (不再裸 *), 同源前端天然带 cookie 不受影响.
    origin = request.headers.get('Origin', '')
    allow_origin = origin if origin in config.CORS_ALLOW_ORIGINS else (
        config.CORS_ALLOW_ORIGINS[0] if config.CORS_ALLOW_ORIGINS else '')
    cors = {'Access-Control-Allow-Origin': allow_origin} if allow_origin else {}
    # 尝试载入内存
    maybe_load_video(path)
    data = get_video_bytes(path)
    if data:
        from flask import Response
        return Response(data, mimetype=mimetype,
                        headers={'Content-Length': str(len(data)),
                                 'Cache-Control': 'max-age=3600',
                                 'X-From-RAM': 'true',
                                 **cors})
    # 太大不在内存, 走磁盘
    resp = send_file(path, mimetype=mimetype)
    if allow_origin:
        resp.headers['Access-Control-Allow-Origin'] = allow_origin
    return resp


@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """前端文件上传，保存到 render_uploads/ (文件名强制净化防穿越)"""
    uid = current_user_id()
    # 多租户: 每个用户的素材落到 UPLOAD_DIR/<user_id>/ 子目录 (Phase 1)
    user_upload_dir = os.path.join(UPLOAD_DIR, uid or '_shared')
    os.makedirs(user_upload_dir, exist_ok=True)
    files = request.files.getlist('files')
    if not files:
        files = [request.files.get('file')]
    results = []
    for f in files:
        if not f or not f.filename:
            continue
        name = secure_filename(f.filename) or 'upload.bin'
        dst = os.path.join(user_upload_dir, name)
        f.save(dst)
        results.append(_classify_asset(name, dst))
    return jsonify({'assets': results})


# 展示给用户的素材类型: 视频/图片/音频/字幕(srt)/文案(txt, 常见于 LocalSend "发文字").
# 其余(包括 .gitkeep 等占位文件、无法识别的扩展名)一律归 'other', 列表接口里过滤掉不展示.
_ASSET_TYPE_BY_EXT = {
    '.mp4': 'video', '.mov': 'video', '.avi': 'video', '.mkv': 'video',
    '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.webp': 'image',
    '.mp3': 'audio', '.wav': 'audio', '.aac': 'audio', '.m4a': 'audio',
    '.srt': 'subtitle',
    '.txt': 'text',
}


def _classify_asset(name, path):
    ext = os.path.splitext(name)[1].lower()
    ftype = _ASSET_TYPE_BY_EXT.get(ext, 'other')
    asset = {'name': name, 'path': path, 'type': ftype, 'file': path}
    try:
        st = os.stat(path)
        asset['size'] = st.st_size
        asset['modified_at'] = st.st_mtime
    except Exception:
        pass
    if ftype == 'video':
        # 探测音轨是否存在, 供前端展示"已去除音轨"状态 (成功去音后这里会变 False)
        try:
            from perceive import has_audio_stream
            asset['has_audio'] = has_audio_stream(path)
        except Exception:
            asset['has_audio'] = None
    return asset


@app.route('/api/assets', methods=['GET'])
@login_required
def api_assets_scan():
    """扫描 render_uploads/ 目录, 返回当前所有素材 (供 LocalSend 收到新文件后前端刷新).
    过滤掉无法识别的文件(如 .gitkeep 占位文件) —— 那些不是给用户看的素材."""
    uid = current_user_id()
    # 多租户: 只扫该用户子目录 (Phase 1). admin 可传 ?user=<id> 查看他人素材.
    target_uid = uid
    if current_user() and current_user().get('is_admin'):
        target_uid = request.args.get('user') or uid
    user_upload_dir = os.path.join(UPLOAD_DIR, target_uid or '_shared')
    results = []
    try:
        # 默认按修改时间倒序: 最新上传/收到的文件排在最前.
        names = [n for n in os.listdir(user_upload_dir)
                 if not n.startswith('.') and os.path.isfile(os.path.join(user_upload_dir, n))]
        names.sort(key=lambda n: os.path.getmtime(os.path.join(user_upload_dir, n)), reverse=True)
        for name in names:
            p = os.path.join(user_upload_dir, name)
            asset = _classify_asset(name, p)
            if asset['type'] == 'other':
                continue
            results.append(asset)
    except Exception:
        pass
    return jsonify({'assets': results})


@app.route('/api/assets/<name>', methods=['DELETE'])
@login_required
def api_delete_asset(name):
    """删除 render_uploads/ 下的一个素材文件 (防路径穿越: 校验 basename 不改名, 否则带空格/括号
    的去重文件名如 'foo (1).mp4' 会被 secure_filename 改写导致匹配不上磁盘上的真实文件名)"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    uid = current_user_id()
    user_upload_dir = os.path.join(UPLOAD_DIR, uid or '_shared')
    path = os.path.join(user_upload_dir, name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    try:
        os.remove(path)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/assets/<name>/strip-audio', methods=['POST'])
@login_required
def api_asset_strip_audio(name):
    """去掉视频的音轨 —— 去音后该文件没有音轨, has_audio_stream()=False, 分析时
    自动跳过 VAD/ASR 整段逻辑, 只靠 VLM 看画面来匹配, 彻底不受环境音/嘈杂人声被
    webrtcvad 误判成"语音"的影响 (即上面 VID_20260814_200626.mp4 那种情况)。"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    uid = current_user_id()
    user_upload_dir = os.path.join(UPLOAD_DIR, uid or '_shared')
    path = os.path.join(user_upload_dir, name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    ext = os.path.splitext(name)[1].lower()
    if _ASSET_TYPE_BY_EXT.get(ext) != 'video':
        return jsonify({'ok': False, 'error': '只能对视频文件去除声音'}), 400

    tmp_path = path + '.noaudio.tmp' + ext
    try:
        r = subprocess.run(
            [resolve_ffmpeg(), '-y', '-i', path, '-c:v', 'copy', '-an', tmp_path],
            capture_output=True, timeout=120
        )
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            err = r.stderr.decode('utf-8', 'replace')[-300:] if r.stderr else 'unknown error'
            return jsonify({'ok': False, 'error': f'ffmpeg 处理失败: {err}'}), 500
        os.replace(tmp_path, path)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass

    from memory_store import invalidate_analysis
    invalidate_analysis(path, owner=uid)
    return jsonify({'ok': True, 'name': name, 'has_audio': False})


def _asset_thumb_path(path):
    """视频缩略图磁盘缓存路径: CACHE_DIR/asset_thumbs/<sha1(path)+mtime>.jpg
    文件名带源 mtime, 源文件被替换后 mtime 变化 → 自动失效重新抽帧."""
    import hashlib
    h = hashlib.sha1(path.encode('utf-8')).hexdigest()[:16]
    try:
        mtime = int(os.path.getmtime(path))
    except Exception:
        mtime = 0
    return os.path.join(config.CACHE_DIR, 'asset_thumbs', f'{h}_{mtime}.jpg')


@app.route('/api/assets/thumbnail', methods=['GET'])
@login_required
def api_asset_thumbnail():
    """轻量缩略图: 视频 → ffmpeg 抽 1s 处一帧短边缩到 320 (缓存 jpg, 按源 mtime 失效);
    图片 → 302 重定向到 /api/video/serve (图片本就是单帧, 无需 ffmpeg).
    供前端卡片懒加载: 视口内才拉一张小 jpg, 而不是整段视频字节. 节省带宽/内存."""
    from urllib.parse import quote
    path = request.args.get('path', '')
    if not path or not os.path.isfile(path):
        return jsonify({'error': 'not found'}), 404
    if not config.is_allowed_path(path):
        return jsonify({'error': 'path not allowed'}), 403

    ext = os.path.splitext(path)[1].lower()
    ftype = _ASSET_TYPE_BY_EXT.get(ext, 'other')

    # 图片直接走 serve (302), 复用其内存缓存 + CORS + Cache-Control
    if ftype == 'image':
        return redirect('/api/video/serve?path=' + quote(path.replace('\\', '/')))

    # 非视频(音频/字幕/文本/other)没有缩略图概念, 404 让前端回退占位图标
    if ftype != 'video':
        return jsonify({'error': 'no thumbnail for this type'}), 404

    jpg = _asset_thumb_path(path)
    # 缓存命中(且比源文件新)直接发, 否则 ffmpeg 抽帧
    if not (os.path.isfile(jpg) and os.path.getmtime(path) <= os.path.getmtime(jpg)):
        try:
            os.makedirs(os.path.dirname(jpg), exist_ok=True)
            # 短边缩到 320: 横屏 320x180, 竖屏 180x320 —— 缩略图够用且极小
            vf = "scale='if(gt(iw,ih),320,-2)':'if(gt(iw,ih),-2,320)'"
            r = subprocess.run(
                [resolve_ffmpeg(), '-y', '-v', 'error', '-ss', '1', '-i', path,
                 '-frames:v', '1', '-vf', vf, jpg],
                capture_output=True, timeout=20)
            if r.returncode != 0 or not os.path.isfile(jpg):
                return jsonify({'error': 'ffmpeg failed'}), 500
        except Exception as e:
            return jsonify({'error': str(e)[:200]}), 500

    resp = send_file(jpg, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'max-age=86400'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


def _shot_url(path):
    if not path:
        return None
    from urllib.parse import quote
    return f'/api/video/serve?path={quote(path)}'


def _shots_response(shots):
    out = []
    for s in shots:
        out.append({**s, 'clip_url': _shot_url(s.get('clip_path')), 'keyframe_url': _shot_url(s.get('keyframe_path'))})
    return out


@app.route('/api/assets/<name>/shots', methods=['GET'])
@login_required
def api_asset_get_shots(name):
    """只读镜头拆分缓存, 没拆过返回 shots: null (前端据此显示"去拆分"而不是"拆分中")。"""
    if not config.safe_folder_name(name):
        return jsonify({'error': 'invalid name'}), 400
    uid = current_user_id()
    path = os.path.join(UPLOAD_DIR, uid or '_shared', name)
    if not os.path.isfile(path):
        return jsonify({'error': 'not found'}), 404
    import shot_split
    shots = shot_split.get_cached_shots(path)
    return jsonify({'shots': _shots_response(shots) if shots else None})


@app.route('/api/assets/<name>/split-shots', methods=['POST'])
@login_required
def api_asset_split_shots(name):
    """分镜拆分: GPU CNN 特征检测镜头边界, 按边界切出每个镜头的独立小视频 + 关键帧。
    结果缓存, 大视频/长视频耗时可能到几十秒(推理+每个镜头重新编码), 非 force 命中缓存立即返回。"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    uid = current_user_id()
    path = os.path.join(UPLOAD_DIR, uid or '_shared', name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    ext = os.path.splitext(name)[1].lower()
    if _ASSET_TYPE_BY_EXT.get(ext) != 'video':
        return jsonify({'ok': False, 'error': '只能对视频文件做分镜拆分'}), 400

    data = request.json or {}
    import shot_split
    try:
        shots = shot_split.split_shots(
            path,
            force=bool(data.get('force', False)),
            sample_fps=int(data.get('sample_fps', 5)),
            min_scene_len_sec=float(data.get('min_scene_len_sec', 0.6)),
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'name': name, 'shots': _shots_response(shots)})


@app.route('/api/main-video', methods=['GET'])
@login_required
def api_get_main_video():
    """当前"主视频"(最新录制的那条, 每次会被替换) —— 跟长期存在的素材库分开管理。"""
    import main_video_store
    info = main_video_store.get(current_user_id())
    if not info:
        return jsonify({'main_video': None})
    return jsonify({'main_video': _main_video_response(info)})


def _main_video_response(info):
    """poster.jpg 是固定文件名, 换主视频后内容变了但 URL 字符串没变 —— 浏览器会拿
    Cache-Control 缓存的旧图顶替, 界面上看到的就是上一条主视频的画面。拼个版本号
    (每次 set() 都会变) 到 URL 后面, 强制换主视频后浏览器认为是新资源, 重新请求。"""
    v = info.get('set_at', 0)
    url = _shot_url(info['path'])
    poster_url = _shot_url(info.get('poster_path'))
    return {
        **info,
        'url': f'{url}&v={v}' if url else url,
        'poster_url': f'{poster_url}&v={v}' if poster_url else poster_url,
    }


@app.route('/api/assets/<name>/set-main', methods=['POST'])
@login_required
def api_asset_set_main(name):
    """把这个素材标记为当前主视频。旧的主视频不用做任何处理 —— 指针一移开它自动就是
    普通素材库里的一条了, 可以被当成补充素材(比如剪一段旧镜头用)继续复用。"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    uid = current_user_id()
    path = os.path.join(UPLOAD_DIR, uid or '_shared', name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    ext = os.path.splitext(name)[1].lower()
    if _ASSET_TYPE_BY_EXT.get(ext) != 'video':
        return jsonify({'ok': False, 'error': '只能把视频文件设为主视频'}), 400
    import main_video_store
    info = main_video_store.set(path, uid)
    return jsonify({'ok': True, 'main_video': _main_video_response(info)})


@app.route('/api/main-video/clear', methods=['POST'])
@login_required
def api_clear_main_video():
    import main_video_store
    main_video_store.clear(current_user_id())
    return jsonify({'ok': True})


# ============================================================ per-user 自定义 render 节点配置
# 每个用户自助配置自己的 render_service (URL + token): 渲染优先走自己的 CapCut,
# 提交失败/未配置时回退公共节点 (config.RENDER_SERVICE_URL). 全部 @login_required,
# 非 admin 也能自助配置 (与 settings_store 的 admin-only 不同).
def _mask_secret(v):
    """token 脱敏: 仅留末 4 位 (与 settings_store._mask 一致, 复用于 GET 接口)。"""
    if not v:
        return ''
    if len(v) <= 4:
        return '*' * len(v)
    return '*' * (len(v) - 4) + v[-4:]


def _render_config_view(uid):
    """返回脱敏后的用户 render 节点配置视图 (token 不出明文)。"""
    import user_render_store
    cfg = user_render_store.get(uid)
    if not cfg:
        return {'url': '', 'token': '', 'configured': False,
                'public_url': config.RENDER_SERVICE_URL}
    return {'url': cfg.get('url', ''), 'token': _mask_secret(cfg.get('token', '')),
            'configured': True, 'public_url': config.RENDER_SERVICE_URL}


@app.route('/api/render-config', methods=['GET'])
@login_required
def api_get_render_config():
    """获取当前用户的 render 节点配置 (token 脱敏). 未配置则 configured=False。"""
    return jsonify(_render_config_view(current_user_id()))


@app.route('/api/render-config', methods=['POST'])
@login_required
def api_save_render_config():
    """保存当前用户的 render 节点配置.

    body: {url, token?}. url 始终覆盖 (空串=清空恢复公共); token 为空/缺省=保留已存
    (避免脱敏回填把真值擦掉, 与 settings_store「留空则不修改」一致)."""
    import user_render_store
    data = request.json or {}
    url = data.get('url', '')
    token = data.get('token')  # None/'' → 保留已存
    user_render_store.save(current_user_id(), url, token)
    return jsonify(_render_config_view(current_user_id()))


@app.route('/api/render-config/test', methods=['POST'])
@login_required
def api_test_render_config():
    """测试连接: 探活用户配置的 render_service 的 /health.

    body: {url, token?}. /health 不验 token (render_service _token_gate 放行 /health),
    但带 token 时也附 header (更接近真实提交路径). 返回 {ok, detail, error?}."""
    data = request.json or {}
    url = (data.get('url') or '').strip().rstrip('/')
    if not url:
        return jsonify({'ok': False, 'error': 'url 不能为空'}), 400
    token = data.get('token')
    # token 为空 = 保留已存 (用已存 token 探活, 与保存语义一致)
    if not token:
        import user_render_store
        existing = user_render_store.get(current_user_id())
        token = existing['token'] if existing else ''
    try:
        r = requests.get(f'{url}/health', headers=_remote_headers(token), timeout=10)
        j = r.json() if r.ok else {}
        if r.ok and j.get('ok'):
            return jsonify({'ok': True, 'detail': j.get('service', 'render_service'),
                            'videos_dir': j.get('videos_dir'),
                            'desktops': j.get('desktops')})
        return jsonify({'ok': False, 'detail': j.get('service') if r.ok else None,
                        'error': 'HTTP %s %s' % (r.status_code, (j.get('error') or '')[:200])})
    except Exception as e:
        return jsonify({'ok': False, 'error': 'unreachable: %s' % str(e)[:200]})


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    """SSE 流式对话: Agent 用 function calling 按需查询资源信息.
    多轮记忆: conversation_id 对应的历史 user/assistant 文本会喂给 LLM 当上下文,
    本轮结束后连同 tool 卡片一起存回 chat_store, 供前端历史列表/切换会话用."""
    data = request.json or {}
    message = data.get('message', '')
    asset_paths = data.get('asset_paths', []) or []
    draft_id = data.get('draft_id')
    conversation_id = data.get('conversation_id')
    if not message:
        return jsonify({'error': 'message is required'}), 400

    # 多租户上下文: 当前用户 id, 透传给所有 store 调用与内部自调用
    uid = current_user_id()

    from flask import Response, stream_with_context
    import agent_runtime, agent_session, agent_tools
    import chat_store

    is_new_conversation = not conversation_id
    if is_new_conversation:
        conversation_id = chat_store.create(draft_id=draft_id, user_id=uid)
    prior = chat_store.get(conversation_id, user_id=uid)
    prior_messages = prior['messages'] if prior else []
    # 若已有会话之前存过 draft_id, 且这次请求没带(比如切换会话后前端还没同步), 用存的
    if not draft_id and prior and prior.get('draft_id'):
        draft_id = prior['draft_id']

    # 会话级锁: 同一会话串行 (旧行为是并发整体覆盖 save_messages, 后完成者赢会丢前一轮)
    _lk = _CHAT_LOCKS.setdefault(conversation_id, threading.Lock())
    if not _lk.acquire(blocking=False):
        # 卡死兜底: 锁被持有超过 10 分钟 (异常路径未释放) → 换新锁强制接管, 不让会话永久 409
        _held_since = _CHAT_LOCK_TIMES.get(conversation_id)
        if _held_since and time.time() - _held_since > 600:
            print('[chat] 会话 %s 锁卡死 %.0fs, 强制接管' % (conversation_id[:8], time.time() - _held_since), flush=True)
            _lk = _CHAT_LOCKS[conversation_id] = threading.Lock()
            _lk.acquire()
        else:
            return jsonify({'error': '该会话正在处理上一条消息, 请等它结束再发'}), 409
    _CHAT_LOCK_TIMES[conversation_id] = time.time()

    # 旧会话历史迁移 (幂等): chats.db 历史 → SDK session items
    if prior_messages:
        try:
            agent_session.migrate_history(conversation_id, prior_messages)
        except Exception as e:
            print('[chat] 历史迁移失败(继续用空session): %s' % e, flush=True)
    session = agent_session.get_session(conversation_id)

    import queue as _queue
    q = _queue.Queue()          # (kind, payload): text / tool / draft / done / error

    def sse(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def on_draft_created(new_id):
        q.put(('draft', {'draft_id': new_id}))

    def on_tool_executed(name, args, result_json):
        try:
            result_obj = json.loads(result_json)
        except Exception:
            result_obj = {'raw': str(result_json)[:500]}
        q.put(('tool', {'tool': name, 'args': args, 'result': result_obj}))

    ctx = agent_tools.ToolContext(uid=uid, asset_paths=asset_paths, draft_id=draft_id or '',
                                  on_draft_created=on_draft_created,
                                  on_tool_executed=on_tool_executed)

    turn_log = list(prior_messages)
    turn_log.append({'role': 'user', 'content': message})
    run_state = {'text_count': 0, 'tool_count': 0}   # 空响应兜底用

    # 手动停止: /api/chat/<cid>/stop 置位事件, 事件循环与 SSE 出队侧都检查
    cancel = threading.Event()
    _CHAT_CANCELS[conversation_id] = cancel

    async def _run_once(agent, user_input):
        """单次 SDK 流式 run: 把事件翻译进队列. 返回是否产出文本."""
        produced = False
        result = Runner.run_streamed(agent, user_input, session=session, max_turns=200)
        async for ev in result.stream_events():
            if cancel.is_set():
                break   # 用户点了停止: 不再消费事件, run 自然收尾
            if ev.type == 'run_item_stream_event':
                item = ev.item
                if item.type == 'message_output_item':
                    txt = ''.join(getattr(c, 'text', '') or ''
                                  for c in (getattr(item.raw_item, 'content', None) or []))
                    if txt.strip():
                        produced = True
                        q.put(('text', {'text': txt}))
        return produced

    async def _run_agent():
        # 先修复历史切口孤儿 (compaction 曾切在工具输出上 → DeepSeek 400), 再压缩
        try:
            if await agent_session.sanitize_session(session):
                print('[chat] 会话 %s 历史切口已修复 (丢弃孤儿工具输出)' % conversation_id[:8], flush=True)
        except Exception as e:
            print('[chat] 历史修复失败(跳过): %s' % e, flush=True)
        # 会话超长压缩 (摘要 + 最近条目)
        try:
            await agent_session.maybe_compact(session, conversation_id, uid)
        except Exception as e:
            print('[chat] compaction 失败(跳过): %s' % e, flush=True)

        agent = agent_runtime.build_agent(ctx)
        user_input = message
        # 空响应/未收尾兜底: run 无文本产出但执行过工具 → 以系统提示续跑 (最多 3 次)
        for attempt in range(3):
            try:
                produced = await _run_once(agent, user_input)
            except Exception as e:
                from agents.exceptions import InputGuardrailTripwireTriggered
                if isinstance(e, InputGuardrailTripwireTriggered):
                    q.put(('error', {'text': '（该请求被输入守卫拦截：消息超长或包含疑似提示注入/越权内容，请调整后再发）'}))
                else:
                    q.put(('error', {'text': f'Agent 运行失败: {e}'}))
                return
            if produced:
                return
            if cancel.is_set():
                return   # 用户点了停止, 不再续跑
            if run_state['tool_count'] == 0 and attempt == 0:
                # 一次工具都没调也没文本 —— 模型彻底空转, 直接提示
                q.put(('text', {'text': '（模型返回空内容, 请重发或换个说法）'}))
                return
            user_input = '（系统提示: 上一轮没有回复内容, 请继续当前任务; 若任务已完成, 请给出总结）'
        q.put(('text', {'text': '（模型连续返回空内容, 本轮中断 —— 发送「继续」我会接着推进）'}))

    _released = {'done': False}
    _acquired_at = time.time()

    def _release_lock():
        """幂等释放会话锁 + 清理取消标志. generate() 正常结束与 _worker 兜底都调 ——
        客户端提前断开 (SSE 被掐) 时 generator 可能不再执行 finally, 由 worker 兜底."""
        if _released['done']:
            return
        _released['done'] = True
        _CHAT_CANCELS.pop(conversation_id, None)
        try:
            _lk.release()
        except RuntimeError:
            pass

    def _worker():
        import asyncio
        try:
            asyncio.run(_run_agent())
        except Exception as e:
            q.put(('error', {'text': f'Agent 运行异常: {e}'}))
        finally:
            q.put(('done', None))
            _release_lock()   # 兜底: 客户端已断开时 generator 不会跑 finally

    def generate():
        # 首帧: 会话 id (新建会话时前端靠它拿到 id)
        yield sse({'conversation_id': conversation_id})
        import threading as _th
        t = _th.Thread(target=_worker, daemon=True)
        t.start()
        stopped = False
        while True:
            try:
                item = q.get(timeout=0.5)
            except Exception:  # queue.Empty — 超时窗口里检查停止标志
                if cancel.is_set():
                    stopped = True
                    break
                continue
            kind, payload = item
            if kind == 'done':
                break
            if kind == 'text':
                run_state['text_count'] += 1
                yield sse(payload)
                turn_log.append({'role': 'assistant', 'content': payload['text']})
            elif kind == 'tool':
                run_state['tool_count'] += 1
                yield sse(payload)
                turn_log.append({
                    'role': 'tool', 'content': f"Invoked: {payload['tool']}",
                    'toolDetails': payload,
                })
            elif kind == 'draft':
                yield sse(payload)
            elif kind == 'error':
                yield sse(payload)
                turn_log.append({'role': 'assistant', 'content': payload['text']})
        if stopped:
            yield sse({'text': '（已手动停止本轮 — 已完成的部分已保存）'})
            turn_log.append({'role': 'assistant', 'content': '（已手动停止本轮）'})
        try:
            chat_store.save_messages(conversation_id, turn_log, ctx.draft_id or draft_id, user_id=uid)
        except Exception as e:
            print('[chat] 会话保存失败: %s' % e, flush=True)
        finally:
            _release_lock()
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/chat/<cid>/stop', methods=['POST'])
@login_required
def api_chat_stop(cid):
    """手动停止该会话正在进行的这轮 Agent 运行 (已完成部分照常落库)."""
    ev = _CHAT_CANCELS.get(cid)
    if ev is None:
        return jsonify({'ok': False, 'error': '该会话当前没有进行中的运行'}), 404
    ev.set()
    return jsonify({'ok': True})


@app.route('/api/chat/conversations', methods=['GET'])
@login_required
def api_chat_conversations_list():
    """历史会话列表 (标题/草稿/时间), 供 Chat 面板侧栏用. 不含 messages 正文.
    带 ?draft_id= 时只返回该草稿下的会话 (一个草稿多个 chat 的过滤视图)."""
    import chat_store
    uid = current_user_id()
    draft_id = request.args.get('draft_id')
    if draft_id:
        return jsonify(chat_store.list_by_draft(draft_id, user_id=uid))
    return jsonify(chat_store.list_all(user_id=uid))


@app.route('/api/chat/conversations', methods=['POST'])
@login_required
def api_chat_conversations_create():
    """新建一条空会话 (New Chat 按钮)."""
    import chat_store
    data = request.json or {}
    cid = chat_store.create(draft_id=data.get('draft_id'), user_id=current_user_id())
    return jsonify({'id': cid})


@app.route('/api/chat/conversations/<cid>', methods=['GET'])
@login_required
def api_chat_conversations_get(cid):
    """取一条会话完整消息 (切换会话时用来恢复聊天记录)."""
    import chat_store
    conv = chat_store.get(cid, user_id=current_user_id())
    if not conv:
        return jsonify({'error': 'not found'}), 404
    return jsonify(conv)


@app.route('/api/chat/conversations/<cid>', methods=['DELETE'])
@login_required
def api_chat_conversations_delete(cid):
    import chat_store
    ok = chat_store.delete(cid, user_id=current_user_id())
    if not ok:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True})


def _text_animation_names(kind):
    """返回 CapCut 文字入场/出场动画的合法名字列表 (供 agent 校验/枚举, 避免瞎猜)."""
    try:
        from pyJianYingDraft.metadata.capcut_text_animation_meta import CapCut_Text_intro, CapCut_Text_outro
        enum_cls = CapCut_Text_intro if kind == 'intro' else CapCut_Text_outro
        return sorted(m.name for m in enum_cls)
    except Exception:
        return []


def _resolve_asset_url(u, uid=None):
    """把 LLM 传的素材标识解析成本地绝对路径 (正斜杠, pyJianYingDraft/ffprobe 都认).
    LLM 有时只传文件名 (如 'VID_20260819_102125.mp4') 而不是完整路径 —— 这种裸名会让
    VectCutAPI 的 update_media_metadata ffprobe 探不到文件 → 素材时长 0 → 主视频
    segment 零时长 → 渲染出来黑屏无声 (叠加段因显式传了 start/end 不受影响).
    规则: http(s)/file 协议不动; 已存在的绝对路径不动; 其余按文件名找 ——
    优先本租户子目录 (UPLOAD_DIR/<uid>/, 多租户后素材都在这), 再上传根目录/GUI 目录.
    模块级 (原为 generate() 闭包; 提到模块级以便 /api/draft/<id>/add-asset 复用,
    generate() 闭包内同名调用已删, 一并走这里)."""
    if not u:
        return u
    if u.startswith(('http://', 'https://', 'file://')):
        return u
    if os.path.isabs(u) and os.path.isfile(u):
        return u.replace('\\', '/')
    base = os.path.basename(u.replace('\\', '/'))
    search_dirs = []
    if uid:
        search_dirs.append(os.path.join(config.UPLOAD_DIR, uid))
    search_dirs.extend((config.UPLOAD_DIR, config.GUI_UPLOAD_DIR))
    for d in search_dirs:
        cand = os.path.join(d, base)
        if os.path.isfile(cand):
            resolved = cand.replace('\\', '/')
            if resolved != u:
                print('[resolve_asset_url] %r -> %r' % (u, resolved), flush=True)
            return resolved
    return u


def _track_end_seconds(draft_id, track_name, user_id=None):
    """查草稿里某条轨道当前已经铺到多少秒 —— add_video 不传 target_start 时用这个自动
    接龙, 不用 agent 自己心算已加了多少段/每段多长(算错了同一时间点会撞车覆盖).
    user_id 显式传入 (Agent 工具在请求外的工作线程跑, 不能读 session);
    不传时回退请求上下文 (add-asset 端点内调用)."""
    if not user_id:
        try:
            user_id = current_user_id()
        except RuntimeError:  # 请求上下文外 (worker 线程) 且未显式传 uid
            user_id = None
    r = _post_internal('query_script', {'draft_id': draft_id}, user_id=user_id)
    try:
        script = json.loads(r.get('output') or '{}')
        track = next((t for t in script.get('tracks', []) if t.get('name') == track_name), None)
        if not track or not track.get('segments'):
            return 0.0
        # 末尾取 max(start+dur); 转成秒后用向上取整到微秒(round(...,6) 会丢尾, ceil 更稳),
        # 再补 1µs 保险 —— 避免 39.613(四舍五入到 3 位) 落在真实末尾 39.613456 之前,
        # 导致下一段 target_start < 已有段 end → pyJianYingDraft 判 overlap 拒绝.
        import math
        end_us = max(s['target_timerange']['start'] + s['target_timerange']['duration']
                     for s in track['segments'])
        return round(math.ceil(end_us / 100) / 10000, 6) + 1e-6
    except Exception:
        return 0.0


def _post_internal(endpoint, data=None, user_id=None):
    """内部 API 调用 (HTTP 自调). 用独立 session 避免 SSE 长连接复用导致的连接池耗尽.
    render/draft 等异步端点立即返回; 同步端点最多等 120s.
    带 X-Internal-Token 旁路 session 认证; user_id 透传租户上下文 (写进 payload _user_id)."""
    try:
        url = f"{config.API_BASE}/{endpoint}"
        headers = {'X-Internal-Token': config.INTERNAL_TOKEN}
        payload = dict(data or {})
        if user_id:
            payload['_user_id'] = user_id
        with requests.Session() as s:
            r = s.post(url, json=payload, headers=headers, timeout=120)
            return r.json()
    except Exception as e:
        return {'error': str(e)}


def _get_internal(endpoint, user_id=None):
    """内部 API 调用 (HTTP 自调, GET). 用法同 _post_internal.
    带 X-Internal-Token 旁路; user_id 透传 (_user_id 查询参数).
    (原为 /api/chat 端点内闭包, SDK 迁移后提升为模块级供 agent_tools 使用)"""
    try:
        sep = '&' if '?' in endpoint else '?'
        url = f"{config.API_BASE}/{endpoint}"
        if user_id:
            url += f"{sep}_user_id={user_id}"
        headers = {'X-Internal-Token': config.INTERNAL_TOKEN}
        with requests.Session() as s:
            r = s.get(url, headers=headers, timeout=30)
            return r.json()
    except Exception as e:
        return {'error': str(e)}


@app.route('/api/templates', methods=['GET'])
@login_required
def api_templates():
    """列出可用模板"""
    try:
        from template_engine import list_templates
        return jsonify(list_templates())
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/templates/render', methods=['POST'])
@login_required
def api_template_render():
    """执行模板"""
    try:
        from template_engine import render_template
        data = request.json or {}
        tpl_path = os.path.join(HERE, 'templates', data.get('template', '') + '.yaml')
        result = render_template(tpl_path, data.get('variables', {}), do_render=data.get('render', False))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/localsend/status', methods=['GET'])
@login_required
def api_localsend_status():
    """LocalSend 接收端状态 (设备名/端口/是否有正在进行的传输/本次接收列表)"""
    try:
        import localsend_recv
        return jsonify(localsend_recv.status())
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)})


@app.route('/api/localsend/start', methods=['POST'])
@login_required
def api_localsend_start():
    """按需启动 LocalSend 接收端 (前端"接收"按钮触发)"""
    try:
        import localsend_recv
        # 设置页改过 LOCALSEND_IF_IP 后无需重启: 启动前按当前 env 刷新绑定 IP
        _if_ip = os.environ.get('LOCALSEND_IF_IP', '').strip()
        if _if_ip:
            localsend_recv.set_multicast_interface(_if_ip)
        # 多租户: 收到的文件落到该用户子目录
        uid = current_user_id()
        save_dir = os.path.join(UPLOAD_DIR, uid or '_shared')
        os.makedirs(save_dir, exist_ok=True)
        ok = localsend_recv.start_server(save_dir=save_dir)
        if ok:
            if config.AUTO_PERCEIVE:
                localsend_recv.set_on_file_received(_auto_perceive_hook)
            return jsonify({'running': True, 'alias': localsend_recv.ALIAS,
                            'port': localsend_recv.PORT, 'save_dir': save_dir})
        return jsonify({'running': False,
                        'error': '端口 53317 被占, 请关闭官方 LocalSend 或其他占用程序后重试'}), 409
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)}), 500


def _auto_perceive_hook(path):
    """素材收件回调: 视频文件后台调 VLM 分析 (避免阻塞收件线程).
    多租户: LocalSend 收件落 UPLOAD_DIR/<user_id>/, 从路径反推 owner, 把分析结果
    归属到该用户 (避免 legacy 全局可见或落到错误租户)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.mp4', '.mov', '.avi', '.mkv'):
        return

    from memory_store import has_analysis, save_analysis

    # 从 UPLOAD_DIR/<user_id>/<name> 反推 owner
    owner = None
    try:
        rp = os.path.realpath(path)
        ru = os.path.realpath(UPLOAD_DIR)
        if rp.startswith(ru + os.sep):
            owner = os.path.relpath(rp, ru).split(os.sep)[0]
            # _shared 占位不算真实租户
            if owner == '_shared':
                owner = None
    except Exception:
        pass

    def _work():
        if has_analysis(path, owner=owner):
            return
        try:
            from perceive import perceive_video
            result = perceive_video(path)
            save_analysis(path, result, owner=owner)
            print('[auto-perceive] 已分析 %s' % os.path.basename(path), flush=True)
        except Exception as e:
            print('[auto-perceive] 分析失败 %s: %s' % (os.path.basename(path), e), flush=True)

    threading.Thread(target=_work, daemon=True).start()


@app.route('/api/localsend/stop', methods=['POST'])
@login_required
def api_localsend_stop():
    """停止 LocalSend 接收端, 返回本次接收的文件列表 (供前端提示)"""
    try:
        import localsend_recv
        received = localsend_recv.stop_server()
        return jsonify({'running': False, 'received': received,
                        'received_count': len(received)})
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)}), 500


@app.route('/api/settings', methods=['GET'])
@admin_required
def api_settings_get():
    """当前 LLM/ASR 配置 (密钥脱敏, 附 configured 标记)"""
    import settings_store
    return jsonify(settings_store.get_settings())


@app.route('/api/settings', methods=['POST'])
@admin_required
def api_settings_save():
    """保存配置到 .env + 热更新 (密钥留空 = 不修改)"""
    import settings_store
    data = request.json or {}
    return jsonify(settings_store.save_settings(data))


@app.route('/api/settings/test', methods=['POST'])
@admin_required
def api_settings_test():
    """用给定(或已保存/默认)的值实测连通性, 不落盘"""
    import settings_store
    data = request.json or {}
    target = data.get('target')
    if target == 'llm':
        api_key = data.get('QWEN_API_KEY') or settings_store.effective_value('QWEN_API_KEY')
        base_url = data.get('QWEN_BASE_URL') or settings_store.effective_value('QWEN_BASE_URL')
        model = data.get('QWEN_MODEL') or settings_store.effective_value('QWEN_MODEL')
        return jsonify(settings_store.test_llm(api_key, base_url, model))
    if target == 'deepseek':
        api_key = data.get('DEEPSEEK_API_KEY') or settings_store.effective_value('DEEPSEEK_API_KEY')
        base_url = data.get('DEEPSEEK_BASE_URL') or settings_store.effective_value('DEEPSEEK_BASE_URL')
        model = data.get('DEEPSEEK_MODEL') or settings_store.effective_value('DEEPSEEK_MODEL')
        return jsonify(settings_store.test_llm(api_key, base_url, model))
    if target == 'asr':
        endpoint = data.get('ASR_ENDPOINT') or settings_store.effective_value('ASR_ENDPOINT')
        api_key = data.get('ASR_API_KEY') or settings_store.effective_value('ASR_API_KEY')
        return jsonify(settings_store.test_asr(endpoint, api_key))
    if target == 'tools':
        # 验证用户填的 FFMPEG_PATH (留空则走自动查找)
        ffmpeg_path = data.get('FFMPEG_PATH') if 'FFMPEG_PATH' in data else settings_store.effective_value('FFMPEG_PATH')
        return jsonify(settings_store.test_ffmpeg(ffmpeg_path))
    return jsonify({'ok': False, 'error': 'unknown target'}), 400


# ============================================================ 静态文件 (React 构建产物)
STATIC_DIR = config.STATIC_DIR

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_vue(path):
    """Flask 服务前端 SPA (catch-all)"""
    if not os.path.isdir(STATIC_DIR):
        return jsonify({'error': 'Frontend not built. Run: cd frontend-react && npm run build'}), 404
    full_path = os.path.join(STATIC_DIR, path)
    if os.path.isfile(full_path):
        return send_from_directory(STATIC_DIR, path)
    # SPA fallback
    index = os.path.join(STATIC_DIR, 'index.html')
    if os.path.isfile(index):
        return send_from_directory(STATIC_DIR, 'index.html')
    return jsonify({'error': 'index.html not found'}), 404


# ============================================================ 感知端点
@app.route('/perceive/video', methods=['POST'])
@login_required
def perceive_video_api():
    """让 agent "看懂"视频: VLM画面分析 + ASR语音转录 + 场景检测
    multipart: video=<视频文件>
    可选 form: do_asr=true/false, frames=5
    """
    from perceive import perceive_video
    f = request.files.get('video')
    if not f:
        return jsonify({'error': "no 'video' file"}), 400
    do_asr = request.form.get('do_asr', 'true').lower() != 'false'
    frame_count = int(request.form.get('frames', '5'))
    # 多租户: 临时文件落到该用户子目录
    uid = current_user_id()
    tmp_dir = os.path.join(UPLOAD_DIR, uid or '_shared')
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, 'perceive_' + uuid.uuid4().hex[:8] + os.path.splitext(f.filename)[1])
    f.save(tmp_path)
    try:
        result = perceive_video(tmp_path, do_asr=do_asr, frame_count=frame_count)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try: os.unlink(tmp_path)
        except: pass


@app.route('/perceive/result', methods=['POST'])
@login_required
def perceive_result_api():
    """渲染质检: 让 agent 检查渲染结果质量
    multipart: video=<mp4文件>
    可选 form: expectations=<JSON字符串, 质检标准>
    """
    from perceive import perceive_result
    f = request.files.get('video')
    if not f:
        return jsonify({'error': "no 'video' file"}), 400
    expectations = None
    if 'expectations' in request.form:
        try: expectations = json.loads(request.form['expectations'])
        except: expectations = {'text': request.form['expectations']}
    # 多租户: 临时文件落到该用户子目录
    uid = current_user_id()
    tmp_dir = os.path.join(UPLOAD_DIR, uid or '_shared')
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, 'check_' + uuid.uuid4().hex[:8] + '.mp4')
    f.save(tmp_path)
    try:
        result = perceive_result(tmp_path, expectations)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try: os.unlink(tmp_path)
        except: pass


if __name__ == '__main__':
    print('render_server on http://%s:%d (render 转发至 %s)' % (
        config.RENDER_SERVER_HOST, config.RENDER_SERVER_PORT,
        config.RENDER_SERVICE_URL), flush=True)
    # 恢复历史任务 (本地影子任务; 远程 render_service 各自独立恢复)
    restore_tasks()

    # 过期任务清理
    threading.Thread(target=task_cleanup_loop, daemon=True).start()
    threading.Thread(target=_node_watchdog_loop, daemon=True).start()  # 节点失联检测

    # LocalSend 接收端: 按需启动 (前端"接收"按钮触发, 不随服务常驻)
    print('[localsend] 接收端待命 (前端点"接收"按钮启动), 端口 53317', flush=True)

    # CORS (仅配置的来源; 前端生产环境同源托管不需要)
    if config.CORS_ALLOW_ORIGINS:
        @app.after_request
        def after_request(response):
            origin = request.headers.get('Origin', '')
            if origin in config.CORS_ALLOW_ORIGINS:
                response.headers.add('Access-Control-Allow-Origin', origin)
                response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
                response.headers.add('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS')
                response.headers.add('Vary', 'Origin')
            return response

    try:
        from waitress import serve
        print('[server] waitress 生产服务器', flush=True)
        serve(app, host=config.RENDER_SERVER_HOST, port=config.RENDER_SERVER_PORT,
              threads=8, channel_timeout=900)
    except ImportError:
        print('[server] WARN: 未安装 waitress, 回退 Flask 开发服务器 (pip install waitress)', flush=True)
        app.run(host=config.RENDER_SERVER_HOST, port=config.RENDER_SERVER_PORT, debug=False)
