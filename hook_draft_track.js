// hook_draft_track.js — P1d 侦察: Draft 指针追踪.
// 原理: hook convert_to_runtime_draft onLeave 拿返回的 shared_ptr<Draft> (sret,
// args[0] 指向 {ptr,ctrl}), 之后在所有嫌疑函数的参数 (原始槽值 + 一级解引用) 里
// 搜这个指针 — 谁收到它, 谁就是草稿安装入口 (C 方案的直接调用目标).
'use strict';

var draftPtrs = {};   // ptr字符串 -> 标签

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

function isKnown(p) {
    if (p.isNull()) return null;
    var k = p.toString();
    return draftPtrs[k] ? k : null;
}

// 检查 args[0..5]: 原始槽值 + 一级解引用 是否命中已知 Draft 指针
function hunt(api, args, ctx) {
    for (var i = 0; i < 6; i++) {
        try {
            var v = args[i];
            var hit = isKnown(v);
            if (hit) { report(api, i, false, hit, ctx); return; }
            var d = v.readPointer();
            hit = isKnown(d);
            if (hit) { report(api, i, true, hit, ctx); return; }
        } catch (e) {}
    }
}

function report(api, argIdx, deref, ptr, ctx) {
    send({ t: 'hit', api: api, argIdx: argIdx, deref: deref, ptr: ptr,
           tid: Process.getCurrentThreadId(),
           ret: modOff(ctx ? ctx.returnAddress : NULL),
           stack: backtrace(ctx, 10) });
}

var TARGETS = [
    ['?draftTransaction@Session@lyra@@', 'session.draftTransaction'],
    ['?draftCombo@Server@lyra@@', 'server.draftCombo'],
    ['?doInitVeWrapper@Session@lyra@@', 'session.doInitVeWrapper'],
    ['?getJsonFromDraft@MixedClient@@', 'mixed.getJsonFromDraft'],
    ['?getDraftFromJsonStr@MixedClient@@', 'mixed.getDraftFromJsonStr'],
    ['?exportStart@ExportClient@@', 'exportStart'],
    ['?deserialize_persistent_draft@Deserializer@lvve@@', 'deserialize_persistent'],
    ['?convert_to_runtime_draft@Deserializer@lvve@@', 'convert_to_runtime'],
    ['?deserialize_draft@Deserializer@lvve@@', 'deserialize_draft'],
    ['?LoadDraft@lvve@@Y', 'lvve.LoadDraft'],
    ['?closeSession@Server@lyra@@QEAAXJ@Z', 'closeSession'],
    ['?changeSession@Server@lyra@@', 'changeSession'],
    ['?InitPlayerReqStruct', null],  // 占位跳过
];

function install() {
    var mod = Process.findModuleByName('videoeditor.dll');
    if (!mod) return false;
    var hooks = {};
    mod.enumerateExports().forEach(function (e) {
        if (e.type !== 'function') return;
        for (var i = 0; i < TARGETS.length; i++) {
            var pre = TARGETS[i][0];
            if (TARGETS[i][1] && !hooks[TARGETS[i][1]] && e.name.indexOf(pre) === 0)
                hooks[TARGETS[i][1]] = e.address;
        }
    });
    var missing = TARGETS.filter(function (t) { return t[1] && !hooks[t[1]]; })
                         .map(function (t) { return t[1]; });
    send({ t: 'symbols', found: Object.keys(hooks), missing: missing });

    // convert_to_runtime_draft onLeave: 收获 Draft 指针
    Interceptor.attach(hooks['convert_to_runtime'], {
        onEnter: function (args) { this.sret = args[0]; },
        onLeave: function (retval) {
            try {
                var p = this.sret.readPointer();
                if (!p.isNull()) {
                    draftPtrs[p.toString()] = 'draft';
                    send({ t: 'draft', ptr: p.toString(), ctrl: this.sret.add(8).readPointer().toString() });
                }
            } catch (e) { send({ t: 'draft', ptr: '(err ' + e + ')' }); }
        }
    });
    // deserialize_persistent_draft onLeave: PersistentDraft 指针
    Interceptor.attach(hooks['deserialize_persistent'], {
        onEnter: function (args) { this.sret = args[0]; },
        onLeave: function (retval) {
            try {
                var p = this.sret.readPointer();
                if (!p.isNull()) {
                    draftPtrs[p.toString()] = 'persistent';
                    send({ t: 'persistent', ptr: p.toString() });
                }
            } catch (e) {}
        }
    });

    ['session.draftTransaction', 'server.draftCombo', 'session.doInitVeWrapper',
     'mixed.getJsonFromDraft', 'mixed.getDraftFromJsonStr', 'exportStart',
     'deserialize_draft', 'lvve.LoadDraft', 'closeSession', 'changeSession'
    ].forEach(function (name) {
        if (!hooks[name]) return;
        Interceptor.attach(hooks[name], {
            onEnter: function (args) { hunt(name, args, this.context); }
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
