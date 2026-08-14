// hook_focus.js — 终极版: focusWindow() + local 坐标 + 程序化 dev
// 所有函数地址动态查找 (按导出名), PID/基址变化都能用
var DIR = 'C:/Users/Administrator/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft/ym/';
var logfile = null;
try { logfile = new File(DIR + 'focus_' + Process.id + '.txt', 'w'); } catch (e) {}
function wlog(l) { if (logfile) { try { logfile.write(l + '\n'); logfile.flush(); } catch (e) {} } }

// 动态找导出 (按 mangled 名子串匹配)
function findExport(substr) {
    var found = null;
    Process.getModuleByName('Qt6Gui.dll').enumerateExports().forEach(function (e) {
        if (!found && (e.name || '').indexOf(substr) >= 0) found = e.address;
    });
    return found;
}

var fnFocus    = new NativeFunction(findExport('?focusWindow@QGuiApplication@@SAPEAVQWindow@@XZ'), 'pointer', []);
var fnWinX     = new NativeFunction(findExport('?x@QWindow@@QEBAHXZ'), 'int', ['pointer']);
var fnWinY     = new NativeFunction(findExport('?y@QWindow@@QEBAHXZ'), 'int', ['pointer']);
var fnPrimDev  = new NativeFunction(findExport('?primaryPointingDevice@QPointingDevice@@SAPEBV1@AEBVQString@@@Z'), 'pointer', ['pointer']);
// winId (拿 HWND, 桌面模式 typewm 用). 名放宽匹配, null 时不创建
var winIdAddr = findExport('?winId@QWindow');
var fnWinId = winIdAddr ? new NativeFunction(winIdAddr, 'pointer', ['pointer']) : null;
// setGeometry (桌面模式固定窗口位置, 避免 Windows 随机放置导致坐标偏)
var setGeomAddr = findExport('?setGeometry@QWindow@@QEAAXHHHH@Z');
var fnSetGeom = setGeomAddr ? new NativeFunction(setGeomAddr, 'void', ['pointer','int','int','int','int']) : null;
var FIXED_X = 403, FIXED_Y = 108, FIXED_W = 1280, FIXED_H = 720;  // 校准时的窗口位置
// Win32 PostMessage (用 Process.getModuleByName 避免 getExportByName(null) 兼容问题)
var fnPostMessage = null;
try {
    var pmAddr = Process.getModuleByName('user32.dll').getExportByName('PostMessageW');
    fnPostMessage = new NativeFunction(pmAddr, 'int', ['pointer', 'uint', 'pointer', 'pointer']);
} catch (e) { wlog('PostMessage bind err: ' + e); }
wlog('focus/x/y bound; winId=' + (winIdAddr||'NULL') + ' postMsg=' + (fnPostMessage ? 'ok' : 'NULL'));

var hmTarget = null;
Process.getModuleByName('Qt6Gui.dll').enumerateExports().forEach(function (e) {
    var n = e.name || '';
    if (n.indexOf('handleMouseEvent@') >= 0
        && n.indexOf('UDefaultDelivery@') >= 0
        && n.indexOf('@@PEBVQPointingDevice@@') >= 0) {
        hmTarget = e.address;
    }
});
var fnHandleMouse = new NativeFunction(hmTarget, 'bool', [
    'pointer','pointer','pointer','pointer','int','int','int','int','int'
]);
wlog('hm=' + hmTarget);

// handleKeyEvent<DefaultDelivery>(QWindow*, ulong ts, Type, int key, QFlags<KbdMod>, QString& text, bool autorep, ushort count)
var hkTarget = null;
Process.getModuleByName('Qt6Gui.dll').enumerateExports().forEach(function (e) {
    var n = e.name || '';
    if (n.indexOf('handleKeyEvent@') >= 0
        && n.indexOf('UDefaultDelivery@') >= 0) {
        hkTarget = e.address;
    }
});
// 签名: bool (QWindow*, ulong, int type, int key, int mods, pointer qstr, int autorep, int count)
var fnHandleKey = new NativeFunction(hkTarget, 'bool', [
    'pointer','uint64','int','int','int','pointer','int','int'
]);
wlog('hk=' + hkTarget);

// 构造 Qt6 QString (UTF-16). 布局: {QTypedArrayData* d; qsizetype size;}
// QArrayData header: {ref(4) + pad(4) + alloc(8) + size(8)} = 24 bytes, 数据紧跟其后
// 简化: 分配 24 + len*2, 写 header + UTF-16 数据, 返回 {data_ptr_24byte_block, size}
function makeQString(str) {
    // 编码 UTF-16 (JS string -> UTF-16LE)
    var codes = [];
    for (var i = 0; i < str.length; i++) {
        codes.push(str.charCodeAt(i) & 0xffff);
    }
    var len = codes.length;
    var buf = Memory.alloc(24 + len * 2 + 2);
    // header: ref=1(不会释放), alloc=len, size=len
    buf.writeU32(1);              // ref @0
    buf.add(4).writeU32(0);       // pad @4
    buf.add(8).writeS64(len);     // alloc @8 (qsizetype 8 bytes)
    buf.add(16).writeS64(len);    // size @16
    var dataStart = buf.add(24);
    for (var i = 0; i < len; i++) {
        dataStart.add(i * 2).writeU16(codes[i]);
    }
    dataStart.add(len * 2).writeU16(0);  // null term
    // QString 结构 = {d=buf, size=len} (16 bytes)
    var qs = Memory.alloc(16);
    qs.writePointer(buf);
    qs.add(8).writeS64(len);
    return qs;
}

// 程序化获取 dev (primaryPointingDevice(空QString))
var emptyQStr = Memory.alloc(8); emptyQStr.writeU64(0);
var curDev = null;
try {
    curDev = fnPrimDev(emptyQStr);
    wlog('primaryPointingDevice = ' + curDev);
} catch (e) { wlog('primDev err: ' + e); }

// 监听窗口显示 (找导出 modal 窗口). hook QWindow::setVisible(bool)
// setVisible(this=QWindow*, bool visible). Win64 thiscall: this=rcx=args[0], visible=dl=args[1]低字节
var setVisibleAddr = null;
Process.getModuleByName('Qt6Gui.dll').enumerateExports().forEach(function (e) {
    var n = e.name || '';
    if (n.indexOf('?setVisible@QWindow@@QEAAX_N@Z') >= 0) setVisibleAddr = e.address;
});
var lastShownWin = null;      // 最近 setVisible(true) 的窗口
var lastShownTime = 0;
var shownWindows = [];        // 所有 show 过的窗口指针 (去重)
var showCount = 0;
if (setVisibleAddr) {
    Interceptor.attach(setVisibleAddr, {
        onEnter: function (args) {
            try {
                var win = args[0];                 // this = QWindow*
                var visible = args[1].toInt32 ? args[1].toInt32() : 0;  // bool
                if (visible !== 0) {
                    lastShownWin = win;
                    lastShownTime = Date.now();
                    showCount++;
                    shownWindows.push(win.toString());
                    wlog('SHOW #' + showCount + ' ' + win);
                }
            } catch (e) { wlog('show hook err: ' + e); }
        }
    });
    wlog('hook setVisible @ ' + setVisibleAddr);
}

var curKeys = 0, curSource = 0;
var isExecuting = false;

// 抓 dev (备用, 真实事件刷新) + 校准 send
Interceptor.attach(hmTarget, {
    onEnter: function (a) {
        if (isExecuting) return;
        try {
            if (!curDev) curDev = a[1];   // 程序化失败时用真实事件兜底
            curKeys = a[7].toInt32();
            curSource = a[8].toInt32();
            if (a[6].toInt32() === 2) {
                var lx = a[2].readDouble(), ly = a[2].add(8).readDouble();
                var gx = a[3].readDouble(), gy = a[3].add(8).readDouble();
                send({ type: 'real', info: { type: 2, lx: lx, ly: ly, gx: gx, gy: gy,
                    win: a[0].toString(), dev: a[1].toString() } });
            }
        } catch (e) {}
    }
});

var localPt = Memory.alloc(16);
var globalPt = Memory.alloc(16);

rpc.exports = {
    click: function (lx, ly) {
        lx = parseFloat(lx); ly = parseFloat(ly);
        if (!curDev) return { ok: false, err: 'no dev' };
        isExecuting = true;
        try {
            var win = fnFocus();
            if (!win || win.isNull()) return { ok: false, err: 'no focus window' };
            // 桌面模式: 固定窗口位置 (Windows 随机放置会导致坐标偏)
            if (fnSetGeom) {
                try { fnSetGeom(win, FIXED_X, FIXED_Y, FIXED_W, FIXED_H); } catch(e){}
            }
            var ox = fnWinX(win), oy = fnWinY(win);
            var gx = ox + lx, gy = oy + ly;
            localPt.writeDouble(lx); localPt.add(8).writeDouble(ly);
            globalPt.writeDouble(gx); globalPt.add(8).writeDouble(gy);
            wlog('CLICK local(' + lx + ',' + ly + ') focusWin=' + win + ' origin(' + ox + ',' + oy + ') global(' + gx + ',' + gy + ')');
            var r1 = fnHandleMouse(win, curDev, localPt, globalPt, 1, 1, 2, curKeys, curSource);
            var r2 = fnHandleMouse(win, curDev, localPt, globalPt, 0, 1, 3, curKeys, curSource);
            return { ok: true, pressRet: r1, releaseRet: r2, win: win.toString(),
                     origin: { x: ox, y: oy }, global: { x: gx, y: gy } };
        } catch (e) {
            wlog('CLICK err: ' + e);
            return { ok: false, err: '' + e };
        } finally {
            isExecuting = false;
        }
    },
    // 用最近显示的窗口 (modal) 点击 — 解决 confirm 在 modal 窗口的问题
    clickmodal: function (lx, ly) {
        lx = parseFloat(lx); ly = parseFloat(ly);
        if (!curDev) return { ok: false, err: 'no dev' };
        isExecuting = true;
        try {
            var win = lastShownWin;
            if (!win || win.isNull()) return { ok: false, err: 'no modal window (setVisible not seen)' };
            var ox = fnWinX(win), oy = fnWinY(win);
            var gx = ox + lx, gy = oy + ly;
            localPt.writeDouble(lx); localPt.add(8).writeDouble(ly);
            globalPt.writeDouble(gx); globalPt.add(8).writeDouble(gy);
            wlog('CLICKMODAL local(' + lx + ',' + ly + ') modalWin=' + win + ' origin(' + ox + ',' + oy + ') global(' + gx + ',' + gy + ')');
            var r1 = fnHandleMouse(win, curDev, localPt, globalPt, 1, 1, 2, curKeys, curSource);
            var r2 = fnHandleMouse(win, curDev, localPt, globalPt, 0, 1, 3, curKeys, curSource);
            return { ok: true, pressRet: r1, releaseRet: r2, win: win.toString(),
                     origin: { x: ox, y: oy }, global: { x: gx, y: gy } };
        } catch (e) {
            wlog('CLICKMODAL err: ' + e);
            return { ok: false, err: '' + e };
        } finally {
            isExecuting = false;
        }
    },
    lastshown: function () {
        return { win: lastShownWin ? lastShownWin.toString() : null, showCount: showCount };
    },
    // hook 内 PostMessage WM_CHAR (桌面模式用, 同进程同桌面有效)
    typewm: function (text) {
        try {
            if (!fnWinId || !fnPostMessage) return { ok: false, err: 'winId/PostMessage not bound' };
            var win = fnFocus();
            if (!win || win.isNull()) return { ok: false, err: 'no focus window' };
            var hwnd = fnWinId(win);   // QWindow::winId() -> HWND (NativePointer)
            var n = 0;
            for (var i = 0; i < text.length; i++) {
                fnPostMessage(hwnd, 0x0102, ptr(text.charCodeAt(i) & 0xffff), ptr(0));  // WM_CHAR
                n++;
            }
            wlog('TYPEWM "' + text + '" (' + n + ') hwnd=' + hwnd);
            return { ok: true, chars: n, hwnd: hwnd.toString() };
        } catch (e) { wlog('TYPEWM err: ' + e); return { ok: false, err: '' + e }; }
    },
    status: function () {
        return { curDev: curDev ? curDev.toString() : null, pid: Process.id };
    },
    // 输入文本: 对每个字符发 KeyPress+KeyRelease
    type: function (text) {
        isExecuting = true;
        try {
            var win = fnFocus();
            if (!win || win.isNull()) return { ok: false, err: 'no focus window' };
            var count = 0;
            for (var i = 0; i < text.length; i++) {
                var ch = text.charAt(i);
                var code = text.charCodeAt(i);
                var qs = makeQString(ch);
                // Qt::Key: 可打印字符用 unicode 码点 (Qt::Key_A=0x41 等)
                var qtKey = code;
                // KeyPress=6, KeyRelease=7
                var r1 = fnHandleKey(win, 0, 6, qtKey, 0, qs, 0, 1);
                var r2 = fnHandleKey(win, 0, 7, qtKey, 0, qs, 0, 1);
                count++;
            }
            wlog('TYPE "' + text + '" (' + count + ' chars) win=' + win);
            return { ok: true, chars: count, win: win.toString() };
        } catch (e) {
            wlog('TYPE err: ' + e);
            return { ok: false, err: '' + e };
        } finally {
            isExecuting = false;
        }
    }
};

wlog('READY pid=' + Process.id);
send({ type: 'ready', pid: Process.id });
