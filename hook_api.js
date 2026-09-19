// hook_api.js — P3 生产 hook: exportStart 输出路径飞行中补丁 (精简版).
// 原理 (P1/P2 实锤, 见 re_libs/P1-运行时侦察报告.md):
//   剪映点导出按钮 → 面板打开 → ExportClient::exportStart 立即触发 (silent_export
//   静默预渲染) → 引擎按请求 +0x50 的路径写盘 → 静默预渲染本身即完整成片.
//   本 hook 在 onEnter 原位改写 +0x50 (仅 temp_folder 请求; 交付类请求的路径是
//   app 预持副本, 补丁会断成片改名 — 红线, 见报告 6 节).
// 由 render_driver 在同一 frida session 里作为第二脚本装载, rpc setpatch 控制.
'use strict';

var PATCH_PATH = null;
rpc.exports.setpatch = function (p) {
    PATCH_PATH = p || null;
    send({ t: 'apilog', msg: 'setpatch=' + (p || '(off)') });
    return true;
};

function install() {
    var mod = Process.findModuleByName('videoeditor.dll');
    if (!mod) return false;
    var addr = null;
    mod.enumerateExports().forEach(function (e) {
        if (!addr && e.type === 'function' &&
            e.name.indexOf('?exportStart@ExportClient@@') === 0) addr = e.address;
    });
    if (!addr) {
        send({ t: 'apilog', msg: 'exportStart 符号未找到 (版本不匹配?), api 模式不可用' });
        return true;  // 停止轮询, 让驱动走传统链
    }
    Interceptor.attach(addr, {
        onEnter: function (args) {
            if (!PATCH_PATH) return;
            try {
                var req = args[0].readPointer();
                if (req.isNull()) return;
                var strObj = req.add(0x50);
                var size = strObj.add(16).readU64().toNumber();
                var cap = strObj.add(24).readU64().toNumber();
                if (cap <= 15) return;                    // SSO 内联串 (非本例形态)
                var buf = strObj.readPointer();
                var opath = buf.readUtf8String(size);
                if (!opath || opath.indexOf('jianying_export_temp_folder') < 0) return;
                if (PATCH_PATH.length > cap) {
                    send({ t: 'apipatch', ok: false, why: 'path>cap(' + cap + ')', from: opath });
                    return;
                }
                buf.writeUtf8String(PATCH_PATH);
                strObj.add(16).writeU64(PATCH_PATH.length);
                send({ t: 'apipatch', ok: true, from: opath, to: PATCH_PATH });
            } catch (e) {
                send({ t: 'apipatch', ok: false, why: '' + e });
            }
        }
    });
    send({ t: 'apilog', msg: 'hook 就位 (videoeditor.dll!ExportClient::exportStart)' });
    return true;
}

if (!install()) {
    var timer = setInterval(function () {
        if (install()) clearInterval(timer);
    }, 500);
}
