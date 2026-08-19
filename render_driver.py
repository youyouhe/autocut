# render_driver.py
# 剪映端到端渲染闭环 driver
#   calibrate : 弹窗引导点击 5 个关键位置(草稿卡片/导出/确认/关完成窗/关编辑器), 存 calib.json
#   run [N]   : 按校准坐标自动渲染 N 次(单草稿闭环; 多草稿需扩展卡片网格定位)
# 依赖: hook_inject1.js (主进程内 hook, 提供捕获+clickfull), render_monitor.py (完成检测)

import frida, time, json, os, sys, threading, subprocess

try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

# 关键: 声明本进程 per-monitor DPI aware, 否则在高 DPI 缩放(如150%)下
# GetWindowRect/MoveWindow 拿到的是被系统虚拟化缩放过的坐标, 跟剪映(真 DPI aware 的 Qt 应用)
# 内部真实物理像素坐标系不一致 —— resize 看起来"成功"但实际物理窗口尺寸是被缩放过的,
# 导致 hook 捕获的点击坐标跟我们以为的窗口尺寸完全不对应(实测偏差正好是缩放比例, 如150%).
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import config

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
HOOK        = os.path.join(SCRIPT_DIR, 'hook_focus.js')   # agent 线程 focusWindow + 点击
CALIB_FILE  = config.CALIB_FILE                            # 现在只存 global 坐标
MONITOR_LOG = os.path.join(SCRIPT_DIR, 'render_monitor.log')
MONITOR_PY  = os.path.join(SCRIPT_DIR, 'render_monitor.py')
# 剪映草稿根目录 (剪映实时监视此目录, 放草稿文件夹进去会自动识别)
DRAFT_ROOT  = config.DRAFT_ROOT

def log(m):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def emit_progress(stage, pct, **extra):
    """输出机器可读进度行, render_server 解析 '[PROGRESS] {json}' 更新任务状态."""
    d = {'stage': stage, 'pct': pct}
    d.update(extra)
    print('[PROGRESS] %s' % json.dumps(d, ensure_ascii=False), flush=True)

def inject_draft(src_draft_dir, new_name=None):
    """复制源草稿文件夹到剪映草稿根目录, 改 id/名字/时间(排首页第一).
    剪映实时监视目录, 会自动识别新草稿. 返回新草稿文件夹路径."""
    import shutil, uuid
    src = os.path.abspath(src_draft_dir)
    src_name = os.path.basename(src)
    new_name = new_name or (src_name + '_render')
    dst = os.path.join(DRAFT_ROOT, new_name)
    # 若已存在先删
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    new_id = str(uuid.uuid4()).upper()
    new_fold = dst.replace('\\', '/')
    now_us = int(time.time() * 1000000)
    # 改 draft_meta_info.json
    mp = os.path.join(dst, 'draft_meta_info.json')
    if os.path.exists(mp):
        m = json.load(open(mp, encoding='utf-8'))
        m['draft_id'] = new_id
        m['draft_name'] = new_name
        m['draft_fold_path'] = new_fold
        m['draft_cover'] = 'draft_cover.jpg'
        m['tm_draft_modified'] = now_us
        m['tm_draft_create'] = now_us
        json.dump(m, open(mp, 'w', encoding='utf-8'), ensure_ascii=False)
    # 改 draft_content.json 的 id + 修正素材绝对路径
    cp = os.path.join(dst, 'draft_content.json')
    if os.path.exists(cp):
        c = json.load(open(cp, encoding='utf-8'))
        if isinstance(c, dict) and 'id' in c:
            c['id'] = new_id
        # 关键修复: 剪映导出时, 若视频素材 path/media_path 为空(无法解析素材文件),
        # 会静默中止导出 —— 点了"导出"确认后弹窗关闭但渲染引擎根本不启动(temp_bytes 全程 0),
        # 之后重试连导出弹窗都不再弹出(剪映内部卡在失败态), 整个任务误报为点击问题.
        # 源草稿素材路径为空有两种来源: (a) copy_draft_external 创建时被抹掉, 素材文件还在
        # 草稿的 assets/video|audio/ 子目录; (b) 生成器只写了 remote_url(原始上传文件名)引用,
        # 素材文件在服务端上传目录 render_uploads/ 里. 两种都回填成新草稿内/本地的绝对路径
        # (正斜杠, 与剪映自身格式一致), 让导出能定位素材.
        mats = c.get('materials', {}) if isinstance(c, dict) else {}
        for mkey in ('videos', 'audios'):
            sub = 'video' if mkey == 'videos' else 'audio'
            for mat in mats.get(mkey, []) or []:
                mname = mat.get('material_name')
                if not mname:
                    continue
                # 若已有有效绝对路径且文件存在, 不动它
                cur = mat.get('path') or mat.get('media_path') or ''
                if cur and os.path.isfile(cur.replace('/', os.sep)):
                    continue
                # (a) 素材已在草稿 assets 子目录
                cand = os.path.join(new_fold, 'assets', sub, mname)
                if not os.path.isfile(cand):
                    # (b) 按原始上传文件名(remote_url)去上传目录找, 找到就拷进草稿 assets
                    # (拷贝而非直接引用: 草稿自包含, 上传目录后续清理也不影响渲染)
                    remote = (mat.get('remote_url') or '').strip()
                    rbase = os.path.basename(remote.replace('\\', '/')) if remote else ''
                    if rbase:
                        src_cand = os.path.join(config.UPLOAD_DIR, rbase)
                        if os.path.isfile(src_cand):
                            os.makedirs(os.path.dirname(cand), exist_ok=True)
                            shutil.copyfile(src_cand, cand)
                if os.path.isfile(cand):
                    fwd = cand.replace('\\', '/')
                    mat['path'] = fwd
                    if not mat.get('media_path'):
                        mat['media_path'] = fwd
        # 关键修复 2: 零时长视频段. 生成器拿不到素材时长时(ffprobe 不了裸文件名 remote_url),
        # 会留下 source/target duration=0 的 segment —— 剪映渲染该段就是纯黑屏+无声
        # (叠加段因显式传了 start/end 区间正常, 只有主视频这种"自动取全长"的段中招).
        # 路径回填后用 ffprobe 探真实时长, 把区间补成 [start, min(素材时长, 成片时间线)].
        def _probe_dur_us(path):
            try:
                out = subprocess.check_output(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'csv=p=0', path], stderr=subprocess.STDOUT).decode()
                return int(float(out.strip().splitlines()[0]) * 1000000)
            except Exception:
                return 0
        mat_by_id = {m.get('id'): m for m in (mats.get('videos') or [])}
        timeline_us = c.get('duration') or 0
        for tr in (c.get('tracks') or []):
            if tr.get('type') != 'video':
                continue
            for seg in (tr.get('segments') or []):
                st = seg.get('source_timerange') or {}
                tt = seg.get('target_timerange') or {}
                if (st.get('duration') or 0) > 0 and (tt.get('duration') or 0) > 0:
                    continue
                m = mat_by_id.get(seg.get('material_id'))
                if not m or not (m.get('path') and os.path.isfile(m['path'])):
                    continue
                dur_us = _probe_dur_us(m['path'])
                if dur_us <= 0:
                    continue
                # 按成片时间线截断: 主视频不该超出字幕/其他轨道定义的结尾 (探到的源常比成片长)
                if 0 < timeline_us < dur_us:
                    dur_us = timeline_us
                if (m.get('duration') or 0) <= 0:
                    m['duration'] = dur_us
                seg['source_timerange'] = {'start': st.get('start') or 0, 'duration': dur_us}
                seg['target_timerange'] = {'start': tt.get('start') or 0, 'duration': dur_us}
                log('修复零时长视频段: %s -> %.1fs' % (m.get('material_name'), dur_us / 1000000))
        json.dump(c, open(cp, 'w', encoding='utf-8'), ensure_ascii=False)
    log('注入草稿: %s -> %s (id=%s)' % (src_name, new_name, new_id))
    return dst

def _cleanup_injected_draft(draft_name):
    """删除注入到剪映草稿根目录的草稿文件夹 + 同步 root_meta_info.json 条目.
    成功与失败路径都要调 (失败不清理会残留 rd* 草稿, 下次启动弹"草稿丢失"对话框且
    首页越来越乱). 任何异常只记日志不抛出."""
    import shutil
    try:
        injected = os.path.join(DRAFT_ROOT, draft_name)
        if os.path.exists(injected):
            shutil.rmtree(injected, ignore_errors=True)
            log('已清理注入草稿: %s' % draft_name)
        rm_path = os.path.join(DRAFT_ROOT, 'root_meta_info.json')
        if os.path.exists(rm_path):
            m = json.load(open(rm_path, encoding='utf-8'))
            before = len(m.get('all_draft_store', []))
            m['all_draft_store'] = [d for d in m.get('all_draft_store', [])
                                    if d.get('draft_name') != draft_name]
            m['draft_ids'] = len(m['all_draft_store'])
            if len(m['all_draft_store']) < before:
                json.dump(m, open(rm_path, 'w', encoding='utf-8'), ensure_ascii=False)
                log('已同步 root_meta (移除 %s)' % draft_name)
    except Exception as e:
        log('清理注入草稿异常: %s' % e)


def find_main_pid():
    """主 UI 进程 = 有窗口标题的那个 JianyingPro.exe"""
    r = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         "Get-Process JianyingPro -ErrorAction SilentlyContinue | "
         "Where-Object { $_.MainWindowTitle -ne '' } | "
         "Select-Object -First 1 -ExpandProperty Id"],
        capture_output=True, text=True)
    out = r.stdout.strip()
    return int(out) if out.isdigit() else None

CURRENT_HDESK = None  # 桌面模式下, start_jianying_in_desktop() 拿到的目标桌面句柄 (供跨桌面操作用)

import contextlib

@contextlib.contextmanager
def _on_target_desktop():
    """桌面模式下, 把本线程临时切到目标桌面, 让 EnumWindows/GetWindowRect/MoveWindow 等
    User32 调用作用到隔离桌面里的真实窗口上 (这些调用默认只认本线程当前关联的桌面);
    用完切回原桌面, 不影响本线程后续其它操作(如键鼠模拟)."""
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not (DESKTOP_MODE and CURRENT_HDESK):
        yield False
        return
    orig_hdesk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    if not user32.SetThreadDesktop(CURRENT_HDESK):
        log('  SetThreadDesktop 失败 err=%d, 跳过跨桌面窗口操作' % ctypes.get_last_error())
        yield False
        return
    try:
        yield True
    finally:
        if orig_hdesk:
            user32.SetThreadDesktop(orig_hdesk)

def _real_click_global(gx, gy):
    """真实 OS 级点击 (SetCursorPos + mouse_event), 走完整 Windows 输入管线, 跟人工鼠标点击
    完全一致. 用于替代 frida 注入的 Qt handleMouseEvent 直调——实测直调对"导出确认"这类弹窗按钮
    能返回成功但压根不触发按钮的业务逻辑(可能是该弹窗内部还依赖了原生输入管线才会更新的状态),
    只有真实输入事件才能可靠触发. 桌面模式下会切到目标桌面操作, 不影响用户主桌面的鼠标.
    注意: 实测在隔离(非输入)桌面下, SetCursorPos/mouse_event 这类全局硬件输入模拟 API 即使
    SetThreadDesktop 切过去也不会真正送达该桌面上的窗口(这类 API 绑定的是当前"输入桌面",
    不是调用线程关联的桌面) —— 弹窗按钮请改用 _post_click_hwnd() (直接 PostMessage 到 HWND,
    不经过全局输入队列, 隔离桌面下也可靠)."""
    import ctypes
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    user32 = ctypes.windll.user32
    with _on_target_desktop():
        user32.SetCursorPos(int(gx), int(gy))
        time.sleep(0.1)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.08)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return True

def _post_click_hwnd(hwnd, lx, ly):
    """对指定 HWND 直接 PostMessage WM_MOUSEMOVE + WM_LBUTTONDOWN/UP (client 坐标, 物理像素).
    实测这是隔离桌面下唯一可靠触发 QtQuick 弹窗按钮业务逻辑的方式: frida 直调
    handleMouseEvent 对弹窗(非主窗口)的按钮不生效(不管坐标/焦点是否正确都无效), 全局
    SetCursorPos+mouse_event 在非输入桌面下也不会真正送达 —— 只有直接 PostMessage 到该
    HWND 的消息队列(不经过桌面级输入管线, 只要 HWND 有效就能送达)才行. lx,ly 是 Qt 逻辑
    坐标(相对窗口左上角), 内部按 GetDpiForWindow 换算成物理像素再发送."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    MK_LBUTTON = 0x0001

    def make_lparam(x, y):
        return (int(y) & 0xffff) << 16 | (int(x) & 0xffff)

    with _on_target_desktop():
        try:
            dpi = user32.GetDpiForWindow(hwnd)
            scale = dpi / 96.0 if dpi else 1.0
        except Exception:
            scale = 1.0
        px, py = lx * scale, ly * scale
        lparam = make_lparam(px, py)
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        time.sleep(0.08)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    return {'ok': True, 'scale': scale, 'physical': {'x': px, 'y': py}}

def find_jy_hwnd():
    """找最大可见剪映窗口 HWND. 假定调用方已经(如需要)切到正确桌面 (见 _on_target_desktop)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    all_h = []
    def cb(h, _):
        if user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, buf, 256)
            if '剪映专业版' in buf.value:
                r = wintypes.RECT(); user32.GetWindowRect(h, ctypes.byref(r))
                all_h.append((h, (r.right-r.left)*(r.bottom-r.top)))
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    if not all_h: return None
    all_h.sort(key=lambda x: x[1], reverse=True)
    return all_h[0][0]

# === 独立桌面模式 (真后台) ===
DESKTOP_MODE = False
# 实际桌面名 (config.DESKTOP_NAMES 默认 'JYRender_0'; OpenDesktop/CreateDesktop 用此名).
# 之前误写 'JYRender', 但系统上真实存在的桌面是 'JYRender_0', 导致 OpenDesktop 失败回退到
# CreateDesktop 新建一个空 'JYRender' 桌面, 与 render_server 用的 'JYRender_0' 不一致.
JY_DESKTOP = 'JYRender_0'

def start_jianying_in_desktop(desktop=JY_DESKTOP):
    """在独立桌面启动剪映, 返回 (pid, hDesk). 剪映在该桌面是前台, focusWindow 有效, 主桌面不被打扰."""
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
    hDesk = OpenDesktop(desktop, 0, True, GENERIC_ALL)
    if not hDesk:
        hDesk = CreateDesktop(desktop, None, None, 0, GENERIC_ALL, None)
        log('创建桌面: %s' % desktop)
    else:
        log('打开桌面: %s' % desktop)
    # 不杀所有剪映 (多桌面互不干扰). 若该桌面已有剪映(上次没关), 单例会复用.
    # 找 exe
    exe = config.find_jianying_exe()
    if not exe:
        log('未找到 JianyingPro.exe (检查 config.JY_APP_BASE)'); return None, hDesk
    # STARTUPINFO 指定 lpDesktop
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
    si.lpDesktop = desktop
    # 指定窗口位置+尺寸 (剪映可能部分遵守; 配合 resize 兜底)
    si.dwFlags = 0x00000001 | 0x00000080  # STARTF_USESHOWWINDOW | STARTF_USESIZE
    si.wShowWindow = 1  # SW_SHOWNORMAL
    si.dwXSize = PRESET_W; si.dwYSize = PRESET_H
    pi = PROCESS_INFORMATION()
    CreateProcessW = kernel32.CreateProcessW
    CreateProcessW.restype = wintypes.BOOL
    CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                               wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                               ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION)]
    ok = CreateProcessW(exe, None, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        log('启动剪映失败 err=%d' % ctypes.get_last_error()); return None, hDesk
    log('剪映启动在桌面 %s, PID=%d' % (desktop, pi.dwProcessId))
    return pi.dwProcessId, hDesk

def focus_jianying():
    """桌面模式下空操作 (剪映在独立桌面已是前台); 普通模式拉前台."""
    if DESKTOP_MODE:
        return None  # 独立桌面里剪映本就是前台, 不需要 SetForegroundWindow
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = find_jy_hwnd()
    if not hwnd: return None
    user32.ShowWindow(hwnd, 9)
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 0x0002, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return hwnd

def _activate_hwnd(hwnd):
    """把指定 HWND 拉到前台/激活 (桌面模式下临时切到目标桌面操作, 见 _on_target_desktop).
    新弹出的弹窗(如导出确认框)从未被 SetForegroundWindow 激活过, focusWindow() 一直是 null,
    导致真实点击第一下只是把窗口"点活"而不触发按钮逻辑(Windows 单击非激活窗口的经典问题),
    必须显式激活它一次再点."""
    import ctypes
    user32 = ctypes.windll.user32
    with _on_target_desktop():
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)

# 预设窗口尺寸 (校准坐标时用的尺寸; resize 到此尺寸保证坐标准)
PRESET_W, PRESET_H = config.PRESET_W, config.PRESET_H

def resize_jianying(w=PRESET_W, h=PRESET_H):
    """把剪映窗口(首页或编辑器, 无论当前是否最大化)强制 resize 成固定预设尺寸,
    保证不同机器/分辨率/缩放下, 校准坐标和渲染时坐标始终对得上.
    桌面模式下会临时切到目标桌面操作(见 _on_target_desktop), 保证隔离桌面里的窗口也生效. 返回 hwnd 或 None."""
    import ctypes
    from ctypes import wintypes
    SW_RESTORE = 9
    user32 = ctypes.windll.user32
    with _on_target_desktop():
        hwnd = find_jy_hwnd()
        if not hwnd: return None
        if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)  # 先取消最大化/最小化, 否则 MoveWindow 不生效或被弹回
            time.sleep(0.3)
        r = wintypes.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(r))
        cur_w, cur_h = r.right - r.left, r.bottom - r.top
        # 固定锚点到主屏 (0,0): 不保留原左上角位置 —— 之前 MoveWindow 保持 r.left/r.top,
        # 导致窗口每次打开落点不同 (甚至落到不同显示器/不同 DPI 缩放), 使校准的绝对全局
        # 坐标(gx,gy)在下次运行时整体偏移/失效. 固定锚点后窗口位置+尺寸每次都完全一致.
        if cur_w == w and cur_h == h and r.left == 0 and r.top == 0:
            return hwnd  # 已是预设尺寸+位置
        user32.MoveWindow(hwnd, 0, 0, w, h, True)
        time.sleep(0.5)
        log('resize 剪映窗口: (%d,%d)%dx%d -> (0,0)%dx%d' % (r.left, r.top, cur_w, cur_h, w, h))
        return hwnd

def resize_jianying_settled(w=PRESET_W, h=PRESET_H, attempts=6, interval=0.5):
    """编辑器刚打开时有些版本会延迟自我最大化/重新布局, 一次性 resize 可能被之后的
    自动最大化覆盖掉. 短时间内反复拉回固定尺寸, 直到真正稳定在预设尺寸再继续."""
    hwnd = None
    for _ in range(attempts):
        hwnd = resize_jianying(w, h)
        time.sleep(interval)
    return hwnd

def kill_jianying(pid=None):
    """关闭剪映进程. 指定 pid 只杀该进程; 否则杀所有 (慎用, 影响其他桌面)."""
    if pid:
        subprocess.run(['powershell', '-NoProfile', '-Command',
            'Stop-Process -Id %d -Force -ErrorAction SilentlyContinue' % pid],
            capture_output=True)
        log('已关闭剪映 PID=%d' % pid)
        return
    subprocess.run(['powershell', '-NoProfile', '-Command',
        "Get-Process JianyingPro -ErrorAction SilentlyContinue | Stop-Process -Force"],
        capture_output=True)
    log('已关闭剪映')

def start_jianying(exe_path=None):
    """启动剪映, 等主窗口就绪 (最多 60s). 返回 main pid 或 None"""
    if exe_path is None:
        exe_path = config.find_jianying_exe()
        if not exe_path:
            log('未找到 JianyingPro.exe (检查 config.JY_APP_BASE)')
            return None
    subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
    log('启动剪映: %s' % exe_path)
    # 等主窗口 (有 MainWindowTitle)
    for _ in range(60):
        time.sleep(1)
        pid = find_main_pid()
        if pid:
            log('剪映就绪, PID=%d' % pid)
            return pid
    log('剪映启动超时')
    return None

class Driver:
    def __init__(self):
        self.pid = None; self.session = None; self.script = None
        self.cond = threading.Condition()
        self.capture_count = 0
        self.last_capture = None
        self.monitor_proc = None

    def on_message(self, msg, data):
        if msg['type'] == 'error':
            log('[script err] %s' % msg.get('description')); return
        if msg['type'] != 'send': return
        p = msg['payload']
        if not isinstance(p, dict): return
        t = p.get('type')
        if t == 'captured' or t == 'real':
            info = p.get('info') or p
            with self.cond:
                self.capture_count += 1
                self.last_capture = info
                self.cond.notify_all()
            gx = info.get('gx'); gy = info.get('gy')
            if gx is None and 'global' in info:
                gx, gy = info['global'].get('x'), info['global'].get('y')
            if t == 'real' and info.get('type') == 5:
                return  # Move 太频繁, 不刷屏
            log('[REAL #%d] global(%s,%s)' % (self.capture_count, gx, gy))
        elif t == 'ready':
            log('[hook ready] %s' % p.get('hookAddr'))
        elif t == 'targets':
            log('[targets] %d' % len(p.get('list', [])))
        elif t == 'err':
            log('[hook err] %s' % p.get('msg'))

    def attach(self, pid=None):
        if pid is not None:
            self.pid = pid
        else:
            self.pid = find_main_pid()
        if not self.pid:
            log('ERROR: 没找到剪映主进程')
            return False
        log('主进程 PID=%d' % self.pid)
        self.session = frida.get_local_device().attach(self.pid)
        code = open(HOOK, encoding='utf-8').read()
        self.script = self.session.create_script(code)
        self.script.on('message', self.on_message)
        self.script.load()
        time.sleep(1.0)  # 等 ready
        return True

    def wait_any_event(self, timeout=600):
        """等任意真实鼠标事件 (含 Move), 返回最新事件 info"""
        with self.cond:
            self.cond.wait_for(lambda: self.capture_count > 0, timeout=timeout)
        return self.last_capture if self.capture_count > 0 else None

    def wait_fresh_win(self, timeout=120):
        """等一次真实鼠标事件 (刷新 hook 的 curWin/curDev), 返回 status"""
        before = self.capture_count
        with self.cond:
            self.cond.wait_for(lambda: self.capture_count > before, timeout=timeout)
        if self.capture_count <= before:
            return None
        try:
            return self.script.exports_sync.status()
        except Exception as e:
            log('  status err: %s' % e); return None

    def wait_click_from(self, base, timeout=600):
        """等一次 Press 点击事件 (type=2), base 为起始计数"""
        with self.cond:
            self.cond.wait_for(
                lambda: (self.capture_count > base
                         and self.last_capture
                         and self.last_capture.get('type') == 2),
                timeout=timeout)
        return self.last_capture if self.capture_count > base else None

    def click_global2(self, gx, gy):
        """先 focus 剪映 (拉前台), 再 click. focusWindow() 失焦时返回空, 必须先拉前台."""
        focus_jianying()
        r = self.script.exports_sync.click(float(gx), float(gy))
        log('  click(%.0f,%.0f) -> %s' % (gx, gy, r))
        return r

    def click_card_by_name(self, draft_name):
        """按草稿名在首页草稿网格里定位该草稿的卡片并 PostMessage 点击其中心, 打开该草稿.
        取代旧的固定 caps['card'] 像素坐标: inject_draft 把新草稿设为 tm_draft_create=now 排到
        首页第一张卡片, 但 caps['card'] 是早期对别的草稿布局校准的固定坐标, 会落到别的旧草稿
        (其视频素材 path 被清空 → 10006 "导出文件缺失"). 改成每次按 draft_name 在首页
        QQuickItem 树里找 QQuickText==draft_name 的文字节点, 取其卡片可点击祖先(MouseArea 90x90)
        几何 + 首页 HWND, 再 _post_click_hwnd 点中心 —— 跟 click_modal_button 同一套机制, 只是
        在首页大窗口(而非 lastShownWin 弹窗)里找. 返回 {ok, via, btn}."""
        btn = None
        for attempt in range(20):
            btn = self.script.exports_sync.findcard(draft_name)
            if isinstance(btn, dict) and btn.get('ok'):
                break
            time.sleep(0.5)
        if isinstance(btn, dict) and btn.get('ok'):
            hwnd = int(btn['hwnd'], 16)
            try:
                _activate_hwnd(hwnd)
            except Exception as e:
                log('  激活首页 hwnd 失败: %s' % e)
            lx, ly = btn['ax'] + btn['w'] / 2, btn['ay'] + btn['h'] / 2
            r = _post_click_hwnd(hwnd, lx, ly)
            log('  findcard(%s) -> btn=%s post click(local=%.0f,%.0f)=%s' %
                (draft_name, {'cls': btn.get('cls'), 'ax': btn['ax'], 'ay': btn['ay'], 'w': btn['w'], 'h': btn['h']}, lx, ly, r))
            return {'ok': True, 'via': 'findcard', 'btn': btn}
        log('  findcard(%s) 未找到: %s' % (draft_name, btn if isinstance(btn, dict) else btn))
        return {'ok': False, 'err': (btn.get('err') if isinstance(btn, dict) else None) or 'card not found by name'}

    def click_modal_button(self, want_texts, fallback_lx=None, fallback_ly=None):
        """按弹窗(lastShownWin)里按钮的文字精确定位并点击, 取代 calib.json 里固定像素坐标.
        原理: hook 侧用 Qt 内部 API 遍历弹窗的 QQuickItem 树(contentItem/childItems/文字),
        按 want_texts(候选文字列表, 如 ['导出']) 找到真实按钮几何位置(每次都重新定位, 不怕
        坐标漂移/DPI变化/弹窗布局变化), 再对该弹窗 HWND 直接 PostMessage 点击 —— 实测这是
        隔离桌面下唯一能触发这类弹窗按钮业务逻辑的方式(frida 直调 handleMouseEvent 对弹窗
        按钮不生效; SetCursorPos+mouse_event 这类全局输入模拟在非输入桌面下也送不到).
        找不到按钮时回退到 fallback_lx/fallback_ly (calib.json 里的旧坐标, 走真实 OS 点击).
        弹窗刚弹出的一小段时间里内容面板可能还没完全加载(实测会先出现一个精简/加载中的版本,
        按钮文字还不存在), 所以按文字找不到时先重试几次再回退, 避免过早误判成"没有这个按钮"."""
        focus_jianying()
        btn = None
        for attempt in range(20):
            btn = self.script.exports_sync.findmodalbutton(json.dumps(want_texts, ensure_ascii=False))
            if btn.get('ok'):
                break
            time.sleep(0.5)
        if btn.get('ok'):
            hwnd = int(btn['hwnd'], 16)
            # 必须先激活弹窗再 PostMessage 点击: 新弹出的导出确认框从未被
            # SetForegroundWindow 激活过(focusWindow() 一直是 null), 直接 PostMessage
            # 的第一下只是把窗口"点活"而不触发按钮逻辑(Windows 单击非激活窗口的经典问题),
            # 表现为按钮坐标正确但 temp_bytes 永远是 0(渲染不启动). 见 _activate_hwnd 注释.
            try:
                _activate_hwnd(hwnd)
            except Exception as e:
                log('  激活弹窗 hwnd 失败: %s' % e)
            lx, ly = btn['ax'] + btn['w'] / 2, btn['ay'] + btn['h'] / 2
            r = _post_click_hwnd(hwnd, lx, ly)
            log('  findmodalbutton%s -> btn=%s post click(local=%.0f,%.0f)=%s' %
                (want_texts, {'cls': btn.get('cls'), 'ax': btn['ax'], 'ay': btn['ay'], 'w': btn['w'], 'h': btn['h']}, lx, ly, r))
            return {'ok': True, 'via': 'findmodalbutton', 'btn': btn}
        log('  findmodalbutton%s 未找到: %s' % (want_texts, btn.get('err')))
        if fallback_lx is None or fallback_ly is None:
            return {'ok': False, 'err': btn.get('err', 'button not found, no fallback coords')}
        log('  回退到固定坐标 local(%.0f,%.0f)' % (fallback_lx, fallback_ly))
        return self.click_modal(fallback_lx, fallback_ly)

    def click_main_button(self, want_texts, fallback_lx=None, fallback_ly=None):
        """按主窗口(编辑器/首页, 跟 findmodalbutton 特意排除的那类同尺寸)工具栏按钮的文字定位并点击.
        原因: 实测 frida 注入的 click()(handleMouseEvent 直调)对"点击后会打开新弹窗"这类操作
        (比如导出按钮打开导出确认框)不总可靠——有时返回成功但弹窗压根没出现(概率性失败,
        跟确认框按钮当初的问题同源). 改成跟弹窗按钮一样的方案: 按文字动态定位坐标, 再用
        PostMessage 直接点该窗口 HWND, 不经过 frida 注入的 Qt 事件管线, 更稳定.
        找不到按钮时回退到 fallback_lx/fallback_ly (calib.json 里的旧坐标, 走 frida click())."""
        focus_jianying()
        btn = None
        for attempt in range(20):
            btn = self.script.exports_sync.findmainbutton(json.dumps(want_texts, ensure_ascii=False))
            if btn.get('ok'):
                break
            time.sleep(0.5)
        if btn.get('ok'):
            hwnd = int(btn['hwnd'], 16)
            # 先激活目标窗口再 PostMessage 点击 (与 click_modal_button 同理, 见 _activate_hwnd 注释).
            # 尤其点击工具栏导出按钮要打开新弹窗, 窗口若未被激活过, 点击只"点活"不触发逻辑.
            try:
                _activate_hwnd(hwnd)
            except Exception as e:
                log('  激活主窗口 hwnd 失败: %s' % e)
            lx, ly = btn['ax'] + btn['w'] / 2, btn['ay'] + btn['h'] / 2
            r = _post_click_hwnd(hwnd, lx, ly)
            log('  findmainbutton%s -> btn=%s post click(local=%.0f,%.0f)=%s' %
                (want_texts, {'cls': btn.get('cls'), 'ax': btn['ax'], 'ay': btn['ay'], 'w': btn['w'], 'h': btn['h']}, lx, ly, r))
            return {'ok': True, 'via': 'findmainbutton', 'btn': btn}
        log('  findmainbutton%s 未找到: %s' % (want_texts, btn.get('err')))
        if fallback_lx is None or fallback_ly is None:
            return {'ok': False, 'err': btn.get('err', 'button not found, no fallback coords')}
        log('  回退到固定坐标 local(%.0f,%.0f) (frida click)' % (fallback_lx, fallback_ly))
        return self.click_global2(fallback_lx, fallback_ly)

    def click_modal(self, lx, ly):
        """confirm/close_done 在弹窗(modal)上, 用真实 OS 级点击 (_real_click_global):
        先问 hook 弹窗当前原点(modalorigin, 只读不点), 再用真实坐标做 SetCursorPos+mouse_event.
        已知问题: 这条路径在隔离(非输入)桌面下不可靠(SetCursorPos/mouse_event 这类全局输入
        模拟 API 不会真正送达非输入桌面上的窗口), 仅作为 click_modal_button() 找不到按钮时
        的回退, 优先用 click_modal_button() (按文字用 Qt 内部 API 定位 + PostMessage 到 HWND)."""
        focus_jianying()
        origin = self.script.exports_sync.modalorigin()
        if not origin.get('ok'):
            log('  modalorigin 失败: %s' % origin.get('err'))
            return {'ok': False, 'err': origin.get('err', 'no modal window')}
        hw = self.script.exports_sync.modalhwnd()
        if hw.get('ok'):
            try:
                _activate_hwnd(int(hw['hwnd'], 16))
            except Exception as e:
                log('  激活弹窗 hwnd 失败: %s' % e)
        else:
            log('  modalhwnd 失败: %s' % hw.get('err'))
        gx, gy = origin['x'] + lx, origin['y'] + ly
        _real_click_global(gx, gy)
        log('  realclick(local=%.0f,%.0f origin=%s -> global=%.0f,%.0f)' % (lx, ly, origin, gx, gy))
        return {'ok': True, 'global': {'x': gx, 'y': gy}, 'win': origin.get('win')}


    def type_text(self, text):
        """键盘输入. 桌面模式: hook 内 PostMessage (typewm); 普通模式: Win32 PostMessage."""
        if DESKTOP_MODE:
            r = self.script.exports_sync.typewm(text)
            log('  typewm(%r) -> %s' % (text, r))
            return r
        import ctypes
        user32 = ctypes.windll.user32
        WM_CHAR = 0x0102
        target = find_jy_hwnd()
        if not target:
            log('  type 失败: 没找到剪映窗口'); return {'ok': False, 'err': 'no hwnd'}
        n = 0
        for ch in text:
            user32.PostMessageW(target, WM_CHAR, ord(ch) & 0xffff, 0)
            n += 1
            time.sleep(0.03)
        log('  type(%r) -> %d chars via WM_CHAR hwnd=%d' % (text, n, target))
        return {'ok': True, 'chars': n, 'hwnd': target}

    def click_local(self, lx, ly):
        """用 local 坐标点击 (相对窗口左上角, 窗口移位稳定)"""
        r = self.script.exports_sync.clicklocal(float(lx), float(ly))
        log('  clicklocal(%.0f,%.0f) -> %s' % (lx, ly, r))
        return r

    def click_global(self, gx, gy):
        """用 global 坐标点击 (旧, 窗口移位会偏)"""
        r = self.script.exports_sync.clickglobal(float(gx), float(gy))
        log('  clickglobal(%.0f,%.0f) -> %s' % (gx, gy, r))
        return r

    def get_counts(self):
        """当前窗口创建/显示计数"""
        st = self.script.exports_sync.status()
        return (st.get('winCreateCount', 0), st.get('winShowCount', 0))

    def clicked_new_window(self, base, wait=2.5, polls=8):
        """点击后自我验证: 等 wait 秒, 查是否弹出新窗口. 返回新增窗口数"""
        for _ in range(polls):
            time.sleep(wait / polls)
            bc, bs = base
            d = self.script.exports_sync.windiff(int(bc), int(bs))
            if d.get('newShown', 0) > 0 or d.get('newCreated', 0) > 0:
                log('  ✓ 检测到新窗口: created+%d shown+%d' % (d['newCreated'], d['newShown']))
                return d
        log('  ✗ 无新窗口出现 (created+%d shown+%d)' % (d['newCreated'], d['newShown']))
        return None

    def get_shown_count(self):
        """当前 setVisible(true) 的累计计数"""
        try:
            r = self.script.exports_sync.lastshown()
            return r.get('showCount', 0)
        except Exception:
            return 0

    def wait_new_window(self, base_count, timeout=10):
        """等 showCount 超过 base_count (有新窗口显示). 返回新 showCount 或 None"""
        start = time.time()
        while time.time() - start < timeout:
            c = self.get_shown_count()
            if c > base_count:
                return c
            time.sleep(0.3)
        return None

    def wait_editor_ready(self, base_show, timeout=30):
        """点结果卡片后等编辑器就绪: showCount 增加(草稿打开触发SHOW) + 稳定1.5s无新SHOW."""
        # 1. 等 showCount 增加 (草稿打开触发窗口)
        if not self.wait_new_window(base_show, timeout=timeout):
            log('  ⚠ 编辑器无新窗口, 仍继续'); return False
        # 2. 等稳定 (1.5秒无新 SHOW = 加载完)
        last = self.get_shown_count(); stable = 0
        for _ in range(30):
            time.sleep(0.3)
            cur = self.get_shown_count()
            if cur == last:
                stable += 1
                if stable >= 5:  # 1.5秒稳定
                    log('  ✓ 编辑器就绪 (showCount=%d 稳定)' % cur); return True
            else:
                stable = 0; last = cur
        log('  ⚠ 编辑器窗口未稳定, 仍继续'); return False

    def start_monitor(self):
        try: os.remove(MONITOR_LOG)
        except: pass
        self.monitor_proc = subprocess.Popen(
            [sys.executable, MONITOR_PY], cwd=SCRIPT_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log('render_monitor 启动')

    def stop_monitor(self):
        if self.monitor_proc:
            try: self.monitor_proc.terminate()
            except: pass
            self.monitor_proc = None

    def wait_render_done(self, draft_name=None, timeout=300):
        """等目标 mp4 生成. draft_name 给定则按 <draft_name>*.mp4 前缀匹配;
        draft_name 为 None 则等任意新出现的 mp4 (mtime 晚于调用前, 用于 calibrate/run).
        判断: mp4 出现 + 大小稳定 (>100KB 且 ~3s 不变) = 完成."""
        VIDEOS = config.VIDEOS_DIR
        TEMP = os.path.join(VIDEOS, '.__jianying_export_temp_folder__')
        log('等待 %s*.mp4 生成 (最多 %ds)...' % (draft_name or '*', timeout))
        start = time.time()
        base_mtime = 0.0
        if not draft_name:
            try:
                existing = [os.path.join(VIDEOS, f) for f in os.listdir(VIDEOS)
                            if f.endswith('.mp4')]
                base_mtime = max([os.path.getmtime(p) for p in existing], default=0.0)
            except Exception:
                base_mtime = 0.0
        last_size = 0; stable_count = 0
        while time.time() - start < timeout:
            # 导出进度: 临时目录 mp4 字节数增长 (渲染进行中)
            temp_bytes = 0
            try:
                for f in os.listdir(TEMP):
                    if f.endswith('.mp4'):
                        temp_bytes += os.path.getsize(os.path.join(TEMP, f))
            except Exception:
                pass
            try:
                if draft_name:
                    files = [f for f in os.listdir(VIDEOS)
                             if f.endswith('.mp4') and f.startswith(draft_name)]
                else:
                    files = [f for f in os.listdir(VIDEOS)
                             if f.endswith('.mp4') and os.path.getmtime(os.path.join(VIDEOS, f)) > base_mtime]
            except: files = []
            if files:
                # 取最新的
                files.sort(key=lambda f: os.path.getmtime(os.path.join(VIDEOS, f)), reverse=True)
                p = os.path.join(VIDEOS, files[0])
                sz = os.path.getsize(p)
                if sz > 100000:  # >100KB 才算 (排除空壳)
                    if sz == last_size:
                        stable_count += 1
                    else:
                        stable_count = 0; last_size = sz
                    if stable_count >= 2:  # 稳定 2 次 (~3s)
                        log('渲染完成! %s size=%d' % (files[0], sz)); return True
            emit_progress('rendering', None, elapsed=int(time.time() - start),
                          temp_bytes=temp_bytes)
            time.sleep(1.5)
        log('等待渲染超时'); return False

    def _final_mp4_exists(self, draft_name):
        """直接查最终 mp4 是否已生成(按草稿名前缀匹配, >100KB 算有效).
        wait_render_done 依赖"文件出现后再连续采样大小稳定", 大文件渲染时文件落盘/搬运
        可能压着超时线, 采样追不上就误判失败 —— 这个只查存在性, 作最终兜底判定."""
        try:
            files = [f for f in os.listdir(config.VIDEOS_DIR)
                     if f.endswith('.mp4') and f.startswith(draft_name)]
        except Exception:
            return False
        for f in files:
            try:
                if os.path.getsize(os.path.join(config.VIDEOS_DIR, f)) > 100000:
                    return True
            except Exception:
                continue
        return False

    def wait_render_start(self, timeout=15):
        """等 temp 文件出现 (渲染开始). 用于 confirm 后验证点中了."""
        TEMP = os.path.join(config.VIDEOS_DIR, '.__jianying_export_temp_folder__')
        start = time.time()
        while time.time() - start < timeout:
            try: temps = [f for f in os.listdir(TEMP) if f.endswith('.mp4')]
            except: temps = []
            if temps:
                return True
            time.sleep(0.5)
        return False

    def _wait_click_countdown(self, base, name, timeout=600):
        """等一次 Press 点击 (type==2), 期间每 5s 输出倒计时日志."""
        start = time.time()
        log('    开始等待点击【%s】, 最多 %ds...' % (name, timeout))
        last_log = 0.0
        while time.time() - start < timeout:
            with self.cond:
                self.cond.wait(timeout=1.0)
                if (self.capture_count > base and self.last_capture
                        and self.last_capture.get('type') == 2):
                    return self.last_capture
            elapsed = time.time() - start
            if elapsed - last_log >= 5:
                last_log = elapsed
                log('    >>> 等待点击【%s】: 已等 %ds / 剩余 %ds'
                    % (name, int(elapsed), int(timeout - elapsed)))
        return None

    def calibrate(self):
        log('=== 校准模式: 依次点击 5 个位置 (每步 600s) ===')
        log('    请跟随日志提示, 依次用鼠标点击剪映中的对应位置。')
        resize_jianying()  # 先固定窗口到预设尺寸, 保证校准坐标与渲染时一致
        time.sleep(2)  # 等剪映按新尺寸重新布局 (草稿卡片位置刷新)
        steps = [
            ('card',         '草稿卡片', '首页里你要渲染的那个草稿 (若不在首页先点左上角返回)'),
            ('export',       '导出按钮', '进入编辑器后, 点顶部的"导出"'),
            ('confirm',      '导出确认', '导出弹窗里点"导出"开始渲染'),
            ('close_done',   '关闭导出完成窗口', '渲染完成后, 点导出完成弹窗的关闭/完成按钮'),
            ('close_editor', '关闭编辑窗口', '关闭编辑器返回首页 (左上角返回/关闭)'),
        ]
        caps = {}
        # 简短等一次鼠标事件做 dev 种子 (3s, 超时也继续; dev 由 hook 程序化获取)
        self.wait_any_event(timeout=3)
        for i, (key, name, desc) in enumerate(steps):
            log('[%d/%d] >>> 请点击【%s】 — %s' % (i + 1, len(steps), name, desc))
            base = self.capture_count
            cap = self._wait_click_countdown(base, name, timeout=600)
            if not cap:
                log('TIMEOUT: 没捕获到 %s, 退出' % key)
                if caps:
                    json.dump(caps, open(CALIB_FILE, 'w', encoding='utf-8'),
                              ensure_ascii=False, indent=2)
                return False
            gx, gy = cap.get('gx'), cap.get('gy')
            if gx is None and cap.get('global'):
                gx, gy = cap['global']['x'], cap['global']['y']
            caps[key] = {'lx': cap.get('lx'), 'ly': cap.get('ly'),
                         'gx': cap.get('gx'), 'gy': cap.get('gy'),
                         'win': cap.get('win'), 'dev': cap.get('dev')}
            json.dump(caps, open(CALIB_FILE, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=2)
            log('  OK 已保存 %s local(%s,%s) global(%s,%s)' %
                (key, cap.get('lx'), cap.get('ly'), cap.get('gx'), cap.get('gy')))
            # 等剪映界面切换完成再进入下一步
            if key == 'card':
                time.sleep(5)   # 点草稿后等编辑器打开
                resize_jianying_settled()  # 编辑器常默认最大化打开(有些还会延迟自我最大化), 反复拉回固定尺寸再继续
            elif key == 'export':
                time.sleep(3)   # 点导出后等导出窗口弹出
            elif key == 'confirm':
                self.wait_render_done(timeout=600)  # 等渲染完成
            elif key == 'close_done':
                time.sleep(2)   # 等完成窗口关闭
            elif key == 'close_editor':
                time.sleep(2)   # 等回到首页
        log('CALIB DONE -> %s' % CALIB_FILE)
        return True

    def run(self, count=1):
        log('=== 自动运行: 渲染 %d 次 (focusWindow + 程序化 dev) ===' % count)
        if not os.path.exists(CALIB_FILE):
            log('没有 calib.json, 先 calibrate'); return False
        caps = json.load(open(CALIB_FILE, encoding='utf-8'))
        resize_jianying()  # 保证窗口尺寸 = 校准时尺寸, 坐标才准
        for k in ('card', 'export', 'confirm'):
            if k not in caps:
                log('calib 缺 %s' % k); return False
        # dev 由 hook 启动时程序化获取 (primaryPointingDevice), 无需手动种子
        st = self.script.exports_sync.status()
        log('  dev=%s (程序化)' % st.get('curDev'))
        if not st.get('curDev'):
            log('dev 未获取, 中止'); return False
        for i in range(count):
            log('--- 第 %d/%d 个 ---' % (i + 1, count))
            log('点草稿卡片'); self.click_global2(caps['card']['lx'], caps['card']['ly'])
            time.sleep(6)
            resize_jianying_settled()  # 编辑器常默认最大化打开(有些还会延迟自我最大化), 反复拉回固定尺寸再继续
            log('点导出按钮'); self.click_main_button(['导出'], caps['export']['lx'], caps['export']['ly'])
            time.sleep(3)
            log('点确认按钮(modal)'); self.click_modal_button(['导出'], caps['confirm']['lx'], caps['confirm']['ly'])
            if not self.wait_render_done(timeout=300):
                log('渲染未完成, 中止'); break
            time.sleep(2)
            # 关闭完成提示 + 关编辑器回首页 (连续渲染下一个需要)
            if 'close_done' in caps:
                log('点关闭完成提示(modal)'); self.click_modal_button(['完成', '关闭', '确定'], caps['close_done']['lx'], caps['close_done']['ly'])
                time.sleep(1)
            if 'close_editor' in caps:
                log('点关闭编辑器(回首页)'); self.click_global2(caps['close_editor']['lx'], caps['close_editor']['ly'])
                time.sleep(2)
        log('ALL DONE')
        return True

    def detach(self):
        try:
            if self.session: self.session.detach()
        except: pass

    def render_draft(self, src_draft_dir, draft_name=None, search_term=None):
        """渲染指定草稿: 注入(让剪映识别) → 搜索定位 → 打开 → 导出.
        draft_name: 注入后的草稿名 (默认 源名_render)
        search_term: 搜索关键词 (默认 = draft_name). 用唯一名字保证结果唯一."""
        import shutil
        if not os.path.exists(CALIB_FILE):
            log('没有 calib.json, 先 calibrate'); return False
        caps = json.load(open(CALIB_FILE, encoding='utf-8'))
        # 桌面模式直接点首页卡片, 无需搜索坐标; 前台模式才需要 search_btn/search_box/result_card
        if DESKTOP_MODE:
            need = ('card', 'export', 'confirm')
        else:
            need = ('search_btn', 'search_box', 'result_card', 'export', 'confirm')
        for k in need:
            if k not in caps:
                log('calib 缺 %s' % k); return False

        resize_jianying()  # 保证窗口尺寸 = 校准时尺寸, 坐标才准

        # 1. 注入草稿 (用唯一英文名, 避免搜索歧义 + 键盘输入简单)
        src_name = os.path.basename(os.path.abspath(src_draft_dir))
        if draft_name is None:
            draft_name = 'rd%d' % int(time.time() * 1000)  # 纯英文数字, 唯一
        if search_term is None:
            search_term = draft_name
        inject_draft(src_draft_dir, draft_name)
        emit_progress('inject', 10)
        time.sleep(2)  # 等剪映实时识别

        # dev 程序化
        st = self.script.exports_sync.status()
        log('  dev=%s' % st.get('curDev'))
        if not st.get('curDev'):
            log('dev 未获取, 中止'); _cleanup_injected_draft(draft_name); return False

        # 2. 打开草稿: 桌面模式按草稿名在首页网格里定位卡片点击(不再用固定 caps['card'] 坐标),
        # 前台模式用搜索
        if DESKTOP_MODE:
            # 桌面模式: 按草稿名(draft_name)在首页 QQuickItem 树里找该草稿的卡片, 取其 MouseArea
            # 几何 + 首页 HWND, _activate_hwnd 后 _post_click_hwnd 点卡片中心. 这取代了旧的固定
            # caps['card'] 像素坐标 —— 那个坐标是早期对别的草稿布局校准的, inject_draft 让新草稿
            # 排到首页第一张卡片后, 旧坐标会落到别的旧草稿(视频 path 被清空 → 10006 缺失文件),
            # 每次打开的都是错草稿. 按名定位确保打开的就是刚注入的草稿. findcard 不经过 frida
            # 注入的点击管线(只读树), _post_click_hwnd 直接 PostMessage 到 HWND, 不会摧毁 session.
            opened = False
            for card_attempt in range(8):
                base_show = self.get_shown_count()
                log('点草稿卡片(尝试%d) — findcard 按名定位 %s' % (card_attempt + 1, draft_name))
                res = self.click_card_by_name(draft_name)
                if not res.get('ok'):
                    log('  findcard 失败: %s' % res.get('err'))
                    time.sleep(1); continue
                if self.wait_editor_ready(base_show, timeout=12):
                    opened = True; break
                log('  草稿未打开, 重试')
                time.sleep(1)
            if not opened:
                log('草稿卡片8次未打开, 中止'); _cleanup_injected_draft(draft_name); return False
        else:
            log('点放大镜'); self.click_global2(caps['search_btn']['lx'], caps['search_btn']['ly'])
            time.sleep(1)
            log('点搜索框'); self.click_global2(caps['search_box']['lx'], caps['search_box']['ly'])
            time.sleep(0.8)
            log('输入草稿名: %s' % search_term); self.type_text(search_term)
            time.sleep(2)
            log('点结果卡片'); self.click_global2(caps['result_card']['lx'], caps['result_card']['ly'])
            base_show = self.get_shown_count()
            self.wait_editor_ready(base_show, timeout=30)
        resize_jianying_settled()  # 编辑器常默认最大化打开(有些还会延迟自我最大化), 反复拉回固定尺寸再继续 (桌面模式下 hwnd 找不到, 是 no-op)
        emit_progress('open', 30)
        time.sleep(1)

        # 3. 导出: 点导出 → 等 modal → 点 confirm → 等 mp4 (失败重试, 每次重试重新点导出刷 modal)
        ok = False
        for attempt in range(4):
            base_show = self.get_shown_count()
            log('点导出按钮 (尝试%d)' % (attempt + 1))
            self.click_main_button(['导出'], caps['export']['lx'], caps['export']['ly'])
            new_show = self.wait_new_window(base_show, timeout=8)
            if new_show:
                log('  ✓ 导出窗口出现 (showCount %d->%d)' % (base_show, new_show))
            else:
                log('  ⚠ 导出窗口未出现, 重试')
                time.sleep(1); continue
            emit_progress('export', 50)
            time.sleep(0.8)  # 等 modal 完全显示
            # confirm 在 modal (lastShownWin = 最近显示的 = 导出窗口)
            st = self.script.exports_sync.lastshown()
            log('  modal win=%s (showCount=%d)' % (st.get('win'), st.get('showCount')))
            log('点确认按钮(modal)'); self.click_modal_button(['导出'], caps['confirm']['lx'], caps['confirm']['ly'])
            emit_progress('confirm', 60)
            # 等渲染完成. 之前用 20s 短等: 大草稿(如 141MB 输出)编码 ~18s + 落盘/搬运耗时,
            # wait_render_done 要文件出现后再连续 2 次采样大小不变(~4.5s)才判完成, 20s 会
            # 差几十秒误判"未触发渲染"→ 重试点导出(此时剪映已进入完成态弹不出导出框)→ 整轮
            # 误报失败, 但 mp4 其实已生成. wait_render_done 成功即刻返回, 放长超时不拖慢成功路径.
            if self.wait_render_done(draft_name, timeout=90):
                ok = True; break
            # 兜底: wait 超时不等于失败 —— 直接查最终 mp4 是否已存在(>100KB 即算),
            # 避免上面说的"文件刚好压线生成完但采样没追上"的误判.
            if draft_name and self._final_mp4_exists(draft_name):
                log('  ✓ wait 超时但 mp4 已存在, 判定成功')
                ok = True; break
            log('  ⚠ confirm 未触发渲染 (attempt %d), 重新点导出' % (attempt + 1))
        if not ok:
            # 最终兜底: 4 轮都没等到也要再直接查一次文件, 防止把已成功的渲染误报为失败.
            if draft_name and self._final_mp4_exists(draft_name):
                log('  ✓ 重试循环结束后发现 mp4 已存在, 判定成功')
                ok = True
            else:
                log('confirm 4次未触发渲染, 中止'); _cleanup_injected_draft(draft_name); return False
        emit_progress('done', 100)

        # 4. 关闭完成提示 (完成窗口 modal) + 关编辑器回首页
        if ok and 'close_done' in caps:
            base_show = self.get_shown_count()
            # 等完成窗口出现
            new_show = self.wait_new_window(base_show, timeout=8)
            if new_show:
                log('  ✓ 完成窗口出现 (showCount %d->%d)' % (base_show, new_show))
            time.sleep(1)
            log('点关闭完成提示(modal)'); self.click_modal_button(['完成', '关闭', '确定'], caps['close_done']['lx'], caps['close_done']['ly'])
            time.sleep(1.5)
        if ok and 'close_editor' in caps:
            log('点关闭编辑器(回首页)'); self.click_global2(caps['close_editor']['lx'], caps['close_editor']['ly'])
            time.sleep(2)

        # 5. 清理注入的草稿 (文件夹 + root_meta 条目, 避免下次启动弹"丢失"对话框)
        _cleanup_injected_draft(draft_name)

        return ok

def main():
    global DESKTOP_MODE, CURRENT_HDESK
    mode = sys.argv[1] if len(sys.argv) > 1 else 'calibrate'
    close_after = '--close' in sys.argv
    desktop_mode = '--desktop' in sys.argv

    if mode == 'kill':
        kill_jianying(); sys.exit(0)
    if mode == 'start':
        pid = start_jianying(); sys.exit(0 if pid else 1)

    # 桌面模式: 在独立桌面启动剪映 (真后台, 主桌面不打扰)
    attach_pid = None
    self_started_pid = None  # 自己启动的剪映 PID (渲染完要 kill, 不影响其他桌面)
    desk_name = JY_DESKTOP
    for i, a in enumerate(sys.argv):
        if a == '--pid' and i + 1 < len(sys.argv):
            try: attach_pid = int(sys.argv[i + 1])
            except: pass
        if a == '--desktop-name' and i + 1 < len(sys.argv):
            desk_name = sys.argv[i + 1]
    if desktop_mode:
        DESKTOP_MODE = True
        if attach_pid is None:
            # 没指定 pid, 自己启动剪映到指定桌面
            attach_pid, hDesk = start_jianying_in_desktop(desk_name)
            if not attach_pid:
                log('桌面启动剪映失败, 中止'); sys.exit(1)
            self_started_pid = attach_pid  # 记录, 渲染完 kill
            CURRENT_HDESK = hDesk  # 供 resize_jianying 跨桌面操作窗口用
            log('等剪映首页加载 30s...')
            time.sleep(30)
        else:
            log('桌面模式 attach 预启动剪映 PID=%d' % attach_pid)

    d = Driver()
    try:
        try:
            if not d.attach(pid=attach_pid):
                sys.exit(1)
        except Exception as e:
            # attach 失败(如 frida VirtualAllocEx 报错)必须走到 finally 清理自己启动的剪映,
            # 否则孤儿进程占着桌面/单实例锁, 下一次渲染会因为"重复实例"而必然再失败一次.
            log('attach 剪映失败: %r' % e)
            sys.exit(1)
        d.start_monitor()
        if mode == 'calibrate':
            ok = d.calibrate()
            sys.exit(0 if ok else 2)
        elif mode == 'run':
            cnt = int(sys.argv[2]) if (len(sys.argv) > 2 and sys.argv[2].isdigit()) else 1
            ok = d.run(count=cnt)
            if close_after and ok:
                log('渲染完成, 关闭剪映...')
                d.detach()
                kill_jianying(self_started_pid)  # 桌面模式只杀自己的实例
        elif mode == 'render-once':
            # 完整外部触发流程: 确保剪映开 → 渲染 → 关闭
            ok = d.run(count=1)
            if ok:
                log('渲染完成, 关闭剪映...')
                d.detach()
                kill_jianying(self_started_pid)
            sys.exit(0 if ok else 2)
        elif mode == 'render-draft':
            # 渲染指定草稿: 注入 + 搜索 + 打开 + 导出
            # 过滤掉 -- 开头的开关及其紧跟的值 (--desktop-name <值> 等), 只留位置参数.
            raw = sys.argv[2:]
            value_opts = ('--desktop-name', '--pid')  # 这些开关后面跟一个值, 需连值一起跳过
            args = []
            i = 0
            while i < len(raw):
                a = raw[i]
                if a in value_opts:
                    i += 2; continue           # 跳过开关 + 它的值
                if a.startswith('--'):
                    i += 1; continue            # 跳过无值开关
                args.append(a); i += 1
            if not args:
                log('用法: render-draft <草稿文件夹路径> [搜索名]'); sys.exit(2)
            src = args[0]
            name = args[1] if len(args) > 1 else None
            ok = d.render_draft(src, draft_name=name)
            if close_after and ok:
                log('渲染完成, 关闭剪映...')
                d.detach()
                kill_jianying(self_started_pid)
            sys.exit(0 if ok else 2)
        else:
            log('用法:')
            log('  python render_driver.py calibrate        # 校准坐标')
            log('  python render_driver.py run [N] [--close] # 渲染首页第N个草稿')
            log('  python render_driver.py render-once      # 外部触发: 渲染1个+关剪映')
            log('  python render_driver.py render-draft <路径> [名字] [--close]  # 渲染指定草稿(注入+搜索)')
            log('  python render_driver.py kill | start     # 关/开 剪映')
            log('  --desktop 加在任意渲染命令后 = 真后台 (剪映在独立桌面, 你在主桌面自由工作)')
    finally:
        d.stop_monitor()
        d.detach()
        # 桌面模式: kill 自己启动的剪映 (不影响其他桌面的剪映)
        if self_started_pid:
            try:
                subprocess.run(['powershell','-NoProfile','-Command',
                    "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % self_started_pid],
                    capture_output=True, timeout=10)
                log('已关闭桌面剪映 PID=%d' % self_started_pid)
            except: pass

if __name__ == '__main__':
    main()
