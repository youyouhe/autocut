# render_service.py — 独立 Render 服务 (Win10, 与 web 后端分离部署)
#
# 目标架构里 web 后端 (render_server.py) 部署在 Linux, render 部署在 Win10。
# 本服务即 Win10 上的纯 render 工人:
#   - 收 zip 草稿 → safe_zip_extract → render_driver 本地剪映 GUI 渲染 → 回传 mp4
#   - 桌面池 + render_pool_worker (从 render_server.py 搬来)
#   - X-Render-Token 轻量认证 (只验调用方是可信 web 后端, 不知用户; 租户隔离已在 web 后端完成)
#
# 依赖约束 (关键): 本服务只 import config + task_store + render_driver(subprocess 调)。
#   不 import capcut_server / 不 import perceive / 不带 web 认证层 / 不带 agent 工具层。
#   必须是全新 Flask app (绝不能 from capcut_server import app, 否则会把 pyJianYingDraft +
#   草稿编辑拖进 Win10 render 节点)。本服务不需要 UPLOAD_DIR (草稿 zip 自包含素材)。
#
# env 集 (全是 config.py 已有的 env, 零新增):
#   JY_APP_BASE / JY_DRAFT_ROOT / VIDEOS_DIR / DESKTOP_NAMES /
#   PRESET_W / PRESET_H / RENDER_TIMEOUT / TASK_TTL /
#   RENDER_SERVICE_HOST / RENDER_SERVICE_PORT / RENDER_SERVICE_TOKEN
import os
import sys
import json
import time
import uuid
import queue
import zipfile
import shutil
import threading
import subprocess

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config
import task_store

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# === 路径 ===
# render_service 不读 UPLOAD_DIR 的业务素材 (zip 自包含), 但 zip 解包需要一个临时目录,
# 复用 config.UPLOAD_DIR 作为解包根 (若不存在则就地建 render_extract/)。
VIDEOS = config.VIDEOS_DIR
DRAFT_ROOT = config.DRAFT_ROOT
EXTRACT_ROOT = config.UPLOAD_DIR  # zip 草稿解包根 (与 web 后端共用 render_uploads 即可)


# === 轻量认证: X-Render-Token ===
def _check_render_token():
    """校验 X-Render-Token。未配置 token (env 未设) 时放行 (同机开发/本地无网络威胁)。
    配置后必须带正确 header 才放行 —— 防止 render_service 跨网暴露被任意调用。"""
    expected = config.RENDER_SERVICE_TOKEN
    if not expected:
        return True  # 未配置 = 不启用认证 (仅同机/内网开发场景)
    tok = request.headers.get('X-Render-Token', '')
    # 恒定时间比较防时序侧信道
    import hmac
    return hmac.compare_digest(tok, expected)


@app.before_request
def _token_gate():
    # 健康检查放行 (不验 token, 供 web 后端探活)
    if request.method == 'OPTIONS' or request.path == '/health':
        return None
    if not _check_render_token():
        return jsonify({'error': 'invalid render token', 'code': 'UNAUTHORIZED'}), 401


# === Render 池基础设施 (从 render_server.py 搬来) ===
import queue
tasks = {}
TASK_LOCK = threading.Lock()
RENDER_QUEUE = queue.Queue()


def _persist(task_id):
    """落盘任务状态 (task_store, 失败不阻塞)."""
    try:
        with TASK_LOCK:
            t = tasks.get(task_id)
        if t:
            task_store.upsert(t)
    except Exception:
        pass


def _new_task(task_id, **fields):
    """创建任务: 写内存 dict + 落盘."""
    with TASK_LOCK:
        tasks[task_id] = dict(fields)
        tasks[task_id].setdefault('created', time.time())
        tasks[task_id]['task_id'] = task_id
    _persist(task_id)
    return tasks[task_id]


# 桌面池配置 (桌面数 = 并行渲染数, 见 config.DESKTOP_NAMES)
DESKTOP_NAMES = config.DESKTOP_NAMES
desktop_pool = {}  # desk_name -> {busy}

DESKTOP_LOCK = threading.Lock()


def acquire_desktop(timeout=600):
    """获取空闲桌面 (阻塞等, 加锁防竞争). 返回 desk_name 或 None."""
    start = time.time()
    while time.time() - start < timeout:
        with DESKTOP_LOCK:
            for desk, info in desktop_pool.items():
                if not info['busy']:
                    info['busy'] = True
                    return desk
        time.sleep(1)
    return None


def release_desktop(desk):
    if desk in desktop_pool:
        desktop_pool[desk]['busy'] = False


def _parse_progress_line(line):
    """解析 '[PROGRESS] {json}' 行, 返回 dict 或 None."""
    s = line.strip()
    if not s.startswith('[PROGRESS] '):
        return None
    try:
        return json.loads(s[len('[PROGRESS] '):])
    except (ValueError, TypeError):
        return None


def _stream_process(cmd, task_id):
    """运行 cmd, 流式解析 '[PROGRESS] {json}' 行更新任务进度.
    返回 (returncode, stdout, stderr). 用读线程而非 select (Windows 不支持管道 select)."""
    proc = subprocess.Popen(
        cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace', bufsize=1)
    out_buf, err_buf = [], []
    out_lock, err_lock = threading.Lock(), threading.Lock()

    def _update_progress(line):
        data = _parse_progress_line(line)
        if data is None:
            return
        with TASK_LOCK:
            t = tasks.get(task_id)
            if t is None:
                return
            t['progress'] = {
                'stage': data.get('stage'),
                'pct': data.get('pct'),
                'elapsed': data.get('elapsed'),
                'temp_bytes': data.get('temp_bytes'),
            }

    def _read(stream, buf, lock, is_stdout):
        for line in iter(stream.readline, ''):
            if not line:
                break
            with lock:
                buf.append(line)
            if is_stdout:
                _update_progress(line)

    tout = threading.Thread(target=_read, args=(proc.stdout, out_buf, out_lock, True), daemon=True)
    terr = threading.Thread(target=_read, args=(proc.stderr, err_buf, err_lock, False), daemon=True)
    tout.start(); terr.start()

    try:
        proc.wait(timeout=config.RENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise

    tout.join(timeout=5)
    terr.join(timeout=5)
    return proc.returncode, ''.join(out_buf), ''.join(err_buf)


def _run_render_streaming(task_id, argv):
    """流式运行 render_driver, 解析 '[PROGRESS] {json}' 行更新任务进度.
    返回 (returncode, stdout_tail, stderr_tail)."""
    cmd = [sys.executable, os.path.join(HERE, 'render_driver.py')] + argv
    return _stream_process(cmd, task_id)


def render_pool_worker():
    """worker 线程: 取队列任务 → 获取桌面 → 渲染 (每次自己 start+kill 剪映)."""
    while True:
        task_id, draft_dir, draft_name = RENDER_QUEUE.get()
        desk = acquire_desktop()
        if not desk:
            with TASK_LOCK:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = 'no free desktop'
            _persist(task_id)
            continue
        with TASK_LOCK:
            tasks[task_id]['status'] = 'rendering'
            tasks[task_id]['desktop'] = desk
            tasks[task_id]['started_at'] = time.time()
        _persist(task_id)
        try:
            t0 = time.time()
            # render_driver 自己启动剪映到 desk 桌面, 渲染完 kill 自己的剪映
            code, stdout_tail, stderr_tail = _run_render_streaming(
                task_id, ['render-draft', draft_dir, '--desktop', '--desktop-name', desk])
            dt = time.time() - t0
            with TASK_LOCK:
                tasks[task_id]['duration'] = dt
                if code == 0:
                    mps = sorted([f for f in os.listdir(VIDEOS)
                                  if f.endswith('.mp4') and (f.startswith(draft_name) or f.startswith('rd'))],
                                 key=lambda f: os.path.getmtime(os.path.join(VIDEOS, f)), reverse=True)
                    if mps:
                        tasks[task_id]['status'] = 'done'
                        tasks[task_id]['mp4_path'] = os.path.join(VIDEOS, mps[0])
                        tasks[task_id]['mp4_name'] = mps[0]
                    else:
                        tasks[task_id]['status'] = 'error'
                        tasks[task_id]['error'] = 'no mp4'
                else:
                    tasks[task_id]['status'] = 'error'
                    tasks[task_id]['error'] = (stderr_tail + stdout_tail)[-800:]
            _persist(task_id)
        except subprocess.TimeoutExpired:
            with TASK_LOCK:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = 'render timeout (%ds)' % config.RENDER_TIMEOUT
            _persist(task_id)
        except Exception as e:
            with TASK_LOCK:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = str(e)
            _persist(task_id)
        finally:
            release_desktop(desk)
            RENDER_QUEUE.task_done()


def enqueue_render(task_id, draft_dir, draft_name):
    RENDER_QUEUE.put((task_id, draft_dir, draft_name))


# === 任务恢复 / 清理 (从 render_server.py 搬来) ===
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
    """启动时从 SQLite 恢复任务历史; 未完成 (queued/rendering) 的标记为中断."""
    saved = task_store.load_all()
    with TASK_LOCK:
        for tid, t in saved.items():
            if t.get('status') in ('queued', 'rendering'):
                t['status'] = 'error'
                t['error'] = '服务重启, 任务中断'
                task_store.upsert(t)
            tasks.setdefault(tid, t)
    print('[render_service] 恢复 %d 条历史任务' % len(saved), flush=True)


def find_draft_dir(extract_dir):
    """在解压目录找含 draft_content.json 的文件夹"""
    for root, dirs, files in os.walk(extract_dir):
        if 'draft_content.json' in files:
            return root
    return None


# === 路由 ===
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'render_service',
                    'videos_dir': VIDEOS, 'desktops': list(desktop_pool.keys())})


@app.route('/render', methods=['POST'])
def render():
    """提交 zip 草稿, 异步渲染, 返回 task_id.
    multipart: draft=<zip文件>  (zip 内含 draft_content.json 的草稿文件夹)
    可选 form: draft_name=自定义草稿名
    可选 form: _user_id=租户归属 (透传落盘, 供 web 后端影子任务对应; 本服务不做租户校验)
    """
    f = request.files.get('draft')
    if not f:
        return jsonify({'error': "no 'draft' file in multipart"}), 400
    task_id = uuid.uuid4().hex[:8]
    draft_name = (request.form.get('draft_name') or ('rd_' + task_id))
    user_id = request.form.get('_user_id') or ''
    extract_dir = os.path.join(EXTRACT_ROOT, draft_name)
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)
    zip_path = os.path.join(EXTRACT_ROOT, draft_name + '.zip')
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
    _new_task(task_id, status='queued', draft_name=draft_name, draft_dir=draft_dir,
              user_id=user_id or None)
    enqueue_render(task_id, draft_dir, draft_name)
    return jsonify({'task_id': task_id, 'status': 'queued',
                    'poll': '/render/status/%s' % task_id})


@app.route('/render/status/<task_id>', methods=['GET'])
def render_status(task_id):
    with TASK_LOCK:
        t = tasks.get(task_id)
    if not t:
        return jsonify({'error': 'unknown task'}), 404
    # 不回 draft_dir (内部路径, 调用方无需知道)
    return jsonify({k: v for k, v in t.items() if k != 'draft_dir'})


@app.route('/render/download/<task_id>', methods=['GET'])
def render_download(task_id):
    with TASK_LOCK:
        t = tasks.get(task_id)
    if not t:
        return jsonify({'error': 'unknown task'}), 404
    if t.get('status') != 'done':
        return jsonify({'error': 'not done', 'status': t.get('status')}), 400
    return send_file(t['mp4_path'], as_attachment=True, download_name=t.get('mp4_name', 'output.mp4'))


@app.route('/render/list', methods=['GET'])
def render_list():
    with TASK_LOCK:
        items = [{'task_id': k, 'status': v.get('status'),
                  'mp4': v.get('mp4_name'), 'draft': v.get('draft_name'),
                  'duration': v.get('duration'), 'error': v.get('error'),
                  'progress': v.get('progress'), 'created': v.get('created'),
                  'user_id': v.get('user_id')}
                 for k, v in tasks.items()]
    return jsonify(items)


@app.route('/render/draft/<draft_id>', methods=['POST'])
def render_by_draft_id(draft_id):
    """按本地草稿文件夹名渲染 (render_service 本机已有的草稿目录).
       draft_id 为 DRAFT_ROOT 下的文件夹名。"""
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
    if not os.path.isdir(draft_dir):
        return jsonify({'error': 'draft folder not found: %s' % draft_id}), 404
    content_json = 'draft_content.json' if os.path.exists(os.path.join(draft_dir, 'draft_content.json')) \
                   else ('draft_info.json' if os.path.exists(os.path.join(draft_dir, 'draft_info.json')) else None)
    if not content_json:
        return jsonify({'error': 'not a valid jianying draft (no draft_content.json / draft_info.json)'}), 400
    user_id = (request.form.get('_user_id') or (request.json or {}).get('_user_id') or '') if request else ''
    task_id = uuid.uuid4().hex[:8]
    _new_task(task_id, status='queued', draft_name=draft_id, draft_dir=draft_dir,
              user_id=user_id or None)
    enqueue_render(task_id, draft_dir, draft_id)
    return jsonify({'task_id': task_id, 'status': 'queued',
                    'poll': '/render/status/%s' % task_id})


if __name__ == '__main__':
    os.makedirs(EXTRACT_ROOT, exist_ok=True)
    print('render_service on http://%s:%d (多桌面: %s)' % (
        config.RENDER_SERVICE_HOST, config.RENDER_SERVICE_PORT, DESKTOP_NAMES), flush=True)
    if not config.RENDER_SERVICE_TOKEN:
        print('[render_service] WARN: RENDER_SERVICE_TOKEN 未配置, 跨网暴露将无认证保护', flush=True)

    # 恢复历史任务
    restore_tasks()
    # 初始化桌面池 (不预启动剪映; render_driver 每次 --desktop-name 自己 start+kill)
    for desk in DESKTOP_NAMES:
        desktop_pool[desk] = {'busy': False}
    for i in range(len(DESKTOP_NAMES)):
        threading.Thread(target=render_pool_worker, daemon=True).start()
    print('[pool] %d 个 worker 启动' % len(DESKTOP_NAMES), flush=True)

    # 过期任务清理
    threading.Thread(target=task_cleanup_loop, daemon=True).start()

    # 剪映强更看守 (env UPGRADE_WATCHDOG=0 关闭). 见 upgrade_watchdog.py 模块头.
    if os.environ.get('UPGRADE_WATCHDOG', '1') != '0':
        import upgrade_watchdog
        upgrade_watchdog.start()

    try:
        from waitress import serve
        print('[server] waitress 生产服务器', flush=True)
        serve(app, host=config.RENDER_SERVICE_HOST, port=config.RENDER_SERVICE_PORT,
              threads=8, channel_timeout=900)
    except ImportError:
        print('[server] WARN: 未安装 waitress, 回退 Flask 开发服务器 (pip install waitress)', flush=True)
        app.run(host=config.RENDER_SERVICE_HOST, port=config.RENDER_SERVICE_PORT, debug=False)
