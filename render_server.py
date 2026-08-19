# render_server.py — RESTful 渲染服务
# 接收 zip 草稿 → 真后台渲染 (--desktop) → 返回 mp4
# 融合 VectCutAPI 的编辑端点 (add_video/add_text/...) → 完整生产线
import os, sys, json, zipfile, threading, uuid, time, subprocess, shutil
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

import config
from flask import Flask, request, jsonify, send_file, send_from_directory
from werkzeug.utils import secure_filename

import task_store

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

# === 多桌面渲染池 ===
# N 个独立桌面, 每个跑一个剪映实例, 支持并行渲染
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
            tasks[task_id]['progress'] = {
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


# 启动 N 个 worker (对应桌面数)
# 注意: 桌面池在服务启动时初始化 (见 __main__)


def find_draft_dir(extract_dir):
    """在解压目录找含 draft_content.json 的文件夹"""
    for root, dirs, files in os.walk(extract_dir):
        if 'draft_content.json' in files:
            return root
    return None


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
    print('[task_store] 恢复 %d 条历史任务' % len(saved), flush=True)


@app.route('/render', methods=['POST'])
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
    _new_task(task_id, status='queued', draft_name=draft_name, draft_dir=draft_dir)
    enqueue_render(task_id, draft_dir, draft_name)
    return jsonify({'task_id': task_id, 'status': 'queued',
                    'poll': '/render/status/%s' % task_id})


@app.route('/render/status/<task_id>', methods=['GET'])
def render_status(task_id):
    with TASK_LOCK:
        t = tasks.get(task_id)
    if not t:
        return jsonify({'error': 'unknown task'}), 404
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
        return jsonify([{'task_id': k, 'status': v.get('status'),
                         'mp4': v.get('mp4_name'), 'draft': v.get('draft_name'),
                         'duration': v.get('duration'), 'error': v.get('error'),
                         'progress': v.get('progress'), 'created': v.get('created')}
                        for k, v in tasks.items()])


@app.route('/render/draft/<draft_id>', methods=['POST'])
def render_by_draft_id(draft_id):
    """按草稿渲染. draft_id 可为:
       - 草稿文件夹名 (root_meta_info.json 的 folder, 如 '8月11日' 或 'JYRender_0')
       - 剪映 draft_id (UUID, 如 'D9C6B7ED-...') — 兜底, 仅当存在同名文件夹时.
    查找 DRAFT_ROOT/draft_id 或 VectCutAPI 目录/draft_id."""
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
    task_id = uuid.uuid4().hex[:8]
    _new_task(task_id, status='queued', draft_name=draft_id, draft_dir=draft_dir)
    enqueue_render(task_id, draft_dir, draft_id)
    return jsonify({'task_id': task_id, 'status': 'queued',
                    'poll': '/render/status/%s' % task_id})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'render_server', 'videos_dir': VIDEOS})


# ============================================================ 前端 API
@app.route('/api/perceive', methods=['POST'])
def api_perceive_by_path():
    """按文件路径分析视频。内存缓存优先 (O(1))，无则调 VLM 并缓存。"""
    from memory_store import get_analysis, has_analysis, save_analysis
    data = request.json or {}
    path = data.get('path', '')
    force = data.get('force', False)
    if not path or not os.path.exists(path):
        return jsonify({'error': f'文件不存在: {path}'}), 400
    if not config.is_allowed_path(path):
        return jsonify({'error': 'path not allowed'}), 403

    # 内存查询 (O(1) dict)
    if not force:
        cached = get_analysis(path)
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
        save_analysis(path, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/perceive/cached', methods=['GET'])
def api_perceive_cached():
    """内存查询 (不读磁盘)"""
    from memory_store import has_analysis, get_analysis
    path = request.args.get('path', '')
    if not path:
        return jsonify({'cached': False})
    if has_analysis(path):
        return jsonify({'cached': True, 'result': get_analysis(path)})
    return jsonify({'cached': False})


@app.route('/api/drafts', methods=['GET'])
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
                cover = d.get('draft_cover', '')
                # 封面路径转 URL
                cover_url = None
                if cover and os.path.exists(cover):
                    cover_url = f'/api/video/serve?path={os.path.basename(cover)}&folder={os.path.basename(fold)}'
                elif cover and not os.path.isabs(cover):
                    # 相对路径 draft_cover.jpg
                    abs_cover = os.path.join(fold, cover)
                    if os.path.exists(abs_cover):
                        cover_url = f'/api/cover?folder={os.path.basename(fold)}'

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

    # 按修改时间倒序
    drafts.sort(key=lambda x: x.get('modified', 0), reverse=True)
    return jsonify(drafts)


@app.route('/api/cover', methods=['GET'])
def api_draft_cover():
    """获取草稿封面图"""
    folder = request.args.get('folder', '')
    if not config.safe_folder_name(folder):
        return '', 400
    cover_path = os.path.join(DRAFT_ROOT, folder, 'draft_cover.jpg')
    if os.path.exists(cover_path):
        return send_file(cover_path, mimetype='image/jpeg')
    return '', 404


@app.route('/api/drafts/<folder>', methods=['DELETE'])
def api_delete_draft(folder):
    """删除草稿（文件夹 + root_meta 同步）"""
    if not config.safe_folder_name(folder):
        return jsonify({'error': 'invalid folder name'}), 400
    draft_path = os.path.join(DRAFT_ROOT, folder)
    if not os.path.isdir(draft_path):
        return jsonify({'error': 'not found'}), 404
    try:
        shutil.rmtree(draft_path)
        # 同步 root_meta
        root_meta = os.path.join(DRAFT_ROOT, 'root_meta_info.json')
        if os.path.exists(root_meta):
            with open(root_meta, encoding='utf-8') as fh:
                m = json.load(fh)
            m['all_draft_store'] = [d for d in m.get('all_draft_store', []) if os.path.basename(d.get('draft_fold_path', '')) != folder]
            m['draft_ids'] = len(m['all_draft_store'])
            with open(root_meta, 'w', encoding='utf-8') as fh:
                json.dump(m, fh, ensure_ascii=False)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/memory/stats', methods=['GET'])
def api_memory_stats():
    """内存缓存统计"""
    from memory_store import video_cache_stats, list_all_analysis
    stats = video_cache_stats()
    stats['analysis_count'] = len(list_all_analysis())
    return jsonify(stats)


@app.route('/api/memory/analysis', methods=['GET'])
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
    cors = {'Access-Control-Allow-Origin': '*'}
    # 尝试载入内存
    maybe_load_video(path)
    data = get_video_bytes(path)
    if data:
        from flask import Response
        return Response(data, mimetype=mimetype,
                        headers={'Content-Length': str(len(data)),
                                 'Cache-Control': 'max-age=3600',
                                 'X-From-RAM': 'true',
                                 'Access-Control-Allow-Origin': '*'})
    # 太大不在内存, 走磁盘
    resp = send_file(path, mimetype=mimetype)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """前端文件上传，保存到 render_uploads/ (文件名强制净化防穿越)"""
    files = request.files.getlist('files')
    if not files:
        files = [request.files.get('file')]
    results = []
    for f in files:
        if not f or not f.filename:
            continue
        name = secure_filename(f.filename) or 'upload.bin'
        dst = os.path.join(UPLOAD_DIR, name)
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
    if ftype == 'video':
        # 探测音轨是否存在, 供前端展示"已去除音轨"状态 (成功去音后这里会变 False)
        try:
            from perceive import has_audio_stream
            asset['has_audio'] = has_audio_stream(path)
        except Exception:
            asset['has_audio'] = None
    return asset


@app.route('/api/assets', methods=['GET'])
def api_assets_scan():
    """扫描 render_uploads/ 目录, 返回当前所有素材 (供 LocalSend 收到新文件后前端刷新).
    过滤掉无法识别的文件(如 .gitkeep 占位文件) —— 那些不是给用户看的素材."""
    results = []
    try:
        for name in sorted(os.listdir(UPLOAD_DIR)):
            if name.startswith('.'):
                continue
            p = os.path.join(UPLOAD_DIR, name)
            if not os.path.isfile(p):
                continue
            asset = _classify_asset(name, p)
            if asset['type'] == 'other':
                continue
            results.append(asset)
    except Exception:
        pass
    return jsonify({'assets': results})


@app.route('/api/assets/<name>', methods=['DELETE'])
def api_delete_asset(name):
    """删除 render_uploads/ 下的一个素材文件 (防路径穿越: 校验 basename 不改名, 否则带空格/括号
    的去重文件名如 'foo (1).mp4' 会被 secure_filename 改写导致匹配不上磁盘上的真实文件名)"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    path = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    try:
        os.remove(path)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/assets/<name>/strip-audio', methods=['POST'])
def api_asset_strip_audio(name):
    """去掉视频的音轨 —— 去音后该文件没有音轨, has_audio_stream()=False, 分析时
    自动跳过 VAD/ASR 整段逻辑, 只靠 VLM 看画面来匹配, 彻底不受环境音/嘈杂人声被
    webrtcvad 误判成"语音"的影响 (即上面 VID_20260814_200626.mp4 那种情况)。"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    path = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    ext = os.path.splitext(name)[1].lower()
    if _ASSET_TYPE_BY_EXT.get(ext) != 'video':
        return jsonify({'ok': False, 'error': '只能对视频文件去除声音'}), 400

    tmp_path = path + '.noaudio.tmp' + ext
    try:
        r = subprocess.run(
            ['ffmpeg', '-y', '-i', path, '-c:v', 'copy', '-an', tmp_path],
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
    invalidate_analysis(path)
    return jsonify({'ok': True, 'name': name, 'has_audio': False})


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
def api_asset_get_shots(name):
    """只读镜头拆分缓存, 没拆过返回 shots: null (前端据此显示"去拆分"而不是"拆分中")。"""
    if not config.safe_folder_name(name):
        return jsonify({'error': 'invalid name'}), 400
    path = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(path):
        return jsonify({'error': 'not found'}), 404
    import shot_split
    shots = shot_split.get_cached_shots(path)
    return jsonify({'shots': _shots_response(shots) if shots else None})


@app.route('/api/assets/<name>/split-shots', methods=['POST'])
def api_asset_split_shots(name):
    """分镜拆分: GPU CNN 特征检测镜头边界, 按边界切出每个镜头的独立小视频 + 关键帧。
    结果缓存, 大视频/长视频耗时可能到几十秒(推理+每个镜头重新编码), 非 force 命中缓存立即返回。"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    path = os.path.join(UPLOAD_DIR, name)
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
def api_get_main_video():
    """当前"主视频"(最新录制的那条, 每次会被替换) —— 跟长期存在的素材库分开管理。"""
    import main_video_store
    info = main_video_store.get()
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
def api_asset_set_main(name):
    """把这个素材标记为当前主视频。旧的主视频不用做任何处理 —— 指针一移开它自动就是
    普通素材库里的一条了, 可以被当成补充素材(比如剪一段旧镜头用)继续复用。"""
    if not config.safe_folder_name(name):
        return jsonify({'ok': False, 'error': 'invalid name'}), 400
    path = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    ext = os.path.splitext(name)[1].lower()
    if _ASSET_TYPE_BY_EXT.get(ext) != 'video':
        return jsonify({'ok': False, 'error': '只能把视频文件设为主视频'}), 400
    import main_video_store
    info = main_video_store.set(path)
    return jsonify({'ok': True, 'main_video': _main_video_response(info)})


@app.route('/api/main-video/clear', methods=['POST'])
def api_clear_main_video():
    import main_video_store
    main_video_store.clear()
    return jsonify({'ok': True})


@app.route('/api/chat', methods=['POST'])
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

    from flask import Response, stream_with_context
    from openai import OpenAI as _OAI
    from memory_store import get_analysis, save_analysis
    from perceive import QWEN_API_KEY as QWEN_KEY, QWEN_BASE_URL as QWEN_URL, QWEN_MODEL as LLM_MODEL
    import chat_store

    is_new_conversation = not conversation_id
    if is_new_conversation:
        conversation_id = chat_store.create(draft_id=draft_id)
    prior = chat_store.get(conversation_id)
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
                "description": "创建新草稿。返回 draft_id。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_video",
                "description": "添加视频到草稿。默认接在主视频轨道('video_main')末尾按顺序拼接。要把某段视频作为'补充素材/花絮/B-roll'叠加显示在主视频的某个时间点上方时，必须指定不同的 track_name 和该素材应出现的 target_start，否则会和主视频挤在同一条轨道上互相覆盖。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "视频URL或路径"},
                        "start": {"type": "number", "description": "源视频截取起始秒(素材文件内的秒数)", "default": 0},
                        "end": {"type": "number", "description": "源视频截取结束秒(素材文件内的秒数)"},
                        "target_start": {"type": "number", "description": "这段素材在成片时间轴上应该出现的秒数(不是素材源文件的秒数)。不填=自动接在同名轨道已有内容末尾"},
                        "track_name": {"type": "string", "description": "轨道名，默认 'video_main'(主视频轨道)。叠加补充素材时用不同的名字，如 'broll_1'"},
                        "relative_index": {"type": "integer", "description": "轨道层级，数值越大越靠上层显示。叠加在主视频上方要设成比主视频轨道更高的值(如 1)，否则会被主视频盖住而不是盖住主视频"}
                    },
                    "required": ["url"]
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
                        "outro_animation": {"type": "string", "description": "出场动画名，同 intro_animation 命名规则，如 'Blur_to_the_Left'/'Horizontal_Close' 等，不确定就别填"}
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
                "name": "save_draft",
                "description": "保存草稿到磁盘。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "render",
                "description": "提交渲染（自动保存草稿并渲染为mp4）。用户说'渲染/导出/出片'时调用。调用前必须已 create_draft + add_video。",
                "parameters": {"type": "object", "properties": {}}
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

    def _get_internal(endpoint):
        """内部 API 调用 (HTTP 自调, GET). 用法同 _post_internal, 见其注释."""
        try:
            url = f"{config.API_BASE}/{endpoint}"
            with requests.Session() as s:
                r = s.get(url, timeout=30)
                return r.json()
        except Exception as e:
            return {'error': str(e)}

    def _find_analysis(path):
        """按 path 查分析缓存. 容忍正/反斜杠差异 (前端传 /, 缓存可能存 \\)."""
        a = get_analysis(path)
        if a:
            return a
        # 归一化斜杠后重试
        alt = path.replace('/', '\\') if '/' in path else path.replace('\\', '/')
        return get_analysis(alt)

    def _resolve_asset_url(u):
        """把 LLM 传的素材标识解析成本地绝对路径 (正斜杠, pyJianYingDraft/ffprobe 都认).
        LLM 有时只传文件名 (如 'VID_20260819_102125.mp4') 而不是完整路径 —— 这种裸名会让
        VectCutAPI 的 update_media_metadata ffprobe 探不到文件 → 素材时长 0 → 主视频
        segment 零时长 → 渲染出来黑屏无声 (叠加段因显式传了 start/end 不受影响).
        规则: http(s)/file 协议不动; 已存在的绝对路径不动; 其余按文件名去上传目录找."""
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
        接龙, 不用 agent 自己心算已加了多少段/每段多长(算错了同一时间点会撞车覆盖)。"""
        r = _post_internal('query_script', {'draft_id': draft_id})
        try:
            script = json.loads(r.get('output') or '{}')
            track = next((t for t in script.get('tracks', []) if t.get('name') == track_name), None)
            if not track or not track.get('segments'):
                return 0.0
            end_us = max(s['target_timerange']['start'] + s['target_timerange']['duration']
                         for s in track['segments'])
            return round(end_us / 1_000_000, 3)
        except Exception:
            return 0.0

    def execute_tool(name, args):
        """执行工具调用，返回结果字符串"""
        import re as _re
        nonlocal draft_id
        result = {}

        if name == 'list_resources':
            import main_video_store
            main_path = (main_video_store.get() or {}).get('path')
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
            info = main_video_store.get()
            if not info:
                result = {'error': '还没有标记任何主视频，请让用户在素材面板里点"设为主视频"'}
            else:
                result = {'name': info['name'], 'path': info['path']}

        elif name == 'search_by_tags':
            keywords = [str(k).strip() for k in (args.get('keywords') or []) if str(k).strip()]
            asset_set = set(asset_paths)
            from asset_store import search_tags as _search_tags
            matches = []
            for m in _search_tags(keywords, type=args.get('type')):
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
            for m in _search_text(query, type=args.get('type')):
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
                    'segments': audio.get('segments', [])
                }
            else:
                result = {'full_text': '(无语音)', 'segments': []}

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
                save_analysis(path, analysis)
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
            r = _post_internal('create_draft', {'width': 1080, 'height': 1920})
            if r.get('success') and r.get('output', {}).get('draft_id'):
                draft_id = r['output']['draft_id']
                result = {'draft_id': draft_id, 'ok': True}
            else:
                result = {'error': str(r)}

        elif name == 'add_video':
            if not draft_id: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name') or 'video_main'
            target_start = args.get('target_start')
            if target_start is None:
                target_start = _track_end_seconds(draft_id, track_name)
            d = {'draft_id': draft_id, 'video_url': _resolve_asset_url(args.get('url','')),
                 'track_name': track_name, 'target_start': target_start}
            if args.get('relative_index') is not None: d['relative_index'] = args['relative_index']
            if args.get('start') is not None: d['start'] = args['start']
            if args.get('end') is not None: d['end'] = args['end']
            r = _post_internal('add_video', d)
            result = {'ok': r.get('success', False), 'track_name': track_name, 'target_start': target_start}

        elif name == 'add_text':
            if not draft_id: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name') or 'text_main'
            d = {
                'draft_id': draft_id, 'text': args.get('text',''),
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
            r = _post_internal('add_text', d)
            result = {'ok': r.get('success', False), 'track_name': track_name}

        elif name == 'list_text_animations':
            names = _text_animation_names(args.get('kind', 'intro'))
            result = {'kind': args.get('kind'), 'names': names} if names else {'error': '动画名字表加载失败'}

        elif name == 'add_audio':
            if not draft_id: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            d = {'draft_id': draft_id, 'audio_url': _resolve_asset_url(args.get('url','')),
                 'volume': args.get('volume', 0.5)}
            if args.get('start') is not None: d['start'] = args['start']
            if args.get('end') is not None: d['end'] = args['end']
            r = _post_internal('add_audio', d)
            result = {'ok': r.get('success', False)}

        elif name == 'add_image':
            if not draft_id: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            track_name = args.get('track_name') or 'image_main'
            start = args.get('start')
            if start is None:
                start = _track_end_seconds(draft_id, track_name)
            end = args.get('end')
            if end is None:
                end = start + 3   # 没给时长默认展示 3 秒
            d = {'draft_id': draft_id, 'image_url': _resolve_asset_url(args.get('url','')),
                 'track_name': track_name, 'start': start, 'end': end}
            if args.get('relative_index') is not None: d['relative_index'] = args['relative_index']
            r = _post_internal('add_image', d)
            result = {'ok': r.get('success', False), 'track_name': track_name, 'start': start, 'end': end}

        elif name == 'save_draft':
            if not draft_id: return json.dumps({'error': '没有草稿'}, ensure_ascii=False)
            r = _post_internal('save_draft', {'draft_id': draft_id})
            result = {'ok': r.get('success', False)}

        elif name == 'render':
            if not draft_id: return json.dumps({'error': '没有草稿'}, ensure_ascii=False)
            _post_internal('save_draft', {'draft_id': draft_id})
            r = _post_internal(f'render/draft/{draft_id}')
            if r.get('task_id'):
                result = {'task_id': r['task_id'], 'poll': r.get('poll', ''), 'ok': True}
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
                    r = _get_internal(f"render/status/{task_id}")
                    cur = (r.get('status'), (r.get('progress') or {}).get('stage')) if isinstance(r, dict) else None
                    if not isinstance(r, dict) or r.get('status') in ('done', 'error') or (prev is not None and cur != prev):
                        break
                    prev = cur
                    time.sleep(2.5)
                    waited += 2.5
                result = r if isinstance(r, dict) else {'error': str(r)}
            else:
                r = _get_internal(f"render/status/{task_id}")
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
            r = _get_internal('api/drafts')
            if isinstance(r, list):
                result = {'drafts': [
                    {'name': d.get('name'), 'id': d.get('id'), 'duration': d.get('duration'),
                     'modified': d.get('modified')} for d in r
                ]}
            else:
                result = {'error': str(r)}

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
3. 引用语音内容时带时间戳；但时间戳/文案必须直接来自工具返回的 segments，禁止自己编造或推测
4. 保持简洁，中文回复
5. 制作视频的标准流程: create_draft → add_video(url, start, end) → [可选 add_text/add_audio/add_image] → save_draft → render
6. 当用户要求"渲染/导出/出片/出视频"时，保存草稿后必须调用 render 工具提交渲染，不要只说"可以渲染了"；提交后【默认自动监控】：立即用 render_status(wait=true) 查询，未完成就继续调用（每次服务端会等~25秒），直到 done/error，然后直接告知用户结果（done 报 mp4 文件名，error 报错误摘要），不要问"需要我帮你监控吗"，也不要中途汇报无意义的进度
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
            try:
                resp = client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    max_tokens=2000,
                )
            except Exception as e:
                yield f"data: {json.dumps({'text': f'错误: {e}'}, ensure_ascii=False)}\n\n"
                turn_log.append({'role': 'assistant', 'content': f'错误: {e}'})
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
            chat_store.save_messages(conversation_id, turn_log, draft_id)
        except Exception as e:
            print('[chat] 会话保存失败: %s' % e, flush=True)

        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/chat/conversations', methods=['GET'])
def api_chat_conversations_list():
    """历史会话列表 (标题/草稿/时间), 供 Chat 面板侧栏用. 不含 messages 正文."""
    import chat_store
    return jsonify(chat_store.list_all())


@app.route('/api/chat/conversations', methods=['POST'])
def api_chat_conversations_create():
    """新建一条空会话 (New Chat 按钮)."""
    import chat_store
    data = request.json or {}
    cid = chat_store.create(draft_id=data.get('draft_id'))
    return jsonify({'id': cid})


@app.route('/api/chat/conversations/<cid>', methods=['GET'])
def api_chat_conversations_get(cid):
    """取一条会话完整消息 (切换会话时用来恢复聊天记录)."""
    import chat_store
    conv = chat_store.get(cid)
    if not conv:
        return jsonify({'error': 'not found'}), 404
    return jsonify(conv)


@app.route('/api/chat/conversations/<cid>', methods=['DELETE'])
def api_chat_conversations_delete(cid):
    import chat_store
    chat_store.delete(cid)
    return jsonify({'ok': True})


def _text_animation_names(kind):
    """返回 CapCut 文字入场/出场动画的合法名字列表 (供 agent 校验/枚举, 避免瞎猜)."""
    try:
        from pyJianYingDraft.metadata.capcut_text_animation_meta import CapCut_Text_intro, CapCut_Text_outro
        enum_cls = CapCut_Text_intro if kind == 'intro' else CapCut_Text_outro
        return sorted(m.name for m in enum_cls)
    except Exception:
        return []


def _post_internal(endpoint, data=None):
    """内部 API 调用 (HTTP 自调). 用独立 session 避免 SSE 长连接复用导致的连接池耗尽.
    render/draft 等异步端点立即返回; 同步端点最多等 120s."""
    try:
        url = f"{config.API_BASE}/{endpoint}"
        with requests.Session() as s:
            r = s.post(url, json=data or {}, timeout=120)
            return r.json()
    except Exception as e:
        return {'error': str(e)}


@app.route('/api/templates', methods=['GET'])
def api_templates():
    """列出可用模板"""
    try:
        from template_engine import list_templates
        return jsonify(list_templates())
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/templates/render', methods=['POST'])
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
def api_localsend_status():
    """LocalSend 接收端状态 (设备名/端口/是否有正在进行的传输/本次接收列表)"""
    try:
        import localsend_recv
        return jsonify(localsend_recv.status())
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)})


@app.route('/api/localsend/start', methods=['POST'])
def api_localsend_start():
    """按需启动 LocalSend 接收端 (前端"接收"按钮触发)"""
    try:
        import localsend_recv
        ok = localsend_recv.start_server(save_dir=UPLOAD_DIR)
        if ok:
            if config.AUTO_PERCEIVE:
                localsend_recv.set_on_file_received(_auto_perceive_hook)
            return jsonify({'running': True, 'alias': localsend_recv.ALIAS,
                            'port': localsend_recv.PORT, 'save_dir': UPLOAD_DIR})
        return jsonify({'running': False,
                        'error': '端口 53317 被占, 请关闭官方 LocalSend 或其他占用程序后重试'}), 409
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)}), 500


def _auto_perceive_hook(path):
    """素材收件回调: 视频文件后台调 VLM 分析 (避免阻塞收件线程)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.mp4', '.mov', '.avi', '.mkv'):
        return

    from memory_store import has_analysis, save_analysis

    def _work():
        if has_analysis(path):
            return
        try:
            from perceive import perceive_video
            result = perceive_video(path)
            save_analysis(path, result)
            print('[auto-perceive] 已分析 %s' % os.path.basename(path), flush=True)
        except Exception as e:
            print('[auto-perceive] 分析失败 %s: %s' % (os.path.basename(path), e), flush=True)

    threading.Thread(target=_work, daemon=True).start()


@app.route('/api/localsend/stop', methods=['POST'])
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
def api_settings_get():
    """当前 LLM/ASR 配置 (密钥脱敏, 附 configured 标记)"""
    import settings_store
    return jsonify(settings_store.get_settings())


@app.route('/api/settings', methods=['POST'])
def api_settings_save():
    """保存配置到 .env + 热更新 (密钥留空 = 不修改)"""
    import settings_store
    data = request.json or {}
    return jsonify(settings_store.save_settings(data))


@app.route('/api/settings/test', methods=['POST'])
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
    # 保存到临时文件
    tmp_path = os.path.join(UPLOAD_DIR, 'perceive_' + uuid.uuid4().hex[:8] + os.path.splitext(f.filename)[1])
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
    tmp_path = os.path.join(UPLOAD_DIR, 'check_' + uuid.uuid4().hex[:8] + '.mp4')
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
    print('render_server on http://%s:%d (多桌面: %s)' % (
        config.RENDER_SERVER_HOST, config.RENDER_SERVER_PORT, DESKTOP_NAMES), flush=True)
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
