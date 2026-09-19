// hook_export_trace.js — P1 侦察: 被动 hook videoeditor.dll!ExportClient::exportStart,
// dump ExportStartReqStruct 完整内存块, 供 Linux 侧按 jianying-headless 参考地图
// (输出路径 string / config 块 width-height-fps-bitrate) 重建 5.9 字段布局.
//
// 用法: 由 re_probe_p1.py 装载 (第二个独立 frida 会话, 不与 hook_focus.js 冲突).
// 产出消息流: {t:'ready'} → {t:'chunk',id,off,data:ArrayBuffer}* → {t:'end',id,bytes,meta}
//
// 关键 ABI 事实 (x64 MSVC):
//   ?exportStart@ExportClient@@SAXV?$shared_ptr@UExportStartReqStruct@lyra@@...@3@_NJ@Z
//   参数 = (shared_ptr<ExportStartReqStruct> 按值, const callback&, bool, long sid)
//   → RCX 指向 shared_ptr 16字节块 {ReqStruct*, ctrl}, RDX 回调, R8 bool, R9 sid.
//     args[0].readPointer() 即 ReqStruct*. 尾部 _N_J = bool, long 与签名吻合.

'use strict';

var HOOKED = false;
var DUMP_CAP = 4 * 1024 * 1024;   // 单次 dump 上限 4MB (上次实测 288KB 全块)
var PAGE = 4096;

function modOff(addr) {
    var m = Process.findModuleByAddr(addr);
    return m ? (m.name + '+0x' + addr.sub(m.base).toString(16)) : addr.toString();
}

function backtrace(ctx) {
    var out = [];
    try {
        Thread.backtrace(ctx, Backtracer.FUZZY).slice(0, 14).forEach(function (a) {
            out.push(modOff(a));
        });
    } catch (e) { out.push('bt-err:' + e); }
    return out;
}

// 逐页安全读: 碰到未映射页就停 (堆块尾部多半邻接未分配区).
// 每读一页立刻 send, 不在 JS 里攒大 buffer.
function streamDump(id, base) {
    var total = 0;
    for (var off = 0; off < DUMP_CAP; off += PAGE) {
        var buf;
        try {
            buf = base.add(off).readByteArray(PAGE);
        } catch (e) { break; }
        if (!buf || buf.byteLength === 0) break;
        send({ t: 'chunk', id: id, off: off }, buf);
        total += buf.byteLength;
        // 提前停: 请求块尾部出现 4KB 全零一般已过界, 继续扫只是浪费 —
        // 但不因此断言结束, 让未映射页做最终截断 (288KB 块中部可能有零段).
    }
    return total;
}

function installHooks() {
    var mod = Process.findModuleByName('videoeditor.dll');
    if (!mod) return false;

    var wanted = [
        // [导出名子串, 说明] — 按导出表精确匹配 mangled 名, 版本无关
        ['?exportStart@ExportClient@@', 'exportStart'],
        ['?exportCancel@ExportClient@@', 'exportCancel'],
        ['?exportCompositionToFileSync2@ExportClient@@', 'exportCompFileSync2'],
        ['?exportCompositionToFileSync@ExportClient@@', 'exportCompFileSync'],
        ['?closeSession@Server@lyra@@QEAAXJ@Z', 'closeSession'],
    ];
    var found = {};
    mod.enumerateExports().forEach(function (e) {
        if (e.type !== 'function') return;
        for (var i = 0; i < wanted.length; i++) {
            if (e.name.indexOf(wanted[i][0]) === 0 && !found[wanted[i][1]]) {
                found[wanted[i][1]] = e.address;
            }
        }
    });

    send({ t: 'symbols', found: Object.keys(found),
           missing: wanted.map(function (w) { return w[1]; })
                          .filter(function (n) { return !found[n]; }) });

    var seq = 0;

    // 导出请求族: 统一 dump. exportStart 是主目标; cancel/composition 兜底观察
    // 5.9 是否走旧 API (真导出触发时若两路都响, 对比即可分辨实际链路).
    ['exportStart', 'exportCancel', 'exportCompFileSync2', 'exportCompFileSync'].forEach(function (name) {
        var addr = found[name];
        if (!addr) return;
        Interceptor.attach(addr, {
            onEnter: function (args) {
                var id = ++seq;
                var meta = {
                    t: 'end', id: id, api: name, tid: Process.getCurrentThreadId(),
                    ret: modOff(this.returnAddress), stack: backtrace(this.context),
                    reqPtr: 'null'
                };
                try {
                    var sp = args[0];                 // → shared_ptr 16字节块
                    var req = sp.readPointer();       // ReqStruct*
                    meta.reqPtr = req.toString();
                    if (!req.isNull()) {
                        var bytes = streamDump(id, req);
                        meta.bytes = bytes;
                    } else {
                        meta.bytes = 0;
                    }
                } catch (e) {
                    meta.err = 'req-read: ' + e;
                    meta.bytes = 0;
                }
                try { meta.sid = args[3].toInt32(); } catch (e) { /* 无尾参的 API */ }
                send(meta);
            }
        });
    });

    // closeSession(long sid) — 关会话时记录 sid, 与 exportStart 的 sid 对表,
    // 确认导出用的会话在导出前没有被销毁重建.
    if (found.closeSession) {
        Interceptor.attach(found.closeSession, {
            onEnter: function (args) {
                send({ t: 'event', api: 'closeSession', sid: args[0].toInt32(),
                       stack: backtrace(this.context).slice(0, 6) });
            }
        });
    }

    HOOKED = true;
    send({ t: 'ready' });
    return true;
}

// videoeditor.dll 可能延迟加载 (首页阶段未必已 load) — 轮询到出现再挂钩.
if (!installHooks()) {
    var timer = setInterval(function () {
        if (installHooks()) { clearInterval(timer); }
    }, 500);
}
