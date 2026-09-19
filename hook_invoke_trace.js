// hook_invoke_trace.js — P1c 侦察: hook 疑似 Server::invoke (videoeditor+0x1da160,
// 静态分析自 ExportClient::exportStart/exportCancel 共同尾调链: 0x877d0→0x1da160),
// 打印流经的全部 RPC 消息 service/api 头 (ReqStruct 基类布局: vtable@0 / svc@8 / api@0x28).
// 目标: 点卡片时抓 DraftInit/RestoreDraft 消息过境实锤 → C 方案(直调 invoke 装载)施工图.
'use strict';

var RVA_INVOKE = 0x1da160;

function modOff(addr) {
    var m = null;
    try { m = Process.findModuleByAddress(addr); } catch (e) {}
    return m ? (m.name + '+0x' + addr.sub(m.base).toString(16)) : ('' + addr);
}

function backtrace(ctx, n) {
    var out = [];
    try {
        Thread.backtrace(ctx, Backtracer.FUZZY).slice(0, n || 8).forEach(function (a) {
            out.push(modOff(a));
        });
    } catch (e) { out.push('bt-err:' + e); }
    return out;
}

function readStdString(sp) {
    var size, cap;
    try {
        size = sp.add(16).readU64().toNumber();
        cap = sp.add(24).readU64().toNumber();
    } catch (e) { return null; }
    try {
        if (cap === 15 && size <= 15) return sp.readUtf8String(size);
        if (cap > 15 && size <= cap && size < 8388608)
            return sp.readPointer().readUtf8String(Math.min(size, 200));
    } catch (e) {}
    return null;
}

function install() {
    var mod = Process.findModuleByName('videoeditor.dll');
    if (!mod) return false;
    Interceptor.attach(mod.base.add(RVA_INVOKE), {
        onEnter: function (args) {
            try {
                var req = args[1].readPointer();
                if (req.isNull()) return;
                send({ t: 'invoke',
                       svc: readStdString(req.add(8)),
                       api: readStdString(req.add(0x28)),
                       tid: Process.getCurrentThreadId(),
                       a0: args[0].toString(),
                       sid: args[3].toInt32(),
                       ret: modOff(this.returnAddress),
                       stack: backtrace(this.context, 8) });
            } catch (e) {
                send({ t: 'invoke', svc: '(err:' + e + ')', api: null, tid: 0,
                       a0: '', sid: -999, ret: '', stack: [] });
            }
        }
    });
    send({ t: 'ready', at: modOff(mod.base.add(RVA_INVOKE)) });
    return true;
}

if (!install()) {
    var timer = setInterval(function () {
        if (install()) clearInterval(timer);
    }, 500);
}
