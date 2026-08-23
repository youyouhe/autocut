// hook_focus.js — 终极版: focusWindow() + local 坐标 + 程序化 dev
// 所有函数地址动态查找 (按导出名), PID/基址变化都能用
var DIR = 'C:/Users/Administrator/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft/ym/';
// 该 ym 目录通常不存在, new File 会抛异常 -> logfile=null -> wlog 静默 no-op (可接受,
// 实时诊断走 send()). 不要在此 mkdir (Frida 的 File 无建目录能力).
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
// 所有当前存在的顶层窗口 (静态方法, 随时查询当前状态, 不依赖"最近 setVisible(true)"这种历史
// 事件追踪 —— lastShownWin 实测会被无关的 hover/tooltip 小窗口(如 MiniMapView 预览)覆盖,
// 导致真正要找的弹窗(导出确认框等)找不到. 找按钮时改成扫描全部顶层窗口, 更可靠.
var fnTopLevelWindows = new NativeFunction(findExport('?topLevelWindows@QGuiApplication@@SA?AV?$QList@PEAVQWindow@@@@XZ'), 'pointer', ['pointer']);
function qiGetTopLevelWindows() {
    var buf = Memory.alloc(24);
    fnTopLevelWindows(buf);
    var dp = buf.add(8).readPointer(); var sz = buf.add(16).readS64();
    var arr = [];
    for (var i = 0; i < sz; i++) { var p = dp.add(i * 8).readPointer(); if (!p.isNull()) arr.push(p); }
    return arr;
}
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

// === QQuickItem 树内省 (找弹窗真实按钮用, 不再依赖 calib.json 里的固定像素坐标) ===
// 背景: calib.json 的 confirm/close_done 坐标是在某次特定 DPI/窗口状态下人工点击校准出来的,
// 一旦窗口尺寸/DPI/弹窗布局变化就完全失效(实测在隔离桌面下这些坐标已经越界/失效).
// 用 Qt 内部 API 直接读取弹窗的 QQuickItem 树, 按钮文字(如"导出"/"取消") 找到真实几何位置,
// 每次点击都重新定位, 天然免疫坐标漂移.
function findExportIn(modName, substr) {
    var found = null;
    try {
        Process.getModuleByName(modName).enumerateExports().forEach(function (e) {
            if (!found && (e.name || '').indexOf(substr) >= 0) found = e.address;
        });
    } catch (e) {}
    return found;
}
var fnContentItem = new NativeFunction(findExportIn('Qt6Quick.dll', '?contentItem@QQuickWindow@@QEBAPEAVQQuickItem@@XZ'), 'pointer', ['pointer']);
// 隐藏返回指针参数顺序实测是 (this, retbuf), 不是常见文档写的 (retbuf, this) —
// 用两种顺序实测过, this 在前才能读到正确数据, 反过来直接访问违例崩溃.
var fnChildItems  = new NativeFunction(findExportIn('Qt6Quick.dll', '?childItems@QQuickItem@@QEBA?AV?$QList@PEAVQQuickItem@@@@XZ'), 'pointer', ['pointer','pointer']);
var fnItemX = new NativeFunction(findExportIn('Qt6Quick.dll', '?x@QQuickItem@@QEBANXZ'), 'double', ['pointer']);
var fnItemY = new NativeFunction(findExportIn('Qt6Quick.dll', '?y@QQuickItem@@QEBANXZ'), 'double', ['pointer']);
var fnItemW = new NativeFunction(findExportIn('Qt6Quick.dll', '?width@QQuickItem@@QEBANXZ'), 'double', ['pointer']);
var fnItemH = new NativeFunction(findExportIn('Qt6Quick.dll', '?height@QQuickItem@@QEBANXZ'), 'double', ['pointer']);
var fnItemVis = new NativeFunction(findExportIn('Qt6Quick.dll', '?isVisible@QQuickItem@@QEBA_NXZ'), 'bool', ['pointer']);
// enabled / opacity: 排查弹窗按钮是否被禁用(如"草稿含未渲染完成的数字人"待处理态把 confirm 灰掉).
// 实测 Qt6Quick.dll 导出: ?isEnabled@QQuickItem@@QEBA_NXZ, ?opacity@QQuickItem@@QEBANXZ.
var fnItemEnabled = new NativeFunction(findExportIn('Qt6Quick.dll', '?isEnabled@QQuickItem@@QEBA_NXZ'), 'bool', ['pointer']);
var fnItemOpacity = new NativeFunction(findExportIn('Qt6Quick.dll', '?opacity@QQuickItem@@QEBANXZ'), 'double', ['pointer']);
var fnTextText = new NativeFunction(findExportIn('Qt6Quick.dll', '?text@QQuickText@@QEBA?AVQString@@XZ'), 'pointer', ['pointer','pointer']);
var fnTextInputText = new NativeFunction(findExportIn('Qt6Quick.dll', '?text@QQuickTextInput@@QEBA?AVQString@@XZ'), 'pointer', ['pointer','pointer']);
var fnClassName = new NativeFunction(findExportIn('Qt6Core.dll', '?className@QMetaObject@@QEBAPEBDXZ'), 'pointer', ['pointer']);

function qiGetClassName(itemPtr) {
    try {
        var vtable = itemPtr.readPointer();
        var metaObjFn = vtable.readPointer(); // vtable slot 0 = QObject::metaObject() (虚函数, Q_OBJECT 宏里最先声明)
        var callMeta = new NativeFunction(metaObjFn, 'pointer', ['pointer']);
        var mo = callMeta(itemPtr);
        if (!mo || mo.isNull()) return '?';
        var namePtr = fnClassName(mo);
        if (!namePtr || namePtr.isNull()) return '?';
        return namePtr.readCString();
    } catch (e) { return null; }
}
function qiReadQString(retbuf) {
    try {
        var dataPtr = retbuf.add(8).readPointer();
        var size = retbuf.add(16).readS64();
        if (size <= 0 || dataPtr.isNull()) return '';
        return dataPtr.readUtf16String(size);
    } catch (e) { return null; }
}
function qiGetText(itemPtr, cls) {
    try {
        var buf = Memory.alloc(24);
        if (cls === 'QQuickText') { fnTextText(itemPtr, buf); return qiReadQString(buf); }
        if (cls === 'QQuickTextInput' || cls === 'QQuickTextEdit') { fnTextInputText(itemPtr, buf); return qiReadQString(buf); }
    } catch (e) {}
    return null;
}
function qiGetChildItems(itemPtr) {
    var buf = Memory.alloc(24);
    fnChildItems(itemPtr, buf);
    var dataPtr = buf.add(8).readPointer();
    var size = buf.add(16).readS64();
    var arr = [];
    for (var i = 0; i < size; i++) {
        var p = dataPtr.add(i * 8).readPointer();
        if (!p.isNull()) arr.push(p);
    }
    return arr;
}
// 在 win 的 QQuickItem 树里找文字等于 wantText 的按钮容器, 返回其相对窗口的绝对逻辑坐标
// (ax/ay 是逐层累加 parent x/y 算出来的, 就是 fnHandleMouse 的 localPt 坐标系, 不用
// QQuickItem::mapToGlobal —— 那个函数在隔离桌面下对某些 item 直接访问违例崩溃).
function qiFindButtonByText(win, wantTexts, maxDepth, maxNodes) {
    maxDepth = maxDepth || 25; maxNodes = maxNodes || 4000;
    var root;
    try { root = fnContentItem(win); } catch (e) { return null; }
    if (!root || root.isNull()) return null;
    var found = null;
    var count = 0;
    function walk(item, depth, ax, ay, ancestorBtn) {
        if (found || count >= maxNodes || depth > maxDepth) return;
        if (!item || item.isNull()) return;
        count++;
        var cls = null, x = 0, y = 0, w = 0, h = 0, vis = false, text = null;
        try {
            cls = qiGetClassName(item);
            x = fnItemX(item); y = fnItemY(item); w = fnItemW(item); h = fnItemH(item);
            vis = fnItemVis(item);
            text = qiGetText(item, cls);
        } catch (e) { return; }
        var nax = ax + x, nay = ay + y;
        // 按钮容器: 有一定尺寸的节点作为"最近的可点击祖先"往下传. 上限过滤掉标题栏/大面板这类
        // 跨越整个弹窗宽度的容器(实测标题栏文字跟按钮文字经常同名, 如都叫"导出", 标题栏在树里
        // 先遍历到, 不限制尺寸上限会错误地把标题栏(如 640x36)当成"按钮"提前命中返回).
        var isClickable = (cls === 'QQuickMouseArea' || cls.indexOf('LVButton') === 0);
        var thisBtn = ancestorBtn;
        if (w > 4 && h > 4 && w <= 220 && h <= 80) {
            // 优先保留真正的可点击祖先(MouseArea/LVButton); 没有这类祖先时才用普通容器兜底.
            // 旧版每次都用更深的节点覆盖, 最终返回最深的 QQuickRow(22x20) 而非真正的
            // LVButton/MouseArea(72x20), 导致点击中心仍落在 MouseArea 内但取到的 cls 是行容器.
            if (isClickable || !ancestorBtn) {
                thisBtn = { ax: nax, ay: nay, w: w, h: h, cls: cls, itemPtr: item };
            }
        }
        if (text && wantTexts.indexOf(text) >= 0 && thisBtn) {
            found = thisBtn;
            return;
        }
        if (!vis) return;
        var kids;
        try { kids = qiGetChildItems(item); } catch (e) { return; }
        for (var i = 0; i < kids.length && !found && count < maxNodes; i++) {
            walk(kids[i], depth + 1, nax, nay, thisBtn);
        }
    }
    walk(root, 0, 0, 0, null);
    return found;
}

var hmTarget = null;
Process.getModuleByName('Qt6Gui.dll').enumerateExports().forEach(function (e) {
    var n = e.name || '';
    // 注意: 必须匹配带 K(ulong ts 时间戳参数) 的 10 参重载 '@@KPEBVQPointingDevice@@'.
    // 不带 K 的 9 参重载里 a[7]=mods(不是 type), 按 a[7]==2 判 Press 永远不成立,
    // 表现就是"真实点击检测不到"(校准卡死在等待点击). 实测 5.9 两个重载都会被真实输入触发,
    // 但只有 ts 版的参数布局和下面 fnHandleMouse 的 10 参签名一致.
    if (n.indexOf('handleMouseEvent@') >= 0
        && n.indexOf('UDefaultDelivery@') >= 0
        && n.indexOf('@@KPEBVQPointingDevice@@') >= 0) {
        hmTarget = e.address;
    }
});
// 真实符号: ??$handleMouseEvent@...@QWindowSystemInterface@@SA_NPEAVQWindow@@KPEBVQPointingDevice@@AEBVQPointF@@2V?$QFlags...@@W4...@@...
//   K = unsigned long (8字节, ulong timestamp) => 第2个参数. 共 10 个参数:
//   (win, ulong ts, dev, localPt, globalPt, button, buttons, type, mods, source)
// 旧版漏掉 ts 写成 9 参, 导致 curDev 落进 ts 槽位 — 真实点击全部发到错误的参数布局.
var fnHandleMouse = new NativeFunction(hmTarget, 'bool', [
    'pointer','uint64','pointer','pointer','pointer','int','int','int','int','int'
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

// 抓 dev (备用, 真实事件刷新) + 校准 send.
// 10参签名: a[0]=win a[1]=ts a[2]=dev a[3]=localPt a[4]=globalPt
//           a[5]=button a[6]=buttons a[7]=type a[8]=mods a[9]=source
Interceptor.attach(hmTarget, {
    onEnter: function (a) {
        if (isExecuting) return;
        try {
            if (!curDev) curDev = a[2];   // 程序化失败时用真实事件兜底 (a[2]=dev, 不是 a[1])
            curKeys = a[8].toInt32();     // mods
            curSource = a[9].toInt32();   // source
            if (a[7].toInt32() === 2) {   // type==2 (Press)
                var lx = a[3].readDouble(), ly = a[3].add(8).readDouble();
                var gx = a[4].readDouble(), gy = a[4].add(8).readDouble();
                send({ type: 'real', info: { type: 2, lx: lx, ly: ly, gx: gx, gy: gy,
                    win: a[0].toString(), dev: a[2].toString() } });
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
            // 先发一个 Move(hover) 再 Press+Release: 新弹出的窗口/控件没收到过任何鼠标事件时,
            // Qt(尤其QtQuick按钮的hover/ripple状态机)有时直接收 Press 命中不到子控件, 必须先有一次
            // Move 建立"当前悬停项", Press 才能正确路由到该控件的点击处理.
            var r0 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 0, 0, 5, curKeys, curSource);
            var r1 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 1, 1, 2, curKeys, curSource);
            var r2 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 0, 1, 3, curKeys, curSource);
            return { ok: true, moveRet: r0, pressRet: r1, releaseRet: r2, win: win.toString(),
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
            // 同 click(): 弹窗是刚显示的新窗口, 没有过任何鼠标事件, 先补一个 Move 建立悬停态再点
            var r0 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 0, 0, 5, curKeys, curSource);
            var r1 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 1, 1, 2, curKeys, curSource);
            var r2 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 0, 1, 3, curKeys, curSource);
            return { ok: true, moveRet: r0, pressRet: r1, releaseRet: r2, win: win.toString(),
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
    // 用一个明确的 QWindow 指针(如之前某次 click()/clickmodal() 返回的 win) 点击,
    // 不依赖 fnFocus()(桌面模式下弹窗弹出后经常整桌面都没有 focusWindow) 也不依赖
    // lastShownWin(可能是无关的 Tool/tooltip 弹窗, 不是真正承载业务逻辑的窗口).
    clickwin: function (winStr, lx, ly) {
        lx = parseFloat(lx); ly = parseFloat(ly);
        if (!curDev) return { ok: false, err: 'no dev' };
        isExecuting = true;
        try {
            var win = ptr(winStr);
            if (!win || win.isNull()) return { ok: false, err: 'bad win ptr' };
            var ox = fnWinX(win), oy = fnWinY(win);
            var gx = ox + lx, gy = oy + ly;
            localPt.writeDouble(lx); localPt.add(8).writeDouble(ly);
            globalPt.writeDouble(gx); globalPt.add(8).writeDouble(gy);
            wlog('CLICKWIN local(' + lx + ',' + ly + ') win=' + win + ' origin(' + ox + ',' + oy + ') global(' + gx + ',' + gy + ')');
            var r0 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 0, 0, 5, curKeys, curSource);
            var r1 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 1, 1, 2, curKeys, curSource);
            var r2 = fnHandleMouse(win, 0, curDev, localPt, globalPt, 0, 1, 3, curKeys, curSource);
            return { ok: true, moveRet: r0, pressRet: r1, releaseRet: r2, win: win.toString(),
                     origin: { x: ox, y: oy }, global: { x: gx, y: gy } };
        } catch (e) {
            wlog('CLICKWIN err: ' + e);
            return { ok: false, err: '' + e };
        } finally {
            isExecuting = false;
        }
    },
    // 只读: 给定窗口指针查它当前原点 (配合 clickwin/真实点击前先算 global 坐标用)
    originof: function (winStr) {
        try {
            var win = ptr(winStr);
            if (!win || win.isNull()) return { ok: false, err: 'bad win ptr' };
            return { ok: true, x: fnWinX(win), y: fnWinY(win), win: win.toString() };
        } catch (e) { return { ok: false, err: '' + e }; }
    },
    // 只读窗口原点, 不点击 — 配合 Python 侧真实 SendInput 点击用 (frida 注入点击对某些
    // 弹窗按钮"看起来成功但不触发业务逻辑", 改用真实 OS 级点击需要先知道窗口原点)
    modalorigin: function () {
        var win = lastShownWin;
        if (!win || win.isNull()) return { ok: false, err: 'no modal window' };
        return { ok: true, x: fnWinX(win), y: fnWinY(win), win: win.toString() };
    },
    focusorigin: function () {
        var win = fnFocus();
        if (!win || win.isNull()) return { ok: false, err: 'no focus window' };
        return { ok: true, x: fnWinX(win), y: fnWinY(win), win: win.toString() };
    },
    // 弹窗(lastShownWin)对应的真实 HWND. 桌面模式下新弹出的弹窗从没被
    // SetForegroundWindow/SetActiveWindow 激活过, focusWindow() 会一直是 null,
    // 导致真实点击只把它"点活"而不触发按钮逻辑(Windows 单击非激活窗口的经典问题).
    // Python 侧拿到这个 HWND 后先 SetForegroundWindow 再点, 才能命中业务逻辑.
    modalhwnd: function () {
        var win = lastShownWin;
        if (!win || win.isNull()) return { ok: false, err: 'no modal window' };
        if (!fnWinId) return { ok: false, err: 'winId not bound' };
        try {
            var hwnd = fnWinId(win);
            return { ok: true, hwnd: hwnd.toString(), win: win.toString() };
        } catch (e) { return { ok: false, err: '' + e }; }
    },
    // 在弹窗(导出确认框/完成提示框)里按文字找按钮, 返回窗口内绝对逻辑坐标 + 该窗口 HWND.
    // wantTextsJson 是候选文字的 JSON 数组(如 '["导出"]'), 命中第一个即返回.
    // 这是取代 calib.json 固定像素坐标的方案: 每次点击都重新按文字定位, 不怕坐标漂移/DPI变化.
    //
    // 关键: 只在"刚弹出的新窗口"里找, 不能扫所有顶层窗口 —— 编辑器窗口(1180x500)本身也是顶层
    // 窗口且尺寸<预设, 它工具栏里也有"导出"按钮, 全量扫描会把编辑器工具栏的导出按钮误当成弹窗
    // 确认按钮, 反复点工具栏导出(打开新弹窗)而不是点弹窗里的确认, 渲染永远启动不了.
    // 所以这里严格限定: 只搜 lastShownWin(最近 setVisible(true) 的窗口, 即刚弹出的导出/完成框),
    // 再兜底搜最近 N 个 show 过的小窗口, 绝不碰大尺寸的编辑器/首页窗口.
    findmodalbutton: function (wantTextsJson) {
        var wantTexts;
        try { wantTexts = JSON.parse(wantTextsJson); } catch (e) { return { ok: false, err: 'bad wantTexts' }; }
        // 候选窗口: 优先 lastShownWin, 再兜底最近 show 过的窗口. 排除大窗口(编辑器/首页).
        var order = [];
        var seen = {};
        function pushWin(w) {
            if (!w || w.isNull()) return;
            var k = w.toString();
            if (seen[k]) return;
            seen[k] = true;
            // 排除编辑器/首页这类大窗口: 它们工具栏也有同名按钮, 误点会重开弹窗而非确认
            try {
                var root = fnContentItem(w);
                if (root && !root.isNull()) {
                    var rw = fnItemW(root), rh = fnItemH(root);
                    if (rw >= FIXED_W - 4 && rh >= FIXED_H - 4) return;  // 预设尺寸 = 首页, 跳过
                    if (rw >= 1000 && rh >= 400) return;  // 编辑器 1180x500 这类, 跳过
                }
            } catch (e) {}
            order.push(w);
        }
        pushWin(lastShownWin);
        // 兜底: 当前所有顶层窗口里的小窗口(弹窗/tooltip 等), 按出现顺序追加
        var wins;
        try { wins = qiGetTopLevelWindows(); } catch (e) { wins = []; }
        for (var i = 0; i < wins.length; i++) pushWin(wins[i]);

        for (var i = 0; i < order.length; i++) {
            var win = order[i];
            var btn;
            try { btn = qiFindButtonByText(win, wantTexts); } catch (e) { continue; }
            if (!btn) continue;
            var hwnd = null;
            try { if (fnWinId) hwnd = fnWinId(win).toString(); } catch (e) {}
            // 顺带读按钮的 enabled/opacity —— 排查 confirm 是否被禁用(如"草稿含未渲染完成的数字人"
            // 待处理态把按钮灰掉, PostMessage 点了也只走 hover 不触发 onClicked).
            var en = true, op = 1.0;
            try {
                if (btn.itemPtr) { en = fnItemEnabled(btn.itemPtr); op = fnItemOpacity(btn.itemPtr); }
            } catch (e) {}
            return { ok: true, win: win.toString(), hwnd: hwnd, ax: btn.ax, ay: btn.ay, w: btn.w, h: btn.h,
                     cls: btn.cls, enabled: en, opacity: Math.round(op * 1000) / 1000 };
        }
        return { ok: false, err: 'button not found in last-shown/small windows (scanned ' + order.length + ')' };
    },
    // 诊断: 给定弹窗 win + 候选文字, 返回命中的按钮节点及其 enabled/vis/opacity 状态.
    // 用于确认 "confirm 按钮是否被禁用" (PostMessage 点禁用按钮不会触发渲染).
    // 不点击, 只读. wantTextsJson: JSON 数组如 '["导出"]'.
    buttonstate: function (wantTextsJson) {
        var wantTexts;
        try { wantTexts = JSON.parse(wantTextsJson); } catch (e) { return { ok: false, err: 'bad wantTexts' }; }
        // 复用 findmodalbutton 的候选窗口过滤逻辑
        var order = [];
        var seen = {};
        function pushWin(w) {
            if (!w || w.isNull()) return;
            var k = w.toString();
            if (seen[k]) return;
            seen[k] = true;
            try {
                var root = fnContentItem(w);
                if (root && !root.isNull()) {
                    var rw = fnItemW(root), rh = fnItemH(root);
                    if (rw >= FIXED_W - 4 && rh >= FIXED_H - 4) return;
                    if (rw >= 1000 && rh >= 400) return;
                }
            } catch (e) {}
            order.push(w);
        }
        pushWin(lastShownWin);
        var wins;
        try { wins = qiGetTopLevelWindows(); } catch (e) { wins = []; }
        for (var i = 0; i < wins.length; i++) pushWin(wins[i]);
        var results = [];
        for (var i = 0; i < order.length; i++) {
            var win = order[i];
            var root;
            try { root = fnContentItem(win); } catch (e) { continue; }
            if (!root || root.isNull()) continue;
            // walk: 找所有命中的文字节点 + 其最近可点击祖先 + 该祖先的 enabled/opacity
            var count = 0;
            function walk(item, depth, ax, ay, ancestorClick) {
                if (count >= 4000 || depth > 30) return;
                if (!item || item.isNull()) return;
                count++;
                var cls = null, x = 0, y = 0, w = 0, h = 0, vis = false, text = null;
                try {
                    cls = qiGetClassName(item);
                    x = fnItemX(item); y = fnItemY(item); w = fnItemW(item); h = fnItemH(item);
                    vis = fnItemVis(item); text = qiGetText(item, cls);
                } catch (e) { return; }
                var nax = ax + x, nay = ay + y;
                var isClickable = (cls === 'QQuickMouseArea' || (cls && cls.indexOf('LVButton') === 0));
                var ac = ancestorClick;
                if (isClickable && w > 4 && h > 4 && w <= 220 && h <= 80) {
                    ac = { ax: nax, ay: nay, w: w, h: h, cls: cls, ptr: item.toString() };
                }
                if (text && wantTexts.indexOf(text) >= 0) {
                    var en = true, op = 1.0;
                    if (ac) {
                        try { en = fnItemEnabled(ptr(ac.ptr)); op = fnItemOpacity(ptr(ac.ptr)); } catch (e) {}
                    }
                    results.push({ text: text, nodeCls: cls, vis: vis,
                                   ancestorClick: ac ? { cls: ac.cls, ax: ac.ax, ay: ac.ay, w: ac.w, h: ac.h,
                                                         enabled: en, opacity: Math.round(op * 1000) / 1000 } : null });
                }
                if (!vis) return;
                var kids;
                try { kids = qiGetChildItems(item); } catch (e) { return; }
                for (var j = 0; j < kids.length && count < 4000; j++) {
                    walk(kids[j], depth + 1, nax, nay, ac);
                }
            }
            try { walk(root, 0, 0, 0, null); } catch (e) {}
        }
        return { ok: true, results: results, scanned: order.length };
    },
    // 在主编辑器/首页窗口里按文字找工具栏按钮(如"导出").
    // 实测 frida 注入的 click()(handleMouseEvent 直调)对"打开新弹窗"这类操作不太可靠(能返回成功但
    // 弹窗没真正出现, 概率性), 改成这里定位坐标 + Python 侧 PostMessage 直接点 HWND
    // (见 render_driver._post_click_hwnd) 更稳定, 跟弹窗按钮同一套机制.
    //
    // 注意: 不能按 FIXED_W/FIXED_H 过滤"主窗口"——剪映首页窗口是 1280x720(预设尺寸), 但
    // 编辑器窗口是另一个独立顶层窗口, 尺寸是 1180x500(不受 resize_jianying 控制), 工具栏"导出"
    // 按钮就在编辑器窗口里. 按 FIXED 尺寸过滤会把编辑器窗口整个排除掉, 永远找不到按钮.
    // 正确做法: 扫描所有顶层窗口, 在每个窗口的 QQuickItem 树里找文字, 优先返回最大可见窗口
    // (编辑器>首页>弹窗) 里的命中 —— 工具栏按钮只可能出现在大窗口里, 小弹窗/tooltip 不含"导出".
    findmainbutton: function (wantTextsJson) {
        var wantTexts;
        try { wantTexts = JSON.parse(wantTextsJson); } catch (e) { return { ok: false, err: 'bad wantTexts' }; }
        var wins;
        try { wins = qiGetTopLevelWindows(); } catch (e) { return { ok: false, err: 'topLevelWindows: ' + e }; }
        // 收集每个窗口的尺寸 + 是否命中按钮, 按窗口面积降序, 优先在最大窗口里找
        var cands = [];
        for (var i = 0; i < wins.length; i++) {
            var win = wins[i];
            var rootW = -1, rootH = -1;
            try {
                var root = fnContentItem(win);
                if (root && !root.isNull()) { rootW = fnItemW(root); rootH = fnItemH(root); }
            } catch (e) { continue; }
            if (rootW <= 0 || rootH <= 0) continue;  // 跳过 0x0 隐藏/未布局窗口
            cands.push({ win: win, w: rootW, h: rootH, area: rootW * rootH });
        }
        cands.sort(function (a, b) { return b.area - a.area; });
        for (var i = 0; i < cands.length; i++) {
            var win = cands[i].win;
            var btn;
            try { btn = qiFindButtonByText(win, wantTexts); } catch (e) { continue; }
            if (!btn) continue;
            var hwnd = null;
            try { if (fnWinId) hwnd = fnWinId(win).toString(); } catch (e) {}
            return { ok: true, win: win.toString(), hwnd: hwnd, ax: btn.ax, ay: btn.ay, w: btn.w, h: btn.h, cls: btn.cls,
                     rootW: cands[i].w, rootH: cands[i].h };
        }
        return { ok: false, err: 'button not found in any top-level window (scanned ' + cands.length + ')' };
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
    // === 诊断: 转储窗口 QQuickItem 树 ===
    // 用于确认"导出确认"按钮的真实类名/文字/几何, 而不是 qiFindButtonByText 可能误取的行容器.
    // winStr: QWindow* 指针字符串. 返回节点列表 [{cls,x,y,w,h,vis,text,depth,clickable}].
    // 只返回有文字 或 可点击(MouseArea/LVButton) 或 有尺寸(>4x4) 的节点, 控制输出量.
    // maxNodes 兜底防止超大树(如编辑器主窗口)卡住.
    dumptree: function (winStr, maxDepth, maxNodes) {
        maxDepth = maxDepth || 30; maxNodes = maxNodes || 2000;
        var win;
        try { win = ptr(winStr); } catch (e) { return { ok: false, err: 'bad win ptr' }; }
        if (!win || win.isNull()) return { ok: false, err: 'bad win ptr' };
        var root;
        try { root = fnContentItem(win); } catch (e) { return { ok: false, err: 'contentItem: ' + e }; }
        if (!root || root.isNull()) return { ok: false, err: 'no contentItem' };
        var nodes = [];
        var count = 0;
        function walk(item, depth, ax, ay) {
            if (count >= maxNodes || depth > maxDepth) return;
            if (!item || item.isNull()) return;
            count++;
            var cls = null, x = 0, y = 0, w = 0, h = 0, vis = false, text = null;
            var en = true, op = 1.0;
            try {
                cls = qiGetClassName(item);
                x = fnItemX(item); y = fnItemY(item); w = fnItemW(item); h = fnItemH(item);
                vis = fnItemVis(item);
                text = qiGetText(item, cls);
                en = fnItemEnabled(item); op = fnItemOpacity(item);
            } catch (e) { return; }
            var nax = ax + x, nay = ay + y;
            var isClickable = (cls === 'QQuickMouseArea' || (cls && cls.indexOf('LVButton') === 0));
            // 收集: 有文字 / 可点击 / 明显尺寸(可能承载子按钮的容器)
            if (text || isClickable || (w > 4 && h > 4)) {
                nodes.push({ cls: cls, x: Math.round(nax), y: Math.round(nay), w: Math.round(w), h: Math.round(h),
                             vis: vis, text: text, depth: depth, clickable: isClickable,
                             enabled: en, opacity: Math.round(op * 1000) / 1000 });
            }
            if (!vis) return;
            var kids;
            try { kids = qiGetChildItems(item); } catch (e) { return; }
            for (var i = 0; i < kids.length && count < maxNodes; i++) {
                walk(kids[i], depth + 1, nax, nay);
            }
        }
        walk(root, 0, 0, 0);
        try { if (fnWinId) { var hwnd = fnWinId(win).toString(); return { ok: true, hwnd: hwnd, win: win.toString(), nodes: nodes, count: count }; } }
        catch (e) {}
        return { ok: true, win: win.toString(), nodes: nodes, count: count };
    },
    // === 诊断: dump 当前 lastShownWin(弹窗) 的完整可点击节点列表, 不接受外部指针 ===
    // 解决 dumptree(winStr) 的 stale-pointer 问题: dumptree 把 Python 传回的 winStr 经
    // ptr() 重新解析, 但该指针字符串往往是前一轮 findmodalbutton 捕获后经 Python 往返的,
    // 期间弹窗可能已 collapse/resize/重建, ptr 解析出的 QWindow* 已失效 → contentItem 返回
    // null → "bad win ptr". 本 rpc 直接用进程内最新的 lastShownWin(由 setVisible hook 实时
    // 更新), 在同一调用内立刻 walk, 杜绝指针往返. 同时也兜底扫所有小顶层窗口.
    // 返回 { ok, modalWin, nodes:[{cls,x,y,w,h,vis,text,depth,clickable,enabled,opacity}], count }
    // 只收 非空文字 或 可点击(MouseArea/LVButton) 的节点.
    dumpmodal: function (maxDepth, maxNodes) {
        maxDepth = maxDepth || 40; maxNodes = maxNodes || 6000;
        var order = [];
        var seen = {};
        function pushWin(w) {
            if (!w || w.isNull()) return;
            var k = w.toString();
            if (seen[k]) return;
            seen[k] = true;
            try {
                var root = fnContentItem(w);
                if (root && !root.isNull()) {
                    var rw = fnItemW(root), rh = fnItemH(root);
                    if (rw >= FIXED_W - 4 && rh >= FIXED_H - 4) return;
                    if (rw >= 1000 && rh >= 400) return;
                }
            } catch (e) {}
            order.push(w);
        }
        pushWin(lastShownWin);
        var wins;
        try { wins = qiGetTopLevelWindows(); } catch (e) { wins = []; }
        for (var i = 0; i < wins.length; i++) pushWin(wins[i]);
        var modalWin = (order.length ? order[0] : null);
        var all = [];
        var total = 0;
        for (var wi = 0; wi < order.length; wi++) {
            var win = order[wi];
            var root;
            try { root = fnContentItem(win); } catch (e) { continue; }
            if (!root || root.isNull()) continue;
            var count = 0;
            function walk(item, depth, ax, ay) {
                if (count >= maxNodes || depth > maxDepth) return;
                if (!item || item.isNull()) return;
                count++; total++;
                var cls = null, x = 0, y = 0, w = 0, h = 0, vis = false, text = null;
                var en = true, op = 1.0;
                try {
                    cls = qiGetClassName(item);
                    x = fnItemX(item); y = fnItemY(item); w = fnItemW(item); h = fnItemH(item);
                    vis = fnItemVis(item);
                    text = qiGetText(item, cls);
                    en = fnItemEnabled(item); op = fnItemOpacity(item);
                } catch (e) { return; }
                var nax = ax + x, nay = ay + y;
                var isClickable = (cls === 'QQuickMouseArea' || (cls && cls.indexOf('LVButton') === 0));
                if (text || isClickable) {
                    all.push({ win: win.toString(), cls: cls, x: Math.round(nax), y: Math.round(nay),
                               w: Math.round(w), h: Math.round(h), vis: vis, text: text, depth: depth,
                               clickable: isClickable, enabled: en, opacity: Math.round(op * 1000) / 1000 });
                }
                if (!vis) return;
                var kids;
                try { kids = qiGetChildItems(item); } catch (e) { return; }
                for (var i2 = 0; i2 < kids.length && count < maxNodes; i2++) {
                    walk(kids[i2], depth + 1, nax, nay);
                }
            }
            try { walk(root, 0, 0, 0); } catch (e) {}
        }
        var hwnd = null;
        try { if (modalWin && fnWinId) hwnd = fnWinId(modalWin).toString(); } catch (e) {}
        return { ok: true, modalWin: modalWin ? modalWin.toString() : null, hwnd: hwnd,
                 nodes: all, scanned: order.length, total: total };
    },
    // === 诊断: 列出弹窗里所有"含指定文字的"节点 + 其祖先链 ===
    // 比 findmodalbutton 更详细: 不只返回第一个命中, 而是返回所有命中文字的节点, 并附上
    // 它们的类名/几何/可点击性, 以及沿祖先链向上找的最近可点击祖先(MouseArea/LVButton).
    // 用于排查"findmodalbutton 取到 QQuickRow(22x20) 而非真正按钮"的问题.
    findmodalbuttons: function (wantTextsJson, maxDepth, maxNodes) {
        var wantTexts;
        try { wantTexts = JSON.parse(wantTextsJson); } catch (e) { return { ok: false, err: 'bad wantTexts' }; }
        maxDepth = maxDepth || 30; maxNodes = maxNodes || 4000;
        // 候选窗口: 同 findmodalbutton 的过滤 (只小窗口)
        var order = [];
        var seen = {};
        function pushWin(w) {
            if (!w || w.isNull()) return;
            var k = w.toString();
            if (seen[k]) return;
            seen[k] = true;
            try {
                var root = fnContentItem(w);
                if (root && !root.isNull()) {
                    var rw = fnItemW(root), rh = fnItemH(root);
                    if (rw >= FIXED_W - 4 && rh >= FIXED_H - 4) return;
                    if (rw >= 1000 && rh >= 400) return;
                }
            } catch (e) {}
            order.push(w);
        }
        pushWin(lastShownWin);
        var wins;
        try { wins = qiGetTopLevelWindows(); } catch (e) { wins = []; }
        for (var i = 0; i < wins.length; i++) pushWin(wins[i]);

        var results = [];
        for (var i = 0; i < order.length; i++) {
            var win = order[i];
            var root;
            try { root = fnContentItem(win); } catch (e) { continue; }
            if (!root || root.isNull()) continue;
            var count = 0;
            // walk 带 ancestorClick (最近可点击祖先) + ancestorChain (沿途容器, 用于回溯)
            function walk(item, depth, ax, ay, ancestorClick) {
                if (count >= maxNodes || depth > maxDepth) return;
                if (!item || item.isNull()) return;
                count++;
                var cls = null, x = 0, y = 0, w = 0, h = 0, vis = false, text = null;
                try {
                    cls = qiGetClassName(item);
                    x = fnItemX(item); y = fnItemY(item); w = fnItemW(item); h = fnItemH(item);
                    vis = fnItemVis(item);
                    text = qiGetText(item, cls);
                } catch (e) { return; }
                var nax = ax + x, nay = ay + y;
                var isClickable = (cls === 'QQuickMouseArea' || (cls && cls.indexOf('LVButton') === 0));
                var thisAnc = ancestorClick;
                if (isClickable && w > 4 && h > 4 && w <= 400 && h <= 120) {
                    var aen = true, aop = 1.0;
                    try { aen = fnItemEnabled(item); aop = fnItemOpacity(item); } catch (e) {}
                    thisAnc = { cls: cls, x: Math.round(nax), y: Math.round(nay), w: Math.round(w), h: Math.round(h),
                                enabled: aen, opacity: Math.round(aop * 1000) / 1000, ptr: item.toString() };
                }
                if (text && wantTexts.indexOf(text) >= 0) {
                    var nen = true, nop = 1.0;
                    try { nen = fnItemEnabled(item); nop = fnItemOpacity(item); } catch (e) {}
                    results.push({ win: win.toString(), text: text,
                        node: { cls: cls, x: Math.round(nax), y: Math.round(nay), w: Math.round(w), h: Math.round(h),
                                vis: vis, clickable: isClickable, enabled: nen, opacity: Math.round(nop * 1000) / 1000 },
                        ancestorClick: thisAnc });
                }
                if (!vis) return;
                var kids;
                try { kids = qiGetChildItems(item); } catch (e) { return; }
                for (var i2 = 0; i2 < kids.length && count < maxNodes; i2++) {
                    walk(kids[i2], depth + 1, nax, nay, thisAnc);
                }
            }
            try { walk(root, 0, 0, 0, null); } catch (e) {}
        }
        return { ok: true, results: results, scanned: order.length };
    },
    // === 首页草稿卡片按"草稿名"定位 ===
    // 背景: 旧 render_driver 用 calib.json 里固定的 caps['card'] 像素坐标点首页卡片打开草稿,
    // 但 inject_draft 把新草稿设为 tm_draft_create=now 让它排到首页第一张卡片(col1,row1, 中心~297,388),
    // 而 calib 的固定坐标(431,501)是早期对别的草稿布局校准的, 落在 col2-row2 = 另一个旧草稿,
    // 导致每次打开的都是旧草稿(其视频素材 path 被清空), 触发 10006 "导出文件缺失". 根因不是
    // 文件真的缺失, 而是点错了卡片. 这里改成按草稿名(注入时已确定唯一)在首页 QQuickItem 树里
    // 找 QQuickText == draftName 的文字节点, 返回其所在卡片的可点击祖先(MouseArea 覆盖整张卡,
    // 实测 90x90 @ y286)的几何 + 首页 HWND, Python 侧再 _post_click_hwnd 点该卡片中心.
    // 只在首页这类大窗口里找(与 findmainbutton 同向, 但 findmainbutton 找的是工具栏按钮, 用
    // qiFindButtonByText 的尺寸上限 w<=220/h<=80 会漏掉 90x90 的卡片 MouseArea), 这里专门放宽
    // 卡片命中区的尺寸上限, 用 draftName 文字精确匹配.
    findcard: function (draftName) {
        if (!draftName) return { ok: false, err: 'no draftName' };
        var wins;
        try { wins = qiGetTopLevelWindows(); } catch (e) { return { ok: false, err: 'topLevelWindows: ' + e }; }
        // 按窗口面积降序, 优先在最大窗口(首页/编辑器)里找. 首页 1280x720 面积最大.
        var cands = [];
        for (var i = 0; i < wins.length; i++) {
            var win = wins[i];
            var rootW = -1, rootH = -1;
            try {
                var root = fnContentItem(win);
                if (root && !root.isNull()) { rootW = fnItemW(root); rootH = fnItemH(root); }
            } catch (e) { continue; }
            if (rootW <= 0 || rootH <= 0) continue;
            cands.push({ win: win, w: rootW, h: rootH, area: rootW * rootH });
        }
        cands.sort(function (a, b) { return b.area - a.area; });
        for (var ci = 0; ci < cands.length; ci++) {
            var win = cands[ci].win;
            var root;
            try { root = fnContentItem(win); } catch (e) { continue; }
            if (!root || root.isNull()) continue;
            // walk: 单次遍历同时收集 (1) 文字 == draftName 的节点 (2) 卡片级可点击祖先 MouseArea.
            // 旧版依赖"可点击祖先在文字节点之前被遍历到"这一假设 —— 但首页卡片里, 90x90 的
            // MouseArea(命中区, @(X,286)) 与草稿名 QQuickText(@(X,380)) 是 *兄弟节点*(同为 d=11),
            // DFS 里文字常先于 MouseArea 被枚举到, 此时 thisAnc 仍为 null → text===draftName 但
            // thisAnc 为空 → 不命中, 永远找不到卡片. 改成收集后按空间邻近匹配:
            // 草稿名文字 @(X,380) 正下方紧贴 90x90 MouseArea @(X,286) (286+90=376 ≈ 380),
            // 取 x 区间覆盖文字 x 且 y 在文字上方最近的 MouseArea 即该卡片的命中区.
            var textHits = [];      // {ax,ay,w,h}
            var cardAreas = [];     // {ax,ay,w,h,cls,itemPtr}
            var lastMissText = '';
            var lastMissCardCount = 0;
            var lastMissCards = '';
            var count = 0;
            function walk(item, depth, ax, ay) {
                if (count >= 6000 || depth > 40) return;
                if (!item || item.isNull()) return;
                count++;
                var cls = null, x = 0, y = 0, w = 0, h = 0, vis = false, text = null;
                try {
                    cls = qiGetClassName(item);
                    x = fnItemX(item); y = fnItemY(item); w = fnItemW(item); h = fnItemH(item);
                    vis = fnItemVis(item);
                    text = qiGetText(item, cls);
                } catch (e) { return; }
                var nax = ax + x, nay = ay + y;
                var isClickable = (cls === 'QQuickMouseArea' || (cls && cls.indexOf('LVButton') === 0));
                // 卡片命中区: MouseArea 且尺寸在卡片量级 (宽高都 >30, 上限放宽到 600 容纳整张卡).
                if (isClickable && w > 30 && h > 30 && w <= 600 && h <= 600) {
                    cardAreas.push({ ax: nax, ay: nay, w: w, h: h, cls: cls, itemPtr: item });
                }
                if (text === draftName) {
                    textHits.push({ ax: nax, ay: nay, w: w, h: h });
                }
                if (!vis) return;
                var kids;
                try { kids = qiGetChildItems(item); } catch (e) { return; }
                for (var k = 0; k < kids.length && count < 6000; k++) {
                    walk(kids[k], depth + 1, nax, nay);
                }
            }
            try { walk(root, 0, 0, 0); } catch (e) {}
            // 匹配: 对每个文字命中点, 找 x 区间覆盖其中心、且在其上方(ay+ah <= ty+几像素容差)
            // 的最近 MouseArea. 首页布局: 文字 @(X,380) 紧贴 MouseArea @(X,286 90x90) 下方.
            var found = null;
            for (var ti = 0; ti < textHits.length && !found; ti++) {
                var th = textHits[ti];
                var tcx = th.ax + th.w / 2;
                var best = null; var bestDist = 1e18;   // 注: 用 1e18 而非 Infinity — Frida QuickJS 下 Infinity 会被求值为 NaN, 使 dist < bestDist 恒为 false, best 永不赋值.
                for (var ai = 0; ai < cardAreas.length; ai++) {
                    var ca = cardAreas[ai];
                    var cax2 = ca.ax + ca.w, cay2 = ca.ay + ca.h;
                    // x: 卡片命中区横向覆盖文字中心 (容差 8px)
                    if (tcx < ca.ax - 8 || tcx > cax2 + 8) continue;
                    // y: 命中区在文字上方或与之重叠 (cay2 <= ty + 容差). 文字在卡片下方.
                    if (cay2 > th.ay + 20) continue;
                    var dist = th.ay - cay2;            // 文字顶 到 命中区底 的距离 (>=0)
                    if (dist < 0) dist = 0;
                    if (dist < bestDist) { bestDist = dist; best = ca; }
                }
                if (best) found = best;
            }
            if (found) {
                var hwnd = null;
                try { if (fnWinId) hwnd = fnWinId(win).toString(); } catch (e) {}
                var en = true, op = 1.0;
                try { en = fnItemEnabled(found.itemPtr); op = fnItemOpacity(found.itemPtr); } catch (e) {}
                return { ok: true, win: win.toString(), hwnd: hwnd,
                         ax: found.ax, ay: found.ay, w: found.w, h: found.h, cls: found.cls,
                         enabled: en, opacity: Math.round(op * 1000) / 1000,
                         rootW: cands[ci].w, rootH: cands[ci].h };
            }
            // 该窗口未命中: 记录本窗口 textHits/cardAreas 的坐标, 便于诊断为何空间匹配失败.
            if (textHits.length > 0) {
                var thDiag = [];
                for (var tdi = 0; tdi < textHits.length && tdi < 8; tdi++) {
                    var t = textHits[tdi];
                    thDiag.push('(@' + t.ax + ',' + t.ay + ' ' + t.w + 'x' + t.h + ' tcx=' + (t.ax + t.w / 2) + ')');
                }
                lastMissText = thDiag.join(' ');
                lastMissCardCount = cardAreas.length;
                // 记录与文字 x 区间相交的候选卡片命中区, 看为何 y 匹配失败.
                var caDiag = [];
                for (var cdi = 0; cdi < cardAreas.length && caDiag.length < 60; cdi++) {
                    var cc = cardAreas[cdi];
                    caDiag.push('(@' + cc.ax + ',' + cc.ay + ' ' + cc.w + 'x' + cc.h + ' b=' + (cc.ay + cc.h) + ')');
                }
                lastMissCards = caDiag.join(' ');
            }
        }
        return { ok: false, err: 'card not found by name (scanned ' + cands.length + ' windows)',
                 textHits: textHits.length, cardAreas: cardAreas.length,
                 lastMissText: lastMissText, lastMissCardCount: lastMissCardCount,
                 lastMissCards: lastMissCards };
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
