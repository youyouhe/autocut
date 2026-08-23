# render_server.py — RESTful 渲染服务
# 接收 zip 草稿 → 真后台渲染 (--desktop) → 返回 mp4
# 融合 VectCutAPI 的编辑端点 (add_video/add_text/...) → 完整生产线
import os, sys, json, zipfile, threading, uuid, time, subprocess, shutil, secrets
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

# === 确保 ffmpeg 可被找到 (pythonw 启动时不继承交互终端的 PATH) ===
# FFmpeg 路径优先级: settings FFMPEG_PATH > C:\ffmpeg\bin 等候选目录 > 系统 PATH.
# resolve_ffmpeg() 返回可执行文件绝对路径, 并把其所在目录补进 PATH, 供所有
# subprocess.run(['ffmpeg', ...]) 调用使用 (去音/封面/分镜/感知抽帧都依赖它).
_FFMPEG_RESOLVED = None  # 缓存解析结果; None=未解析, ''=未找到(走 PATH 兜底)

def _ffmpeg_default_candidates():
    """系统级默认候选目录 (settings 未配置 FFMPEG_PATH 时兜底)."""
    return [
        r'C:\ffmpeg\bin',
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'ffmpeg', 'bin'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ffmpeg', 'bin'),
    ]

def resolve_ffmpeg(refresh=False):
    """返回 ffmpeg 可执行文件绝对路径; 找不到时返回 'ffmpeg' (交给系统 PATH).
    refresh=True 强制重新解析 (settings 改了 FFMPEG_PATH 后调用)."""
    global _FFMPEG_RESOLVED
    if _FFMPEG_RESOLVED is not None and not refresh:
        return _FFMPEG_RESOLVED

    from shutil import which

    # 1. settings 里配置的 FFMPEG_PATH (可指向 exe 本身或其所在目录)
    try:
        import settings_store
        configured = settings_store.effective_value('FFMPEG_PATH').strip()
    except Exception:
        configured = os.environ.get('FFMPEG_PATH', '').strip()

    resolved = ''
    candidates = []
    if configured:
        # 用户可能填的是 exe 路径, 也可能填的是 bin 目录
        if configured.lower().endswith('.exe') and os.path.isfile(configured):
            resolved = configured
            candidates.append(os.path.dirname(configured))
        elif os.path.isdir(configured):
            candidates.append(configured)

    # 2. 系统级默认目录兜底
    candidates.extend(_ffmpeg_default_candidates())

    # 3. 沿候选目录找 ffmpeg.exe; 同时也信任系统 PATH (which)
    for d in candidates:
        exe = os.path.join(d, 'ffmpeg.exe')
        if os.path.isfile(exe):
            resolved = resolved or exe
            if d not in os.environ.get('PATH', ''):
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
            break
    if not resolved:
        resolved = which('ffmpeg') or 'ffmpeg'  # 最后交给 PATH

    _FFMPEG_RESOLVED = resolved
    return resolved

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
            return jsonify({'ok': False, 'error': '草稿未在缓存, 可能服务已重启; 请在 Drafts 重新打开该草稿(点 Render 触发载入)或新建草稿'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': '草稿缓存校验失败: %s' % repr(e)[:160]}), 500

    # 2. 路径解析 (裸文件名 → 上传目录绝对路径; 已是绝对路径/协议不动).
    url = _resolve_asset_url(asset_path)

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
    from openai import OpenAI as _OAI
    from memory_store import get_analysis, save_analysis
    from perceive import QWEN_API_KEY as QWEN_KEY, QWEN_BASE_URL as QWEN_URL, QWEN_MODEL as LLM_MODEL
    import chat_store

    is_new_conversation = not conversation_id
    if is_new_conversation:
        conversation_id = chat_store.create(draft_id=draft_id, user_id=uid)
    prior = chat_store.get(conversation_id, user_id=uid)
    prior_messages = prior['messages'] if prior else []
    # 若已有会话之前存过 draft_id, 且这次请求没带(比如切换会话后前端还没同步), 用存的
    if not draft_id and prior and prior.get('draft_id'):
        draft_id = prior['draft_id']

    # 定义工具 (function calling)
    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "list_resources",
                "description": "列出所有已上传的资源（名称、类型、是否已分析、已有标签）。用户问'有哪些素材'时调用。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_main_video",
                "description": "获取当前被标记为'主视频'的素材(每次最新录制/最新一期的那条，跟长期存在的素材库分开管理，由用户在界面手动标记)。用户说'主视频'/'这次的视频'/'最新录的'而没指定具体文件名时，先调用这个解析出实际文件名，不要直接去猜或要求用户重复报文件名。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_by_tags",
                "description": "按关键词标签快速检索素材 —— SQLite 索引查询, 不调用 LLM/网络, 毫秒级返回。素材数量多时,先用这个粗筛出候选文件名, 缩小范围再对具体某个文件调用 get_resource_detail/get_transcript 看全文细节, 避免一次性把所有素材的完整描述都塞进对话上下文浪费 token。用户想找'带山的视频''风景图'这类按内容筛选素材的需求时优先用这个。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {"type": "array", "items": {"type": "string"}, "description": "要匹配的关键词, 如 ['山','水塘','风景']"},
                        "type": {"type": "string", "description": "只筛选某类素材: video/image/audio, 可选"}
                    },
                    "required": ["keywords"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_assets",
                "description": "按自由文本全文检索素材 —— SQLite 模糊查询, 在文件名/画面描述(VLM)/口播文案(ASR)/标签里搜, 不调用 LLM/网络。当标签检索(search_by_tags)不够精确、或用户用整句话描述要找的内容(如'有晚霞和凉亭的镜头')时, 用这个全文检索兜底。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索文本, 如 '晚霞 凉亭' 或 '河边风景'"},
                        "type": {"type": "string", "description": "只筛选某类素材: video/image/audio, 可选"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_resource_detail",
                "description": "查询单个资源的完整分析：画面描述(VLM)、口播文案(ASR)、时间轴、元数据。用户问'视频讲了什么/文案是什么/内容是什么'时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "资源文件名（如 file (3).mp4）"}
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_transcript",
                "description": "获取视频的语音转录文案（含时间戳）。用户问'口播文案/字幕/语音内容'时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "资源文件名"}
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_resource",
                "description": "对一个尚未分析的视频/图片资源做内容分析并缓存结果 (视频: 画面VLM+语音ASR; 图片: 直接VLM看图)。用户要求分析/查看内容而资源显示'未分析'时，直接调用这个工具自己触发分析，不要让用户去点界面按钮。视频较长时可能耗时数十秒。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "资源文件名"}
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "split_shots",
                "description": "对一个视频做分镜拆分：检测镜头边界(切镜点), 并把每个镜头切成独立的小视频文件+关键帧。用户要求'分镜/拆镜头/按镜头切开'时调用。已经拆过的直接返回缓存结果, 不重复拆。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "视频资源文件名"},
                        "force": {"type": "boolean", "description": "忽略缓存重新拆分, 默认 false"}
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_draft",
                "description": "获取一个可用草稿用于编辑。已存在激活草稿时直接复用当前草稿（不会新建空草稿、不丢失已有内容），只有当前无草稿时才真正新建。绝大多数情况不需要传参。只有用户明确说“新建草稿/重新做一个/另起一个”才需要 force_new=true 强制新建。返回 draft_id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "force_new": {"type": "boolean", "description": "强制新建一个空草稿（默认 false）。仅当用户明确要求“新建/重新做”时设 true。已有草稿时若误设 true 会丢失当前草稿上下文。", "default": False}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "use_draft",
                "description": "确认/切换当前激活草稿。要继续在已有草稿上编辑（加段/补字幕/渲染）时调用：不传 draft_id = 确认沿用当前激活草稿；传 draft_id = 切换到指定的已有草稿（从 list_drafts / get_draft_timeline 拿到的 id）。用它来“接着做”，不要用 create_draft（create_draft 在已有草稿时只是复用，语义混淆）。返回当前草稿的时间线摘要。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string", "description": "要切换到的草稿 id。不传则沿用当前激活草稿。"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_video",
                "description": "添加视频到草稿。默认接在主视频轨道('video_main')末尾按顺序拼接。要把某段视频作为'补充素材/花絮/B-roll'叠加显示在主视频的某个时间点上方时，必须指定不同的 track_name 和该素材应出现的 target_start，否则会和主视频挤在同一条轨道上互相覆盖。默认操作当前激活草稿；如需操作其他草稿可传 draft_id 指定。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "视频URL或路径"},
                        "start": {"type": "number", "description": "源视频截取起始秒(素材文件内的秒数)", "default": 0},
                        "end": {"type": "number", "description": "源视频截取结束秒(素材文件内的秒数)"},
                        "target_start": {"type": "number", "description": "这段素材在成片时间轴上应该出现的秒数(不是素材源文件的秒数)。不填=自动接在同名轨道已有内容末尾"},
                        "track_name": {"type": "string", "description": "轨道名，默认 'video_main'(主视频轨道)。叠加补充素材时用不同的名字，如 'broll_1'"},
                        "relative_index": {"type": "integer", "description": "轨道层级，数值越大越靠上层显示。叠加在主视频上方要设成比主视频轨道更高的值(如 1)，否则会被主视频盖住而不是盖住主视频"},
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_subtitle",
                "description": "按 SRT 内容批量加字幕轨(一条 cue 一段, 时间轴来自 SRT 本身, 和语音天然同步). "
                               "做视频字幕【必须】用这个工具, 禁止用 add_text 一条条手动排字幕 (拿不到真实语音时间点, "
                               "排出来必然不同步). SRT 从 get_transcript 拿 (返回里有 srt 字段) 或用户直接提供.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "srt": {"type": "string", "description": "SRT 全文 (标准格式: 序号/起止时间行/文本行), 不是文件路径"},
                        "time_offset": {"type": "number", "description": "整体时间偏移秒数, 默认0"},
                        "font_size": {"type": "number", "description": "字号, 默认5"},
                        "font_color": {"type": "string", "description": "字体颜色十六进制, 默认 '#FFFFFF'"},
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    },
                    "required": ["srt"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_text",
                "description": "添加文字到草稿；不止字幕，也可用于标题/水印/角标等任意文字标识——通过 track_name 区分轨道、transform_x/transform_y 控制画面位置。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "文字内容"},
                        "start": {"type": "number", "description": "开始秒"},
                        "end": {"type": "number", "description": "结束秒"},
                        "track_name": {"type": "string", "description": "轨道名，默认 'text_main'（字幕轨）；做独立文字标识/标题/水印时用不同轨道名（如 'label_1'）避免和字幕冲突叠压"},
                        "transform_x": {"type": "number", "description": "水平位置，-1(左)~1(右)，0为居中，默认0"},
                        "transform_y": {"type": "number", "description": "垂直位置，-1(底)~1(顶)，默认-0.8（画面下方，字幕常用位置）；标题/角标常用 0.7~0.9（画面上方）"},
                        "font_size": {"type": "number", "description": "字号，默认12"},
                        "font_color": {"type": "string", "description": "字体颜色，十六进制，默认 '#FFFFFF'"},
                        "background_color": {"type": "string", "description": "文字背景色（如水印底色），默认不显示背景"},
                        "background_alpha": {"type": "number", "description": "背景不透明度 0.0~1.0，默认0（无背景，纯色块不是毛玻璃/模糊效果——本工具不支持真实的背景模糊/毛玻璃特效）"},
                        "intro_animation": {"type": "string", "description": "入场动画名，如 'Random_Typewriter'(打字机)/'Blur_to_the_Left'(左移模糊)/'Bounce_from_TR'(右上弹入) 等，需精确匹配预置动画名，不确定就别填"},
                        "outro_animation": {"type": "string", "description": "出场动画名，同 intro_animation 命名规则，如 'Blur_to_the_Left'/'Horizontal_Close' 等，不确定就别填"},
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    },
                    "required": ["text", "start", "end"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_text_animations",
                "description": "查询 add_text 可用的入场/出场动画名字列表，调用 add_text 的 intro_animation/outro_animation 前先查一下，别瞎猜名字。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["intro", "outro"], "description": "查入场动画还是出场动画"}
                    },
                    "required": ["kind"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_audio",
                "description": "给草稿添加背景音乐/音频。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "音频URL或本地路径"},
                        "start": {"type": "number", "description": "插入到草稿时间轴的开始秒", "default": 0},
                        "end": {"type": "number", "description": "结束秒（不填=到音频末尾）"},
                        "volume": {"type": "number", "description": "音量 0-1", "default": 0.5}
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_image",
                "description": "给草稿添加图片（作为一段画面）。默认接在图片轨道('image_main')末尾顺序展示。要把图片作为'补充素材'叠加显示在主视频的某个时间点上方时，指定 start/end 为该时间点在成片时间轴上的秒数（而不是省略靠自动接龙），必要时指定更高的 relative_index 确保盖住主视频画面。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "图片URL或本地路径"},
                        "start": {"type": "number", "description": "插入到草稿时间轴的开始秒。不填=自动接在图片轨道已有内容末尾"},
                        "end": {"type": "number", "description": "结束秒。不填=从 start 起默认展示 3 秒"},
                        "track_name": {"type": "string", "description": "轨道名，默认 'image_main'。跟主视频叠加时可以保持默认(图片轨默认就在视频轨上方)"},
                        "relative_index": {"type": "integer", "description": "轨道层级，数值越大越靠上层显示"}
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_segment",
                "description": "从草稿删除一个轨道上的单个片段(段)。用户说'删掉某段/去掉后两段/这段不要了'时用。删除后底层会自动清理孤儿素材引用并重算总时长, 不用手动处理。定位方式二选一: (1) track_name + index —— 指定轨道第几个片段(从0开始); (2) segment_id —— 精确匹配(从 get_draft_timeline 拿不到 segment_id, 一般用 index 定位即可)。强烈建议: 先 get_draft_timeline 看清各段在哪个轨道、index 是几, 再删, 别凭记忆猜; 删一段后该轨道后面的段 index 会前移, 连删多段时从后往前删(index 不会乱)。删完可再 get_draft_timeline 复核。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "track_name": {"type": "string", "description": "轨道名, 如 'video_main'/'broll_1'/'text_main'。与 index 配合定位片段。"},
                        "index": {"type": "integer", "description": "该轨道上要删的片段序号, 从0开始(第1段=0)。与 track_name 配合。连删多段时从最大的 index 往前删。"},
                        "segment_id": {"type": "string", "description": "片段唯一 id, 精确匹配(可选, 一般不需要, 用 track_name+index 即可)。"},
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    },
                    "required": ["track_name", "index"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_track",
                "description": "删除一整条轨道(含其上所有片段)。用户说'把整个花絮轨/补充素材轨都删掉/不要这条轨'时用, 比逐段 delete_segment 更快。注意: 主轨道(video_main)删了草稿基本就空了, 删主轨道前跟用户确认。删完底层自动清理孤儿素材+重算时长。定位方式(三选一, 优先用前两种避免同名歧义): (1) track_id —— 从 get_draft_timeline 返回的 track_id 字段拿, 同名轨道也能精确删指定那一条(推荐); (2) delete_all=true + track_name —— 一次删掉所有同名的轨道; (3) track_name —— 删第一个匹配项, 若同名轨道>1条会返回 ambiguous:true, 此时应改用 track_id 或 delete_all。先 get_draft_timeline 看清再删。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "track_name": {"type": "string", "description": "要删除的轨道名, 如 'broll_1'/'broll_2'。与 track_id 二选一。"},
                        "track_id": {"type": "string", "description": "轨道唯一 id(从 get_draft_timeline 的 track_id 字段拿), 同名轨道消歧用, 精确删指定一条。"},
                        "delete_all": {"type": "boolean", "description": "为 true 时删除所有同名 track_name 的轨道。默认 false。"},
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_empty_tracks",
                "description": "删除所有零片段的空轨道。用户说'删掉空轨道/清掉空的 video 轨/有空轨道不要了'时用——一次清掉所有空轨道, 比逐条 delete_track 更省事, 也绕开同名歧义。可选用 track_type(如 'video')或 track_name 进一步过滤。建议先 get_draft_timeline 确认哪些是空的(is_empty:true)。删完底层自动清理孤儿素材+重算时长, 返回删除的轨道列表(含 track_id)。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "track_type": {"type": "string", "description": "只删该类型的空轨道, 如 'video'/'audio'/'text'（可选）。"},
                        "track_name": {"type": "string", "description": "只删该名字的空轨道, 如 'video'（可选, 用于精准清理某种预建垃圾轨）。"},
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "save_draft",
                "description": "保存草稿到磁盘。默认保存当前激活草稿；如需保存其他草稿可传 draft_id 指定。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "render",
                "description": "提交渲染（自动保存草稿并渲染为mp4）。用户说'渲染/导出/出片'时调用。调用前必须已 create_draft + add_video。默认渲染当前激活草稿；如需渲染其他草稿可传 draft_id 指定。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "render_status",
                "description": "查询一个渲染任务的进度/是否完成。render 工具返回的 task_id 传进来查。"
                               "wait=true 时服务端会阻塞等待(约25秒/次)直到状态变化或完成再返回 —— 自动监控时用它, "
                               "免得连续空查; 反复调用直到 status 变为 done/error 即完成监控。",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string", "description": "render 工具返回的任务ID"},
                                   "wait": {"type": "boolean", "description": "服务端等待到状态变化再返回(默认false立即返回)"}},
                    "required": ["task_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "bsk_run",
                "description": "执行一条 BrowserSkill(bsk) CLI 命令, 驱动用户已登录的浏览器完成网页操作"
                               "(如把渲染好的视频发布到视频号/抖音/小红书). 命令原样传给 bsk, 返回 stdout/stderr. "
                               "标准生命周期: bsk session start 拿会话id → 各命令都带 --session <id> → 最后必须 bsk session stop <id>.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "bsk 子命令(不含 bsk 前缀), 如 'session start' / 'navigate https://... --session ab12' / 'snapshot --session ab12'"},
                        "timeout": {"type": "number", "description": "秒, 默认 60; request-help 等待用户时给 300"}
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_drafts",
                "description": "列出磁盘上已保存的草稿（名称/时长/修改时间）。用户问'有哪些草稿/之前做的视频'时调用。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_draft_timeline",
                "description": "读取草稿的时间线:每条轨道上的素材(文件名/类型)、起止秒数、轨道名、总时长。用于了解草稿现状再编辑——用户说'草稿里有什么/现在组装成什么样了/在第二段后面加/把某段换掉/还差什么'时先调用这个看现状。默认读当前激活草稿；传 draft_id 可读指定草稿。无激活草稿时返回提示。注意:每段返回 name(草稿内部名, 形如 video_xxx.mp4, 不可直接用于查内容) 和 source_name(原始文件名, 如 VID_xxx.mp4)。要查某段视频讲了什么/内容时, 用 source_name 调 get_resource_detail / get_transcript, 不要用 name。每条轨道还返回 track_id(稳定唯一id) 和 is_empty(是否零片段): 空轨道(is_empty:true, segment_count:0)也可见——用户说'有空轨道/删掉空轨'时直接据此定位; 同名轨道重复时用 track_id 精确指定删除哪一条(传给 delete_track 的 track_id)。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_templates",
                "description": "列出可用的视频模板及每个模板需要填的变量名。用户想用模板快速做视频时先调用这个看有什么模板。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_template",
                "description": "用预设模板一次性生成草稿（自动创建草稿+按模板组装所有场景+保存，返回 draft_id）。适合用户说'用XX模板做个视频'时调用。先用 list_templates 确认模板名和需要的变量。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "template": {"type": "string", "description": "模板文件名（不含.yaml），如 product_intro"},
                        "variables": {"type": "object", "description": "模板变量填充，key 为变量名（如 product_name/demo_video）"}
                    },
                    "required": ["template", "variables"]
                }
            }
        },
    ]

    def _get_internal(endpoint, user_id=None):
        """内部 API 调用 (HTTP 自调, GET). 用法同 _post_internal, 见其注释.
        带 X-Internal-Token 旁路; user_id 透传 (_user_id 查询参数)."""
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

    def _find_analysis(path):
        """按 path 查分析缓存. 容忍正/反斜杠差异 (前端传 /, 缓存可能存 \\).
        多租户: 带 owner=uid 过滤 (只命中本租户或 legacy NULL 的记录)。"""
        a = get_analysis(path, owner=uid)
        if a:
            return a
        # 归一化斜杠后重试
        alt = path.replace('/', '\\') if '/' in path else path.replace('\\', '/')
        return get_analysis(alt, owner=uid)

    def execute_tool(name, args):
        """执行工具调用，返回结果字符串"""
        import re as _re
        nonlocal draft_id
        result = {}

        if name == 'list_resources':
            import main_video_store
            main_path = (main_video_store.get(uid) or {}).get('path')
            items = []
            for path in asset_paths:
                fname = os.path.basename(path)
                ext = os.path.splitext(fname)[1].lower()
                ftype = 'video' if ext in ('.mp4','.mov','.avi','.mkv') else \
                        'image' if ext in ('.jpg','.png','.jpeg') else \
                        'audio' if ext in ('.mp3','.wav','.aac') else 'other'
                analysis = _find_analysis(path)
                analyzed = '已分析' if analysis else '未分析'
                entry = f'{fname} ({ftype}, {analyzed}'
                tags = analysis.get('tags') if analysis else None
                if tags:
                    entry += ', tags: ' + '/'.join(tags)
                if path == main_path:
                    entry += ', 主视频'
                entry += ')'
                items.append(entry)
            result = {'resources': items}

        elif name == 'get_main_video':
            import main_video_store
            info = main_video_store.get(uid)
            if not info:
                result = {'error': '还没有标记任何主视频，请让用户在素材面板里点"设为主视频"'}
            else:
                result = {'name': info['name'], 'path': info['path']}

        elif name == 'search_by_tags':
            keywords = [str(k).strip() for k in (args.get('keywords') or []) if str(k).strip()]
            asset_set = set(asset_paths)
            from asset_store import search_tags as _search_tags
            matches = []
            for m in _search_tags(keywords, type=args.get('type'), owner=uid):
                if m['path'] not in asset_set:
                    continue
                hit = [k for k in keywords if any(k in t for t in m['tags'])]
                matches.append({'name': m['name'], 'tags': m['tags'], 'matched_keywords': hit})
            result = {'matches': matches, 'total_analyzed_scanned': sum(1 for p in asset_paths if _find_analysis(p))}

        elif name == 'search_assets':
            query = str(args.get('query') or '').strip()
            asset_set = set(asset_paths)
            from asset_store import search_text as _search_text
            matches = []
            for m in _search_text(query, type=args.get('type'), owner=uid):
                if m['path'] not in asset_set:
                    continue
                matches.append({
                    'name': m['name'], 'type': m['type'], 'tags': m['tags'],
                    'duration': m['duration'], 'visual_snippet': m['visual'],
                    'transcript_snippet': m['audio_text'],
                })
            result = {'matches': matches}

        elif name == 'get_resource_detail':
            fname = args.get('name', '')
            path = next((p for p in asset_paths if fname in p), None)
            if not path:
                return json.dumps({'error': f'未找到资源: {fname}'}, ensure_ascii=False)
            analysis = _find_analysis(path)
            if not analysis:
                return json.dumps({'error': f'{fname} 尚未分析，请调用 analyze_resource 先分析'}, ensure_ascii=False)
            detail = {'filename': fname, 'path': path}
            meta = analysis.get('meta', {})
            detail['duration'] = meta.get('duration', 0)
            detail['resolution'] = f'{meta.get("width","?")}x{meta.get("height","?")}'
            detail['tags'] = analysis.get('tags', [])
            # VLM
            visual = analysis.get('visual_analysis', '')
            try:
                m = _re.search(r'\{[\s\S]*\}', visual or '')
                if m: detail['visual'] = json.loads(m.group())
            except: detail['visual_raw'] = (visual or '')[:300]
            # ASR
            audio = analysis.get('audio', {})
            if isinstance(audio, dict):
                detail['transcript'] = audio.get('full_text', '')
                detail['segments'] = audio.get('segments', [])
            result = detail

        elif name == 'get_transcript':
            fname = args.get('name', '')
            path = next((p for p in asset_paths if fname in p), None)
            if not path:
                return json.dumps({'error': f'未找到: {fname}'}, ensure_ascii=False)
            analysis = _find_analysis(path)
            if not analysis:
                return json.dumps({'error': f'{fname} 尚未分析，请调用 analyze_resource 先分析'}, ensure_ascii=False)
            audio = analysis.get('audio', {})
            if isinstance(audio, dict):
                result = {
                    'full_text': audio.get('full_text', '(无语音)'),
                    'segments': audio.get('segments', []),
                    # srt 全文: 喂给 add_subtitle 用 (时间轴来自语音识别, 天然同步)
                    'srt': analysis.get('srt', '')
                }
            else:
                result = {'full_text': '(无语音)', 'segments': [], 'srt': ''}

        elif name == 'analyze_resource':
            fname = args.get('name', '')
            path = next((p for p in asset_paths if fname in p), None)
            if not path:
                return json.dumps({'error': f'未找到资源: {fname}'}, ensure_ascii=False)
            try:
                ext = os.path.splitext(path)[1].lower()
                if _ASSET_TYPE_BY_EXT.get(ext) == 'image':
                    from perceive import perceive_image
                    analysis = perceive_image(path)
                else:
                    from perceive import perceive_video
                    analysis = perceive_video(path, do_asr=True, frame_count=4)
                save_analysis(path, analysis, owner=uid)
                result = {
                    'ok': True,
                    'analysis_mode': analysis.get('analysis_mode'),
                    'duration': analysis.get('meta', {}).get('duration', 0),
                }
            except Exception as e:
                result = {'error': str(e)}

        elif name == 'split_shots':
            fname = args.get('name', '')
            path = next((p for p in asset_paths if fname in p), None)
            if not path:
                return json.dumps({'error': f'未找到资源: {fname}'}, ensure_ascii=False)
            try:
                import shot_split
                shots = shot_split.split_shots(path, force=bool(args.get('force', False)))
                result = {
                    'ok': True,
                    'shot_count': len(shots),
                    'shots': [{'index': s['index'], 'start': s['start'], 'end': s['end'], 'duration': s['duration']}
                              for s in shots],
                }
            except Exception as e:
                result = {'error': str(e)}

        elif name == 'create_draft':
            # 草稿复用铁律: 已有激活草稿且非强制新建时, 直接复用, 不新建空草稿 ——
            # 避免 agent 在已有草稿上继续编辑时误调 create_draft 把会话草稿覆盖成空草稿。
            if draft_id and not args.get('force_new'):
                result = {'draft_id': draft_id, 'ok': True, 'note': '复用已有激活草稿（未新建）。要继续编辑直接 add_video/add_subtitle；用户明确要求"新建/重新做"再传 force_new=true。'}
            else:
                r = _post_internal('create_draft', {'width': 1080, 'height': 1920}, user_id=uid)
                if r.get('success') and r.get('output', {}).get('draft_id'):
                    draft_id = r['output']['draft_id']
                    result = {'draft_id': draft_id, 'ok': True, 'note': '已新建空草稿。'}
                else:
                    result = {'error': str(r)}

        elif name == 'use_draft':
            # 切换/确认当前激活草稿。传 draft_id → 切换到该草稿; 不传 → 沿用当前草稿。
            target = args.get('draft_id') or draft_id
            if not target:
                result = {'error': '当前无激活草稿，也没有传入 draft_id。请先 create_draft 新建，或从 list_drafts 选一个传进来。'}
            else:
                # 验证该草稿真实存在 (缓存或磁盘), 切换闭包 draft_id
                r = _get_internal(f'api/draft/timeline/{target}', user_id=uid)
                if not isinstance(r, dict) or not r.get('success'):
                    result = {'error': f"草稿 '{target}' 不存在或读取失败: {(r.get('error') if isinstance(r, dict) else None)}。可用 list_drafts 查看草稿列表。"}
                else:
                    draft_id = target
                    # 冷草稿热身: use_draft 只校验了磁盘存在, 但编辑工具 (delete/add/save) 要求草稿
                    # 在 DRAFT_CACHE 里。服务重启后草稿会变冷, 这里就地载入, 让后续编辑能直接命中。
                    warmed = _warmup_draft(draft_id)
                    try:
                        content = json.loads(r['output'])
                        result = {
                            'draft_id': draft_id, 'ok': True,
                            'duration_s': round((content.get('duration') or 0) / 1_000_000, 3),
                            'track_count': len([t for t in (content.get('tracks') or []) if t.get('segments')]),
                            'note': '已切换/确认当前激活草稿。后续 add_video/add_subtitle/delete_segment/save_draft/render 默认操作它。',
                        }
                        if warmed:
                            result['note'] += ' (冷草稿已从磁盘载入内存, 可编辑)'
                    except Exception as e:
                        # 切换成功但摘要解析失败: 仍算切换成功, 只是没有摘要
                        result = {'draft_id': draft_id, 'ok': True, 'note': f'已切换到草稿（摘要解析失败: {e}）'}

        elif name == 'add_video':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name') or 'video_main'
            target_start = args.get('target_start')
            if target_start is None:
                target_start = _track_end_seconds(did, track_name)
            d = {'draft_id': did, 'video_url': _resolve_asset_url(args.get('url','')),
                 'track_name': track_name, 'target_start': target_start}
            if args.get('relative_index') is not None: d['relative_index'] = args['relative_index']
            if args.get('start') is not None: d['start'] = args['start']
            if args.get('end') is not None: d['end'] = args['end']
            r = _post_internal('add_video', d, user_id=uid)
            result = {'ok': r.get('success', False), 'draft_id': did, 'track_name': track_name, 'target_start': target_start}

        elif name == 'add_subtitle':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            srt = args.get('srt') or ''
            if not srt.strip():
                result = {'error': 'srt 内容为空'}
            else:
                d = {'draft_id': did, 'srt': srt}
                if args.get('time_offset') is not None: d['time_offset'] = args['time_offset']
                if args.get('font_size') is not None: d['font_size'] = args['font_size']
                if args.get('font_color') is not None: d['font_color'] = args['font_color']
                r = _post_internal('add_subtitle', d, user_id=uid)
                result = {'ok': r.get('success', False), 'draft_id': did, 'error': r.get('error')}

        elif name == 'add_text':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name') or 'text_main'
            d = {
                'draft_id': did, 'text': args.get('text',''),
                'start': args.get('start',0), 'end': args.get('end',5),
                'track_name': track_name,
                'font_size': args.get('font_size', 12),
                'font_color': args.get('font_color', '#FFFFFF'),
                'transform_x': args.get('transform_x', 0),
                'transform_y': args.get('transform_y', -0.8),
            }
            if args.get('background_color') is not None: d['background_color'] = args['background_color']
            if args.get('background_alpha') is not None: d['background_alpha'] = args['background_alpha']
            if args.get('intro_animation'):
                valid = _text_animation_names('intro')
                if valid and args['intro_animation'] not in valid:
                    return json.dumps({'error': f"intro_animation 名字不存在: {args['intro_animation']!r}，先调用 list_text_animations(kind='intro') 看合法名字"}, ensure_ascii=False)
                d['intro_animation'] = args['intro_animation']
            if args.get('outro_animation'):
                valid = _text_animation_names('outro')
                if valid and args['outro_animation'] not in valid:
                    return json.dumps({'error': f"outro_animation 名字不存在: {args['outro_animation']!r}，先调用 list_text_animations(kind='outro') 看合法名字"}, ensure_ascii=False)
                d['outro_animation'] = args['outro_animation']
            r = _post_internal('add_text', d, user_id=uid)
            result = {'ok': r.get('success', False), 'draft_id': did, 'track_name': track_name}

        elif name == 'list_text_animations':
            names = _text_animation_names(args.get('kind', 'intro'))
            result = {'kind': args.get('kind'), 'names': names} if names else {'error': '动画名字表加载失败'}

        elif name == 'add_audio':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            d = {'draft_id': did, 'audio_url': _resolve_asset_url(args.get('url','')),
                 'volume': args.get('volume', 0.5)}
            if args.get('start') is not None: d['start'] = args['start']
            if args.get('end') is not None: d['end'] = args['end']
            r = _post_internal('add_audio', d, user_id=uid)
            result = {'ok': r.get('success', False), 'draft_id': did}

        elif name == 'add_image':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name') or 'image_main'
            start = args.get('start')
            if start is None:
                start = _track_end_seconds(did, track_name)
            end = args.get('end')
            if end is None:
                end = start + 3   # 没给时长默认展示 3 秒
            d = {'draft_id': did, 'image_url': _resolve_asset_url(args.get('url','')),
                 'track_name': track_name, 'start': start, 'end': end}
            if args.get('relative_index') is not None: d['relative_index'] = args['relative_index']
            r = _post_internal('add_image', d, user_id=uid)
            result = {'ok': r.get('success', False), 'draft_id': did, 'track_name': track_name, 'start': start, 'end': end}

        elif name == 'delete_segment':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name')
            index = args.get('index')
            segment_id = args.get('segment_id')
            if segment_id is None and (not track_name or index is None):
                result = {'error': '需要提供 segment_id, 或同时提供 track_name 与 index 来定位片段。'}
            else:
                # 冷草稿防御: delete_segment 走 capcut_server → delete_impl._get_script, 要求草稿在
                # DRAFT_CACHE。服务重启后草稿会变冷 (use_draft 会热身, 但 agent 可能跳过它直接删),
                # 这里先确保载入, 否则底层抛 KeyError"不存在于缓存中"让 agent 误判草稿坏了。
                _warmup_draft(did)
                d = {'draft_id': did}
                if track_name is not None: d['track_name'] = track_name
                if index is not None: d['index'] = index
                if segment_id is not None: d['segment_id'] = segment_id
                r = _post_internal('delete_segment', d, user_id=uid)
                if r.get('success'):
                    out = r.get('output', {})
                    result = {
                        'ok': True, 'draft_id': did,
                        'deleted': out if isinstance(out, dict) else {'info': str(out)},
                        'duration_s': out.get('duration_sec') if isinstance(out, dict) else None,
                    }
                else:
                    result = {'ok': False, 'draft_id': did, 'error': r.get('error') or str(r)}

        elif name == 'delete_track':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name')
            track_id = args.get('track_id')
            delete_all = bool(args.get('delete_all'))
            if not track_name and not track_id:
                result = {'error': 'track_name 或 track_id 至少提供一个'}
            else:
                _warmup_draft(did)  # 同 delete_segment: 防冷草稿 KeyError
                d = {'draft_id': did}
                if track_name is not None: d['track_name'] = track_name
                if track_id is not None: d['track_id'] = track_id
                if delete_all: d['delete_all'] = True
                r = _post_internal('delete_track', d, user_id=uid)
                if r.get('success'):
                    out = r.get('output', {}) if isinstance(r.get('output'), dict) else {}
                    # 批量删返回 deleted_tracks(列表), 单删返回 deleted_track; 都透传
                    result = {'ok': True, 'draft_id': did, 'duration_s': out.get('duration_sec')}
                    if 'deleted_tracks' in out:
                        result['deleted_tracks'] = out['deleted_tracks']
                        result['deleted_count'] = out.get('deleted_count')
                    if 'deleted_track' in out:
                        result['deleted_track'] = out['deleted_track']
                    if out.get('ambiguous'):
                        result['ambiguous'] = True
                else:
                    result = {'ok': False, 'draft_id': did, 'error': r.get('error') or str(r)}

        elif name == 'delete_empty_tracks':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            _warmup_draft(did)  # 防冷草稿 KeyError
            d = {'draft_id': did}
            if args.get('track_type'): d['track_type'] = args['track_type']
            if args.get('track_name'): d['track_name'] = args['track_name']
            r = _post_internal('delete_empty_tracks', d, user_id=uid)
            if r.get('success'):
                out = r.get('output', {}) if isinstance(r.get('output'), dict) else {}
                result = {
                    'ok': True, 'draft_id': did,
                    'deleted_tracks': out.get('deleted_tracks', []),
                    'deleted_count': out.get('deleted_count', 0),
                    'duration_s': out.get('duration_sec'),
                }
            else:
                result = {'ok': False, 'draft_id': did, 'error': r.get('error') or str(r)}

        elif name == 'save_draft':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '没有草稿'}, ensure_ascii=False)
            _warmup_draft(did)  # 防冷草稿: save_draft 底层也要草稿在缓存里
            r = _post_internal('save_draft', {'draft_id': did}, user_id=uid)
            result = {'ok': r.get('success', False), 'draft_id': did}

        elif name == 'render':
            did = args.get('draft_id') or draft_id
            if not did: return json.dumps({'error': '没有草稿'}, ensure_ascii=False)
            _post_internal('save_draft', {'draft_id': did}, user_id=uid)
            r = _post_internal(f'render/draft/{did}', user_id=uid)
            if r.get('task_id'):
                result = {'task_id': r['task_id'], 'poll': r.get('poll', ''), 'ok': True, 'draft_id': did}
            else:
                result = {'error': str(r)}

        elif name == 'render_status':
            task_id = args.get('task_id', '')
            if args.get('wait'):
                # 服务端阻塞等待: 每次调用最多等 ~25s, 直到状态/阶段变化或终态再返回.
                # 给 agent 自动监控用 —— 没有它 agent 只能连续空转查询 (LLM 侧无法 sleep).
                waited = 0.0
                r = None
                prev = None
                while waited < 25:
                    r = _get_internal(f"render/status/{task_id}", user_id=uid)
                    cur = (r.get('status'), (r.get('progress') or {}).get('stage')) if isinstance(r, dict) else None
                    if not isinstance(r, dict) or r.get('status') in ('done', 'error') or (prev is not None and cur != prev):
                        break
                    prev = cur
                    time.sleep(2.5)
                    waited += 2.5
                result = r if isinstance(r, dict) else {'error': str(r)}
            else:
                r = _get_internal(f"render/status/{task_id}", user_id=uid)
                result = r if isinstance(r, dict) else {'error': str(r)}

        elif name == 'bsk_run':
            cmd = (args.get('command') or '').strip()
            if not cmd:
                result = {'error': 'command required'}
            else:
                try:
                    import shlex
                    bsk = getattr(config, 'BSK_BIN', None) or 'bsk'
                    p = subprocess.run([bsk] + shlex.split(cmd),
                                       capture_output=True, text=True, encoding='utf-8',
                                       errors='replace', timeout=int(args.get('timeout', 60)),
                                       cwd=HERE)
                    result = {'ok': p.returncode == 0, 'code': p.returncode,
                              'stdout': (p.stdout or '')[-3000:], 'stderr': (p.stderr or '')[-1500:]}
                except FileNotFoundError:
                    result = {'error': 'bsk 未安装 (见 BrowserSkill/AGENT_INSTALL.md)', 'ok': False}
                except subprocess.TimeoutExpired:
                    result = {'error': 'bsk 命令超时', 'ok': False}

        elif name == 'list_drafts':
            r = _get_internal('api/drafts', user_id=uid)
            if isinstance(r, list):
                result = {'drafts': [
                    {'name': d.get('name'), 'id': d.get('id'), 'duration': d.get('duration'),
                     'modified': d.get('modified')} for d in r
                ]}
            else:
                result = {'error': str(r)}

        elif name == 'get_draft_timeline':
            did = args.get('draft_id') or draft_id
            if not did:
                result = {'error': '当前无激活草稿。先 create_draft 新建或让用户在 Drafts 打开一个草稿。'}
            else:
                r = _get_internal(f'api/draft/timeline/{did}', user_id=uid)
                if not isinstance(r, dict) or not r.get('success'):
                    result = {'error': (r.get('error') if isinstance(r, dict) else None) or '读取草稿时间线失败'}
                else:
                    try:
                        content = json.loads(r['output'])
                    except Exception as e:
                        result = {'error': f'解析草稿 json 失败: {e}'}
                    else:
                        # material_id -> 素材摘要 索引 (跨所有素材类型, 仿 TimelinePanel.tsx:86-93)
                        mats = content.get('materials', {}) or {}
                        mat_by_id = {}
                        for mlist in (mats.get('videos'), mats.get('audios'), mats.get('texts'),
                                      mats.get('stickers'), mats.get('images') or mats.get('photos')):
                            for m in (mlist or []):
                                if m.get('id'):
                                    mat_by_id[m['id']] = m

                        def _mat_name(m):
                            # video/text/sticker 用 material_name; audio 用 name; 兜底 basename(path)
                            n = m.get('material_name') or m.get('name')
                            if n:
                                return n
                            p = m.get('path') or m.get('media_path') or m.get('remote_url') or ''
                            return os.path.basename(p) if p else '(无名)'

                        def _mat_source_name(m):
                            # 草稿内的 material_name 是 video_<hash>.mp4 这种内部名, agent 无法用它去
                            # 素材库 (list_resources / get_resource_detail, 那里存的是原始文件名如
                            # VID_20260819_102125.mp4) 查内容。这里补出"原始文件名", 让 agent 能直接
                            # 拿它去 get_resource_detail, 不必先 list_resources 再逐个猜对得上。
                            src = m.get('remote_url') or m.get('path') or m.get('media_path') or ''
                            if src:
                                return os.path.basename(src)
                            return None

                        def _mat_text(m):
                            # 文字素材: content 是 {"styles":[...],"text":"..."} 的 json 串
                            c = m.get('content')
                            if not c:
                                return None
                            try:
                                return (json.loads(c).get('text') if isinstance(c, str) else c.get('text')) or None
                            except Exception:
                                return None

                        tracks_out = []
                        for t in (content.get('tracks') or []):
                            segs = t.get('segments') or []
                            # 不再跳过空轨道: agent 必须能看见空轨(如预建未填充的默认
                            # "video" 轨), 才知道该删它。用 is_empty/segment_count 标注。
                            seg_list = []
                            for s in segs:
                                tr = s.get('target_timerange') or {}
                                start_us = tr.get('start', 0)
                                dur_us = tr.get('duration', 0)
                                m = mat_by_id.get(s.get('material_id'), {})
                                seg_list.append({
                                    'name': _mat_name(m),
                                    'source_name': _mat_source_name(m),  # 原始文件名(素材库里的真名), agent 拿它去 get_resource_detail 查内容; 视/图段有, 文字段无
                                    'type': t.get('type'),            # video/audio/text/sticker/...
                                    'track': t.get('name'),
                                    'start_s': round(start_us / 1_000_000, 3),
                                    'end_s': round((start_us + dur_us) / 1_000_000, 3),
                                    'duration_s': round(dur_us / 1_000_000, 3),
                                    'text': _mat_text(m),             # 仅文字段有值
                                })
                            tracks_out.append({
                                'track': t.get('name'), 'type': t.get('type'),
                                'track_id': t.get('id'),              # 稳定唯一 id, 同名轨道消歧用 (传给 delete_track(track_id=...))
                                'segment_count': len(segs),
                                'is_empty': len(segs) == 0,           # 空轨道标注, agent 一眼看出该删
                                'segments': seg_list,
                            })
                        result = {
                            'draft_id': did,
                            'duration_s': round((content.get('duration') or 0) / 1_000_000, 3),
                            'tracks': tracks_out,
                            'total_segments': sum(t['segment_count'] for t in tracks_out),
                            'source': r.get('source'),   # cache / disk —— 冷草稿时 disk
                        }

        elif name == 'list_templates':
            r = _get_internal('api/templates')
            result = {'templates': r} if isinstance(r, list) else {'error': str(r)}

        elif name == 'run_template':
            r = _post_internal('api/templates/render', {
                'template': args.get('template', ''),
                'variables': args.get('variables', {}) or {},
                'render': False,
            })
            if r.get('draft_id'):
                draft_id = r['draft_id']
                result = {'ok': True, 'draft_id': draft_id}
            else:
                result = {'error': str(r)}

        else:
            # 模型偶尔会吐出空/未知的 tool_call name (观察到 qwen 多轮工具调用时出现过).
            # 明确报错而不是静默回空 dict, 否则模型会把"空结果"当成"调用成功但没内容"
            # 继而自己编一个看起来合理的答案 —— 这比直接报错更危险.
            return json.dumps({'error': f'未知工具: {name!r}'}, ensure_ascii=False)

        # 确保 result 是 dict, 再序列化
        if not isinstance(result, dict):
            result = {'raw': str(result)[:500]}
        return json.dumps(result, ensure_ascii=False)

    def generate():
        nonlocal draft_id
        client = _OAI(api_key=QWEN_KEY, base_url=QWEN_URL)

        # 先把 conversation_id 发给前端 (新建会话时前端还不知道 id, 后续消息才能带上它)
        yield f"data: {json.dumps({'conversation_id': conversation_id}, ensure_ascii=False)}\n\n"

        # 简洁 system prompt (只列资源名，不列详情)
        resource_names = ', '.join(os.path.basename(p) for p in asset_paths) if asset_paths else '无'
        system = f"""你是一个 AI 视频编辑助手。

已上传资源: {resource_names}
当前草稿: {draft_id or '无'}

重要规则:
1. 回答视频内容/文案问题时，调用 get_resource_detail 或 get_transcript 查询，不要编造
2. 资源未分析时，直接调用 analyze_resource 自己触发分析，不要让用户去点界面按钮；分析完再继续
2a. 【修改前必须先查询·铁律】对草稿做任何修改前（加段/换段/删段/补字幕/加 B-roll/加文字/换素材，乃至 create_draft 之后接 add_video），必须先调用 get_draft_timeline 查当前时间线真实现状（各轨素材、起止秒、总时长、已有字幕/B-roll 轨），基于真实现状再决策——绝不凭记忆或之前对话猜草稿里有什么。这一步不可省略、不可用记忆代替，否则会基于不存在的草稿状态做修改导致操作静默失效（实测出现过"以为加了字幕和 B-roll，实际只有主视频"的问题）。先查后改，每次修改前都查一次
3. 引用语音内容时带时间戳；但时间戳/文案必须直接来自工具返回的 segments，禁止自己编造或推测
4. 保持简洁，中文回复
5. 制作视频的标准流程: create_draft → add_video(url, start, end) → [可选 add_audio/add_image] → add_subtitle(字幕) → save_draft → render
5a. 【草稿复用铁律】当前已有激活草稿时（system prompt 顶部“当前草稿”不是“无”，或对话里已建过草稿），继续编辑一律用 add_video/add_subtitle/use_draft 等编辑工具直接接着做，**不要调 create_draft**——create_draft 在已有草稿时只会复用当前草稿、不会新建，调了也是空跑还会让语义混乱。只有用户明确说“新建草稿/重新做一个/另起一个/不要这个了”时，才调 create_draft(force_new=true) 真正新建空草稿；判断不准时优先用 use_draft 确认当前草稿现状再继续
5b. 【字幕铁律】加字幕只能用 add_subtitle 传 SRT 全文(从 get_transcript 返回的 srt 字段拿), 绝不用 add_text 排字幕——add_text 拿不到真实语音时间点, 排出来必然不同步。用户说"字幕不同步/重新解析过要更新字幕"时: 重新 get_transcript 拿最新 srt, 重建草稿(或确认旧草稿字幕后重做), 再渲染
6. 当用户要求"渲染/导出/出片/出视频"时，保存草稿后必须调用 render 工具提交渲染，不要只说"可以渲染了"；提交后【默认自动监控】：立即用 render_status(wait=true) 查询，未完成就继续调用（每次服务端会等~25秒），直到 done/error，然后直接告知用户结果（done 报 mp4 文件名，error 报错误摘要），不要问"需要我帮你监控吗"，也不要中途汇报无意义的进度。6b. 【渲染结果铁律】渲染完成后，mp4 文件名/路径/大小必须且只能来自 render_status 返回的 mp4_name/mp4 字段——绝不允许凭草稿 id、时间戳或猜测编造文件名（如"草稿 xxx.mp4"）或路径（如"Downloads/.just_animate/"）；用户问"看看结果/出片了吗/在哪个文件"时，必须调用 render_status 取真实结果再回复，没拿到就如实说"还没出"，不得假装成功
7. add_video 的 start/end 是源视频的截取起止秒数（如 start=0, end=10 取前10秒）；target_start 才是成片时间轴上的位置，不填会自动接在同名轨道末尾
7b. "主视频"（贯穿全片的主体素材）始终放在 add_video 默认的 'video_main' 轨道，按顺序多次调用即可自动接龙；"补充素材/花絮/B-roll"（叠加在主视频某个时间点上方的片段）必须用不同的 track_name（如 'broll_1'）并显式指定 target_start=该素材要出现的成片秒数，同时给一个比主视频轨道更高的 relative_index（如 1），否则会被主视频盖住或跟主视频撞在同一条轨道上
8. 用户想用模板快速做视频时，先 list_templates 看有哪些模板和需要填的变量，再 run_template
9. 【发布视频到平台】用户说"发布/发到视频号/抖音/小红书"时，用 bsk_run 驱动浏览器完成（需用户已装 BrowserSkill 扩展并登录过平台）。流程：
   a. bsk_run "session start --no-focus" 拿 4 位会话 id（记为 SID，后续所有命令都带 --session SID）
   b. bsk_run "navigate <平台发布页> --session SID"。平台发布页：视频号 https://channels.weixin.qq.com/platform/post/create ；抖音 https://creator.douyin.com/creator-micro/content/upload ；小红书 https://creator.xiaohongshu.com/publish/publish
   c. bsk_run "snapshot --session SID" 看页面结构拿 @eN 引用。若跳登录页 → bsk_run "request-help --session SID --prompt 请扫码/登录后点完成 --timeout 5m" 让用户处理
   d. 注入视频文件（bsk 无文件上传命令，用 evaluate + DataTransfer）：先 snapshot 找到上传区的 <input type=file>（通常藏在"拖拽上传"区块内，snapshot 看不到就 get-html 找），然后 evaluate 执行：
      (async()=>{{const r=await fetch('http://<本机IP>:9010/api/video/serve?path=<mp4绝对路径URL编码>');const b=await r.blob();const f=new File([b],'<文件名>.mp4',{{type:'video/mp4'}});const d=new DataTransfer();d.items.add(f);const i=document.querySelector('input[type=file]');i.files=d.files;i.dispatchEvent(new Event('change',{{bubbles:true}}));return 'ok'}})()
      （本机IP用 192.168.8.133；mp4 路径取 render 任务结果里的 mp4_path）
   e. 等页面解析完视频后再 snapshot，填标题/话题（fill），必要时 request-help 处理验证码
   f. 点发布（click），snapshot 确认发布成功，最后必须 bsk_run "session stop SID"
   g. 每步失败最多重试一次；连续失败就 request-help 或如实告知用户卡在哪一步
9. 用户问"有哪些草稿/之前做的视频"时调用 list_drafts
9b. 用户问"草稿里有什么/现在什么样子/组装到哪了"，或要在已有草稿上继续编辑（加段/换段/补字幕）前，先调用 get_draft_timeline 看当前时间线现状（各轨素材、起止秒、总时长），基于真实现状再决策，不要凭记忆猜草稿里有什么
10. 任何工具调用返回 error 或结果为空时，必须原样告知用户失败原因，绝不能假装成功或编造一个看起来合理的分析结果顶替
11. 按内容/主题找素材(如"找一个有山的视频")时，优先用 search_by_tags 关键词粗筛(SQLite 索引查询，不耗 token，毫秒级返回)，缩小候选范围后再对具体文件调用 get_resource_detail/get_transcript 看全文细节确认；标签查不到、或用户用整句话描述要找的内容时，改用 search_assets 全文检索(搜文件名/画面描述/口播文案/标签)兜底；素材没几个再退化成直接读全部资源详情
12. 用户要求"分镜/拆镜头"时调用 split_shots，拆完告诉用户拆出了几个镜头，各镜头的起止时间；不要自己编造镜头数量
13. 用户说"主视频/这次的视频/最新录的"而不指名具体文件时，先调用 get_main_video 解析出实际文件名再继续，别直接猜或反复问用户文件名
14. add_text 不止能加字幕，也能加标题/水印/角标等文字标识：字幕用默认 track_name='text_main'、transform_y=-0.8（画面下方）；独立的标题/标识要换一个不同的 track_name（如 'label_1'）避免和字幕叠压覆盖，并按需调整 transform_x/transform_y 到画面其他位置（如 0.8 靠上做标题）；background_alpha 只是纯色块透明度，不是模糊/毛玻璃特效，别答应用户做不到的效果；要用入场/出场动画时先调 list_text_animations 查真实存在的动画名，不要凭印象瞎填"""

        # 多轮记忆: 历史里只取 user/assistant 的文本内容喂给 LLM 当上下文
        # (tool 卡片只是给用户看的执行细节, 不需要还原 tool_call_id 链路); 截断最近 20 条防止越聊越贵
        history_for_llm = [
            {'role': m['role'], 'content': m['content']}
            for m in prior_messages
            if m.get('role') in ('user', 'assistant') and m.get('content')
        ][-20:]

        messages = [
            {"role": "system", "content": system},
            *history_for_llm,
            {"role": "user", "content": message},
        ]

        # 本轮展示用的消息日志 (供存回 chat_store, 前端历史/切换会话时按这个渲染)
        turn_log = list(prior_messages)
        turn_log.append({'role': 'user', 'content': message})

        # 多轮工具调用 (最多 12 轮 — 覆盖 analyze→list→detail→create→add(video/text/audio/image)→save→render→status 完整链路)
        for _ in range(12):
            # LLM 提供商偶发 Connection error / 超时: 重试 3 次 (递增退避), 避免一次抖动就中断整轮对话
            resp = None
            last_err = None
            for attempt in range(3):
                try:
                    resp = client.chat.completions.create(
                        model=LLM_MODEL,
                        messages=messages,
                        tools=TOOLS,
                        tool_choice="auto",
                        max_tokens=2000,
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))   # 2s, 4s
            if resp is None:
                # 三次都失败 — 渲染等已在后台 worker 线程独立进行, 这里如实告知, 不假装成功
                hint = '（若正在渲染，任务仍在后台继续，可用「看看结果」或 Tasks 面板查看进度）'
                yield f"data: {json.dumps({'text': f'模型连接失败: {last_err}\\n{hint}'}, ensure_ascii=False)}\n\n"
                turn_log.append({'role': 'assistant', 'content': f'模型连接失败: {last_err}'})
                break

            msg = resp.choices[0].message
            messages.append(msg.model_dump())

            # 有文本输出 → 流式发送
            if msg.content:
                yield f"data: {json.dumps({'text': msg.content}, ensure_ascii=False)}\n\n"
                turn_log.append({'role': 'assistant', 'content': msg.content})

            # 没有工具调用 → 结束
            if not msg.tool_calls:
                break

            # 执行工具调用
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments or '{}')
                tool_result_str = execute_tool(fn_name, fn_args)
                # tool_result_str 是 JSON 字符串
                tool_result_obj = json.loads(tool_result_str)

                # 通知前端工具执行结果
                yield f"data: {json.dumps({'tool': fn_name, 'args': fn_args, 'result': tool_result_obj}, ensure_ascii=False)}\n\n"
                turn_log.append({
                    'role': 'tool', 'content': f'Invoked: {fn_name}',
                    'toolDetails': {'tool': fn_name, 'args': fn_args, 'result': tool_result_obj},
                })

                # 如果是 create_draft，更新 draft_id
                if fn_name in ('create_draft', 'run_template'):
                    new_id = tool_result_obj.get('draft_id') or tool_result_obj.get('output', {}).get('draft_id')
                    if new_id:
                        draft_id = new_id
                        yield f"data: {json.dumps({'draft_id': new_id}, ensure_ascii=False)}\n\n"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result_str
                })

            # 继续下一轮，让 LLM 基于工具结果继续回答

        try:
            chat_store.save_messages(conversation_id, turn_log, draft_id, user_id=uid)
        except Exception as e:
            print('[chat] 会话保存失败: %s' % e, flush=True)

        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


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


def _resolve_asset_url(u):
    """把 LLM 传的素材标识解析成本地绝对路径 (正斜杠, pyJianYingDraft/ffprobe 都认).
    LLM 有时只传文件名 (如 'VID_20260819_102125.mp4') 而不是完整路径 —— 这种裸名会让
    VectCutAPI 的 update_media_metadata ffprobe 探不到文件 → 素材时长 0 → 主视频
    segment 零时长 → 渲染出来黑屏无声 (叠加段因显式传了 start/end 不受影响).
    规则: http(s)/file 协议不动; 已存在的绝对路径不动; 其余按文件名去上传目录找.
    模块级 (原为 generate() 闭包; 提到模块级以便 /api/draft/<id>/add-asset 复用,
    generate() 闭包内同名调用已删, 一并走这里)."""
    if not u:
        return u
    if u.startswith(('http://', 'https://', 'file://')):
        return u
    if os.path.isabs(u) and os.path.isfile(u):
        return u.replace('\\', '/')
    base = os.path.basename(u.replace('\\', '/'))
    for d in (config.UPLOAD_DIR, config.GUI_UPLOAD_DIR):
        cand = os.path.join(d, base)
        if os.path.isfile(cand):
            resolved = cand.replace('\\', '/')
            if resolved != u:
                print('[resolve_asset_url] %r -> %r' % (u, resolved), flush=True)
            return resolved
    return u


def _track_end_seconds(draft_id, track_name):
    """查草稿里某条轨道当前已经铺到多少秒 —— add_video 不传 target_start 时用这个自动
    接龙, 不用 agent 自己心算已加了多少段/每段多长(算错了同一时间点会撞车覆盖).
    模块级 (原为 generate() 闭包; 提到模块级以便 /api/draft/<id>/add-asset 复用)."""
    r = _post_internal('query_script', {'draft_id': draft_id}, user_id=current_user_id())
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
