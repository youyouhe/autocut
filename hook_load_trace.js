// hook_load_trace.js — P1b 侦察: 草稿装载链追踪.
// 背景 (RTTI 实锤): 5.9 存在 DraftInitReqStruct / RestoreDraftReqStruct 消息, 但
// Server::invoke 分发器未导出, 无任何导出函数以它们为参数. 可达的"原料层"全是
// 导出符号 — 在驱动点卡片的瞬间 hook 它们, 抓装载序列 + 草稿 JSON 本体 + 回溯栈.
// 由 re_probe_load.py 作为旁路 frida 会话装载.
'use strict';

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

// MSVC std::string 读取 (16字节 buf/ptr + size@16 + cap@24)
function readStdString(sp) {
    var size, cap;
    try {
        size = sp.add(16).readU64().toNumber();
        cap = sp.add(24).readU64().toNumber();
    } catch (e) { return null; }
    try {
        if (cap === 15 && size <= 15) return sp.readUtf8String(size);
        if (cap > 15 && size <= cap && size < 8388608)
            return sp.readPointer().readUtf8String(Math.min(size, 500));
    } catch (e) {}
    return null;
}

var TARGETS = [
    // [导出名前缀, 标签, 取字符串的 arg 序号列表 (sret 之后)]
    ['?GetDraftFromJson@lvve@@', 'lvve.GetDraftFromJson', [1]],
    ['?GetJsonFromDraft@lvve@@', 'lvve.GetJsonFromDraft', []],
    ['?deserialize_persistent_draft@Deserializer@lvve@@', 'lvve.deserialize_persistent_draft', [1]],
    ['?deserialize_draft@Deserializer@lvve@@', 'lvve.deserialize_draft', [1]],
    ['?convert_to_runtime_draft@Deserializer@lvve@@', 'lvve.convert_to_runtime_draft', []],
    ['?getDraftFromJsonStr@MixedClient@@', 'mixed.getDraftFromJsonStr', [1, 2]],
    ['?getJsonFromDraft@MixedClient@@', 'mixed.getJsonFromDraft', [1, 2]],
    ['?draftCombo@Server@lyra@@', 'server.draftCombo', [2]],
    ['?draftTransaction@Session@lyra@@', 'session.draftTransaction', []],
    ['?doInitVeWrapper@Session@lyra@@', 'session.doInitVeWrapper', []],
    ['?LoadDrafts@DraftStoreManager@draft_store@@', 'store.LoadDrafts', [1]],
    ['?LoadRootDrafts@DraftStoreManager@draft_store@@', 'store.LoadRootDrafts', []],
    ['?LoadDraft@lvve@@Y', 'lvve.LoadDraft', [1]],
    ['?LoadIntegratedLogic@DraftTransform@lvve@@', 'transform.LoadIntegratedLogic', []],
    ['?EnsureDraftRenderIndex@lvve@@', 'lvve.EnsureDraftRenderIndex', []],
];

function install() {
    var mod = Process.findModuleByName('videoeditor.dll');
    if (!mod) return false;
    var found = {};
    mod.enumerateExports().forEach(function (e) {
        if (e.type !== 'function') return;
        for (var i = 0; i < TARGETS.length; i++) {
            if (!found[TARGETS[i][1]] && e.name.indexOf(TARGETS[i][0]) === 0)
                found[TARGETS[i][1]] = e.address;
        }
    });
    var missing = TARGETS.map(function (t) { return t[1]; })
                         .filter(function (n) { return !found[n]; });
    send({ t: 'symbols', found: Object.keys(found), missing: missing });

    Object.keys(found).forEach(function (name) {
        var strArgs = TARGETS.filter(function (t) { return t[1] === name; })[0][2];
        Interceptor.attach(found[name], {
            onEnter: function (args) {
                var rec = { t: 'load', api: name, tid: Process.getCurrentThreadId(),
                            ret: modOff(this.returnAddress),
                            stack: backtrace(this.context, 10) };
                strArgs.forEach(function (i) {
                    try {
                        var s = readStdString(args[i]);
                        if (s && s.length > 3) rec['str' + i] = s.slice(0, 400);
                    } catch (e) {}
                });
                // shared_ptr 形参样本: 解引用第一个指针读字符串样本 (可能是 draft 对象)
                try {
                    var p1 = args[1].readPointer();
                    if (!p1.isNull()) {
                        var s = p1.readUtf8String(120);
                        if (s && /^[\x20-\x7e]{8,}/.test(s)) rec.deref1 = s.slice(0, 150);
                    }
                } catch (e) {}
                send(rec);
            }
        });
    });

    send({ t: 'ready' });
    return true;
}

if (!install()) {
    var timer = setInterval(function () {
        if (install()) clearInterval(timer);
    }, 500);
}
