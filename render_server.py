# render_server.py — RESTful 渲染服务
# 接收 zip 草稿 → 真后台渲染 (--desktop) → 返回 mp4
# 融合 VectCutAPI 的编辑端点 (add_video/add_text/...) → 完整生产线
import os, sys, json, zipfile, threading, uuid, time, subprocess, shutil
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

from flask import Flask, request, jsonify, send_file, send_from_directory

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
UPLOAD_DIR = os.path.join(HERE, 'render_uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
DRAFT_ROOT = r'C:\Users\Administrator\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft'
VIDEOS = r'C:\Users\Administrator\Videos'

# === 多桌面渲染池 ===
# N 个独立桌面, 每个跑一个剪映实例, 支持并行渲染
import queue
tasks = {}
TASK_LOCK = threading.Lock()
RENDER_QUEUE = queue.Queue()

# 桌面池配置 (2 个桌面 = 2 并行渲染)
DESKTOP_NAMES = ['JYRender_0']  # 串行单桌面 (JYRender_1 导出 modal 点 confirm 不触发渲染, 弃用)
desktop_pool = {}  # desk_name -> {pid, busy, lock}


def init_desktop_pool():
    """预启动: 每个桌面启动剪映实例."""
    for desk in DESKTOP_NAMES:
        print('[pool] 启动桌面 %s...' % desk, flush=True)
        pid = start_desktop_jianying(desk)
        if pid:
            desktop_pool[desk] = {'pid': pid, 'busy': False}
            print('[pool] %s 剪映就绪 PID=%d' % (desk, pid), flush=True)
        else:
            print('[pool] %s 启动失败!' % desk, flush=True)


def start_desktop_jianying(desk_name):
    """在指定桌面启动剪映, 返回 PID."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    GENERIC_ALL = 0x10000000
    OpenDesktop = user32.OpenDesktopW
    OpenDesktop.restype = wintypes.HDESK
    OpenDesktop.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    CreateDesktop = user32.CreateDesktopW
    CreateDesktop.restype = wintypes.HDESK
    CreateDesktop.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    hDesk = OpenDesktop(desk_name, 0, True, GENERIC_ALL)
    if not hDesk:
        hDesk = CreateDesktop(desk_name, None, None, 0, GENERIC_ALL, None)
    # exe
    base = r'C:\Users\Administrator\AppData\Local\JianyingPro\Apps'
    exe = None
    for d in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, d, 'JianyingPro.exe')
        if os.path.exists(p): exe = p; break
    class STARTUPINFO(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD),('lpReserved', wintypes.LPWSTR),('lpDesktop', wintypes.LPWSTR),
                    ('lpTitle', wintypes.LPWSTR),('dwX', wintypes.DWORD),('dwY', wintypes.DWORD),
                    ('dwXSize', wintypes.DWORD),('dwYSize', wintypes.DWORD),('dwXCountChars', wintypes.DWORD),
                    ('dwYCountChars', wintypes.DWORD),('dwFillAttribute', wintypes.DWORD),('dwFlags', wintypes.DWORD),
                    ('wShowWindow', wintypes.WORD),('cbReserved2', wintypes.WORD),('lpReserved2', ctypes.c_void_p),
                    ('hStdInput', wintypes.HANDLE),('hStdOutput', wintypes.HANDLE),('hStdError', wintypes.HANDLE)]
    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [('hProcess', wintypes.HANDLE),('hThread', wintypes.HANDLE),('dwProcessId', wintypes.DWORD),('dwThreadId', wintypes.DWORD)]
    si = STARTUPINFO(); si.cb = ctypes.sizeof(si)
    si.lpDesktop = desk_name
    si.dwFlags = 0x00000001 | 0x00000080; si.wShowWindow = 1
    si.dwXSize = 1280; si.dwYSize = 720
    pi = PROCESS_INFORMATION()
    CreateProcessW = kernel32.CreateProcessW
    CreateProcessW.restype = wintypes.BOOL
    CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                               wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                               ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION)]
    if CreateProcessW(exe, None, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi)):
        return pi.dwProcessId
    return None


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


def render_pool_worker():
    """worker 线程: 取队列任务 → 获取桌面 → 渲染 (每次自己 start+kill 剪映)."""
    while True:
        task_id, draft_dir, draft_name = RENDER_QUEUE.get()
        desk = acquire_desktop()
        if not desk:
            with TASK_LOCK:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = 'no free desktop'
            continue
        with TASK_LOCK:
            tasks[task_id]['status'] = 'rendering'
            tasks[task_id]['desktop'] = desk
        try:
            t0 = time.time()
            # render_driver 自己启动剪映到 desk 桌面, 渲染完 kill 自己的剪映
            r = subprocess.run(
                [sys.executable, os.path.join(HERE, 'render_driver.py'),
                 'render-draft', draft_dir, '--desktop', '--desktop-name', desk],
                cwd=HERE, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=600)
            dt = time.time() - t0
            with TASK_LOCK:
                if r.returncode == 0:
                    mps = sorted([f for f in os.listdir(VIDEOS)
                                  if f.endswith('.mp4') and (f.startswith(draft_name) or f.startswith('rd'))],
                                 key=lambda f: os.path.getmtime(os.path.join(VIDEOS, f)), reverse=True)
                    if mps:
                        tasks[task_id]['status'] = 'done'
                        tasks[task_id]['mp4_path'] = os.path.join(VIDEOS, mps[0])
                        tasks[task_id]['mp4_name'] = mps[0]
                        tasks[task_id]['duration'] = dt
                    else:
                        tasks[task_id]['status'] = 'error'
                        tasks[task_id]['error'] = 'no mp4'
                else:
                    tasks[task_id]['status'] = 'error'
                    tasks[task_id]['error'] = (r.stdout or '')[-800:]
        except Exception as e:
            with TASK_LOCK:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = str(e)
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


def render_worker(task_id, draft_dir, draft_name):
    """后台线程: 调 render_driver render-draft --desktop --close"""
    with TASK_LOCK:
        tasks[task_id]['status'] = 'rendering'
        tasks[task_id]['started_at'] = time.time()
    try:
        t0 = time.time()
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, 'render_driver.py'),
             'render-draft', draft_dir, '--desktop', '--close'],
            cwd=HERE, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600)
        dt = time.time() - t0
        with TASK_LOCK:
            if r.returncode == 0:
                # 找输出的 mp4 (draft_name.mp4 或 draft_name(N).mp4)
                mps = sorted(
                    [f for f in os.listdir(VIDEOS)
                     if f.endswith('.mp4') and f.startswith(draft_name)],
                    key=lambda f: os.path.getmtime(os.path.join(VIDEOS, f)), reverse=True)
                if mps:
                    tasks[task_id]['status'] = 'done'
                    tasks[task_id]['mp4_path'] = os.path.join(VIDEOS, mps[0])
                    tasks[task_id]['mp4_name'] = mps[0]
                    tasks[task_id]['duration'] = dt
                else:
                    # render-draft 注入名是 rd<ts>, 找最新的 rd*.mp4
                    rds = sorted(
                        [f for f in os.listdir(VIDEOS)
                         if f.endswith('.mp4') and f.startswith('rd')],
                        key=lambda f: os.path.getmtime(os.path.join(VIDEOS, f)), reverse=True)
                    if rds:
                        tasks[task_id]['status'] = 'done'
                        tasks[task_id]['mp4_path'] = os.path.join(VIDEOS, rds[0])
                        tasks[task_id]['mp4_name'] = rds[0]
                        tasks[task_id]['duration'] = dt
                    else:
                        tasks[task_id]['status'] = 'error'
                        tasks[task_id]['error'] = 'render ok but no mp4 found'
            else:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = (r.stdout or '')[-800:]
                tasks[task_id]['duration'] = dt
    except subprocess.TimeoutExpired:
        with TASK_LOCK:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['error'] = 'render timeout (600s)'
    except Exception as e:
        with TASK_LOCK:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['error'] = str(e)


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
            z.extractall(extract_dir)
    except Exception as e:
        return jsonify({'error': 'zip extract failed: %s' % e}), 400
    draft_dir = find_draft_dir(extract_dir)
    if not draft_dir:
        return jsonify({'error': 'invalid draft: no draft_content.json in zip'}), 400
    with TASK_LOCK:
        tasks[task_id] = {'status': 'queued', 'draft_name': draft_name,
                          'draft_dir': draft_dir, 'created': time.time()}
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
                         'mp4': v.get('mp4_name'), 'draft': v.get('draft_name')}
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
    with TASK_LOCK:
        tasks[task_id] = {'status': 'queued', 'draft_name': draft_id,
                          'draft_dir': draft_dir, 'created': time.time()}
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

    # 内存查询 (O(1) dict)
    if not force:
        cached = get_analysis(path)
        if cached:
            cached['_cached'] = True
            return jsonify(cached)

    # 调 VLM 分析
    try:
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
    cover_path = os.path.join(DRAFT_ROOT, folder, 'draft_cover.jpg')
    if os.path.exists(cover_path):
        return send_file(cover_path, mimetype='image/jpeg')
    return '', 404


@app.route('/api/drafts/<folder>', methods=['DELETE'])
def api_delete_draft(folder):
    """删除草稿（文件夹 + root_meta 同步）"""
    import shutil
    draft_path = os.path.join(DRAFT_ROOT, folder)
    if not os.path.isdir(draft_path):
        return jsonify({'error': 'not found'}), 404
    try:
        shutil.rmtree(draft_path)
        # 同步 root_meta
        root_meta = os.path.join(DRAFT_ROOT, 'root_meta_info.json')
        if os.path.exists(root_meta):
            m = json.load(open(root_meta, encoding='utf-8'))
            m['all_draft_store'] = [d for d in m.get('all_draft_store', []) if os.path.basename(d.get('draft_fold_path', '')) != folder]
            m['draft_ids'] = len(m['all_draft_store'])
            json.dump(m, open(root_meta, 'w', encoding='utf-8'), ensure_ascii=False)
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


@app.route('/api/video/serve', methods=['GET'])
def api_video_serve():
    """从内存缓存提供视频字节 (≤50MB 视频零磁盘 IO)"""
    from memory_store import maybe_load_video, get_video_bytes
    path = request.args.get('path', '')
    if not path or not os.path.exists(path):
        return jsonify({'error': 'not found'}), 404
    # 尝试载入内存
    maybe_load_video(path)
    data = get_video_bytes(path)
    if data:
        from flask import Response
        return Response(data, mimetype='video/mp4',
                        headers={'Content-Length': str(len(data)),
                                 'Cache-Control': 'max-age=3600',
                                 'X-From-RAM': 'true'})
    # 太大不在内存, 走磁盘
    return send_file(path, mimetype='video/mp4')


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """前端文件上传，保存到 gui_uploads/"""
    files = request.files.getlist('files')
    if not files:
        files = [request.files.get('file')]
    results = []
    for f in files:
        if not f or not f.filename:
            continue
        name = f.filename
        dst = os.path.join(UPLOAD_DIR, name)
        f.save(dst)
        ext = os.path.splitext(name)[1].lower()
        ftype = 'video' if ext in ('.mp4', '.mov', '.avi', '.mkv') else \
                'image' if ext in ('.jpg', '.png', '.jpeg', '.webp') else \
                'audio' if ext in ('.mp3', '.wav', '.aac', '.m4a') else 'other'
        results.append({'name': name, 'path': dst, 'type': ftype, 'file': dst})
    return jsonify({'assets': results})


def _classify_asset(name, path):
    ext = os.path.splitext(name)[1].lower()
    ftype = 'video' if ext in ('.mp4', '.mov', '.avi', '.mkv') else \
            'image' if ext in ('.jpg', '.png', '.jpeg', '.webp') else \
            'audio' if ext in ('.mp3', '.wav', '.aac', '.m4a') else 'other'
    return {'name': name, 'path': path, 'type': ftype, 'file': path}


@app.route('/api/assets', methods=['GET'])
def api_assets_scan():
    """扫描 render_uploads/ 目录, 返回当前所有素材 (供 LocalSend 收到新文件后前端刷新)"""
    results = []
    try:
        for name in sorted(os.listdir(UPLOAD_DIR)):
            p = os.path.join(UPLOAD_DIR, name)
            if os.path.isfile(p):
                results.append(_classify_asset(name, p))
    except Exception:
        pass
    return jsonify({'assets': results})


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """SSE 流式对话: Agent 用 function calling 按需查询资源信息"""
    from flask import Response, stream_with_context
    from openai import OpenAI as _OAI
    from memory_store import get_analysis
    from perceive import QWEN_API_KEY as QWEN_KEY, QWEN_BASE_URL as QWEN_URL, QWEN_MODEL as LLM_MODEL

    data = request.json or {}

    # 定义工具 (function calling)
    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "list_resources",
                "description": "列出所有已上传的资源（名称、类型、是否已分析）。用户问'有哪些素材'时调用。",
                "parameters": {"type": "object", "properties": {}}
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
                "name": "create_draft",
                "description": "创建新草稿。返回 draft_id。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_video",
                "description": "添加视频到草稿。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "视频URL或路径"},
                        "start": {"type": "number", "description": "源开始秒", "default": 0},
                        "end": {"type": "number", "description": "源结束秒"}
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_text",
                "description": "添加文字到草稿。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "文字内容"},
                        "start": {"type": "number", "description": "开始秒"},
                        "end": {"type": "number", "description": "结束秒"}
                    },
                    "required": ["text", "start", "end"]
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
    ]

    def _find_analysis(path):
        """按 path 查分析缓存. 容忍正/反斜杠差异 (前端传 /, 缓存可能存 \\)."""
        a = get_analysis(path)
        if a:
            return a
        # 归一化斜杠后重试
        alt = path.replace('/', '\\') if '/' in path else path.replace('\\', '/')
        return get_analysis(alt)

    def execute_tool(name, args):
        """执行工具调用，返回结果字符串"""
        import re as _re
        nonlocal draft_id
        result = {}

        if name == 'list_resources':
            items = []
            for path in asset_paths:
                fname = os.path.basename(path)
                ext = os.path.splitext(fname)[1].lower()
                ftype = 'video' if ext in ('.mp4','.mov','.avi','.mkv') else \
                        'image' if ext in ('.jpg','.png','.jpeg') else \
                        'audio' if ext in ('.mp3','.wav','.aac') else 'other'
                analyzed = '已分析' if _find_analysis(path) else '未分析'
                items.append(f'{fname} ({ftype}, {analyzed})')
            result = {'resources': items}

        elif name == 'get_resource_detail':
            fname = args.get('name', '')
            path = next((p for p in asset_paths if fname in p), None)
            if not path:
                return json.dumps({'error': f'未找到资源: {fname}'}, ensure_ascii=False)
            analysis = _find_analysis(path)
            if not analysis:
                return json.dumps({'error': f'{fname} 尚未分析，请先在资源面板点分析按钮'}, ensure_ascii=False)
            detail = {'filename': fname, 'path': path}
            meta = analysis.get('meta', {})
            detail['duration'] = meta.get('duration', 0)
            detail['resolution'] = f'{meta.get("width","?")}x{meta.get("height","?")}'
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
                return json.dumps({'error': f'{fname} 尚未分析'}, ensure_ascii=False)
            audio = analysis.get('audio', {})
            if isinstance(audio, dict):
                result = {
                    'full_text': audio.get('full_text', '(无语音)'),
                    'segments': audio.get('segments', [])
                }
            else:
                result = {'full_text': '(无语音)', 'segments': []}

        elif name == 'create_draft':
            r = _post_internal('create_draft', {'width': 1080, 'height': 1920})
            if r.get('success') and r.get('output', {}).get('draft_id'):
                draft_id = r['output']['draft_id']
                result = {'draft_id': draft_id, 'ok': True}
            else:
                result = {'error': str(r)}

        elif name == 'add_video':
            if not draft_id: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            d = {'draft_id': draft_id, 'video_url': args.get('url','')}
            if args.get('start') is not None: d['start'] = args['start']
            if args.get('end') is not None: d['end'] = args['end']
            r = _post_internal('add_video', d)
            result = {'ok': r.get('success', False)}

        elif name == 'add_text':
            if not draft_id: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
            r = _post_internal('add_text', {
                'draft_id': draft_id, 'text': args.get('text',''),
                'start': args.get('start',0), 'end': args.get('end',5),
                'font_size': 12, 'font_color': '#FFFFFF'
            })
            result = {'ok': r.get('success', False)}

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

        # 确保 result 是 dict, 再序列化
        if not isinstance(result, dict):
            result = {'raw': str(result)[:500]}
        return json.dumps(result, ensure_ascii=False)

    def generate():
        nonlocal draft_id
        client = _OAI(api_key=QWEN_KEY, base_url=QWEN_URL)

        # 简洁 system prompt (只列资源名，不列详情)
        resource_names = ', '.join(os.path.basename(p) for p in asset_paths) if asset_paths else '无'
        system = f"""你是一个 AI 视频编辑助手。

已上传资源: {resource_names}
当前草稿: {draft_id or '无'}

重要规则:
1. 回答视频内容/文案问题时，调用 get_resource_detail 或 get_transcript 查询，不要编造
2. 资源未分析时，告知用户先点分析按钮
3. 引用语音内容时带时间戳
4. 保持简洁，中文回复
5. 制作视频的标准流程: create_draft → add_video(url, start, end) → save_draft → render
6. 当用户要求"渲染/导出/出片/出视频"时，保存草稿后必须调用 render 工具提交渲染，不要只说"可以渲染了"
7. add_video 的 start/end 是源视频的截取起止秒数（如 start=0, end=10 取前10秒）"""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]

        # 多轮工具调用 (最多 8 轮 — 覆盖 list→detail→create→add→save→render 完整链路)
        for _ in range(8):
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
                break

            msg = resp.choices[0].message
            messages.append(msg.model_dump())

            # 有文本输出 → 流式发送
            if msg.content:
                yield f"data: {json.dumps({'text': msg.content}, ensure_ascii=False)}\n\n"

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

                # 如果是 create_draft，更新 draft_id
                if fn_name == 'create_draft':
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

        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


def _parse_and_execute_actions(text, draft_id):
    """解析 LLM 输出中的 ACTION 行，执行并返回结果"""
    import re
    results = []
    current_draft = draft_id

    for line in text.split('\n'):
        line = line.strip()
        if not line.startswith('ACTION:'):
            continue
        action = line[7:].strip()

        try:
            if 'CREATE_DRAFT' in action:
                r = _post_internal('create_draft', {'width': 1080, 'height': 1920})
                if r.get('success'):
                    current_draft = r['output']['draft_id']
                    results.append({'text': f'✅ 草稿: {current_draft}', 'success': True, 'draft_id': current_draft})
                else:
                    results.append({'text': f'❌ 创建失败', 'success': False})

            elif 'ADD_VIDEO' in action and current_draft:
                url_match = re.search(r'\(([^)]+)\)', action)
                url = url_match.group(1) if url_match else ''
                r = _post_internal('add_video', {'draft_id': current_draft, 'video_url': url, 'start': 0, 'end': 10})
                ok = r.get('success', False)
                results.append({'text': f'{"✅" if ok else "❌"} 添加视频', 'success': ok})

            elif 'ADD_TEXT' in action and current_draft:
                parts = re.findall(r'[\w]+=*"?([^",)]+)"?', action)
                txt = parts[0] if parts else '文字'
                r = _post_internal('add_text', {'draft_id': current_draft, 'text': txt, 'start': 0, 'end': 5, 'font_size': 12})
                ok = r.get('success', False)
                results.append({'text': f'{"✅" if ok else "❌"} 添加文字: {txt}', 'success': ok})

            elif 'SAVE_DRAFT' in action and current_draft:
                r = _post_internal('save_draft', {'draft_id': current_draft})
                ok = r.get('success', False)
                results.append({'text': f'{"✅" if ok else "❌"} 保存草稿', 'success': ok})

            elif 'RENDER' in action and current_draft:
                r = _post_internal(f'render/draft/{current_draft}')
                tid = r.get('task_id')
                if tid:
                    results.append({'text': f'✅ 渲染已提交: {tid}', 'success': True})
                else:
                    results.append({'text': '❌ 渲染失败', 'success': False})

        except Exception as e:
            results.append({'text': f'❌ {e}', 'success': False})

    return results


def _post_internal(endpoint, data=None):
    """内部 API 调用 (HTTP 自调). 用独立 session 避免 SSE 长连接复用导致的连接池耗尽.
    render/draft 等异步端点立即返回; 同步端点最多等 120s."""
    try:
        url = f"http://127.0.0.1:9002/{endpoint}"
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
            return jsonify({'running': True, 'alias': localsend_recv.ALIAS,
                            'port': localsend_recv.PORT, 'save_dir': UPLOAD_DIR})
        return jsonify({'running': False,
                        'error': '端口 53317 被占, 请关闭官方 LocalSend 或其他占用程序后重试'}), 409
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)}), 500


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


# ============================================================ 静态文件 (Vue 构建产物)
STATIC_DIR = os.path.join(HERE, 'static')

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_vue(path):
    """Flask 服务 Vue SPA (catch-all)"""
    if not os.path.isdir(STATIC_DIR):
        return jsonify({'error': 'Frontend not built. Run: cd frontend && npm run build'}), 404
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
    print('render_server on http://localhost:9002 (多桌面: %s)' % DESKTOP_NAMES, flush=True)
    # 初始化桌面池 (不预启动剪映; render_driver 每次 --desktop-name 自己 start+kill)
    for desk in DESKTOP_NAMES:
        desktop_pool[desk] = {'busy': False}
    for i in range(len(DESKTOP_NAMES)):
        threading.Thread(target=render_pool_worker, daemon=True).start()
    print('[pool] %d 个 worker 启动' % len(DESKTOP_NAMES), flush=True)

    # LocalSend 接收端: 按需启动 (前端"接收"按钮触发, 不随服务常驻)
    print('[localsend] 接收端待命 (前端点"接收"按钮启动), 端口 53317', flush=True)

    # CORS for dev mode (Vite on 5173)
    @app.after_request
    def after_request(response):
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        return response

    app.run(host='0.0.0.0', port=9002, debug=False)
