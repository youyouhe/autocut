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

// P2-A: 飞行中补丁 — 非空时, exportStart onEnter 把请求 +0x50 的输出路径
// 原位改写为此路径 (要求目标 ≤ 原堆缓冲容量, 避免重分配/悬垂指针).
var PATCH_PATH = null;
rpc.exports.setpatch = function (p) {
    PATCH_PATH = p;
    send({ t: 'event', api: 'setpatch', path: p });
    return true;
};

var HOOKED = false;
var DUMP_CAP = 4 * 1024 * 1024;   // 单次 dump 上限 4MB (上次实测 288KB 全块)
var PAGE = 4096;

function modOff(addr) {
    var m = null;
    try { m = Process.findModuleByAddress(addr); }  // 注意: 全名是 ByAddress, 不是 ByAddr
    catch (e) { /* 老版本兜底 */ try { m = Process.findModuleByAddr(addr); } catch (e2) {} }
    return m ? (m.name + '+0x' + addr.sub(m.base).toString(16)) : ('' + addr);
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
// 每读一页立刻 send, 不在 JS 里攒大 buffer. cap 限制总量 (栈上引用可达数 MB).
function streamDump(id, base, cap) {
    cap = cap || DUMP_CAP;
    var total = 0;
    for (var off = 0; off < cap; off += PAGE) {
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
        ['?exportAudioStart@ExportClient@@', 'exportAudioStart'],
        ['?export_start@NewVEWrapper@lvve@@', 'newVE_export_start'],
        ['?export_cancel@NewVEWrapper@lvve@@', 'newVE_export_cancel'],
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

    // 读 MSVC std::string (16字节 buf/ptr + size@16 + cap@24); cap>15 时 buf 是堆指针.
    // 只在 dump 内存范围内跟指针, 防野指针.
    function readStdString(sp, dumpBase, dumpLen) {
        var size, cap, out = { size: -1, s: '' };
        try {
            size = sp.add(16).readU64().toNumber();
            cap = sp.add(24).readU64().toNumber();
        } catch (e) { return out; }
        if (cap === 15 && size <= 15) {
            out.size = size;
            out.s = sp.readUtf8String(size);
            return out;
        }
        if (cap > 15 && size <= cap && size < 1048576) {
            var p = sp.readPointer();
            if (dumpBase && !(p.compare(dumpBase) >= 0 && p.compare(dumpBase.add(dumpLen)) < 0)) {
                out.s = '(ptr-out-of-dump 0x' + p.toString(16) + ')';
                out.size = size;
                return out;
            }
            try { out.s = p.readUtf8String(Math.min(size, 4096)); out.size = size; } catch (e) {}
        }
        return out;
    }

    // NewVEWrapper::export_start(this, const string& out, const ExportConfig&,
    //                            fn progress, fn done, string extra)
    // ExportConfig 是明文参数结构体 — P2 直调的核心目标, 全量 dump.
    function rttiName(objPtr) {
        // MSVC x64: vtable[-1] → CompleteObjectLocator, COL+12 = TypeDescriptor RVA,
        // TypeDescriptor+16 = 类名 (".?AVReqStruct@lyra@@")
        try {
            var vf = objPtr.readPointer();
            var col = vf.add(-8).readPointer();
            var mod = Process.findModuleByAddress(vf);
            if (!mod) return '(no-mod)';
            var td = mod.base.add(col.add(12).readU32());
            return td.add(16).readCString(160);
        } catch (e) { return '(rtti-err ' + e + ')'; }
    }

    // 结构体漫游: 8字节步进分类字段, 堆字符串/指针指向的内容直接解引用读出.
    // len 字节内的形状判断需要读 base+o+16/+24, 允许越界 (try 包裹).
    function structWalk(base, len) {
        var out = [];
        for (var o = 0; o < len; o += 8) {
            var v;
            try { v = base.add(o).readPointer(); } catch (e) { break; }
            var ent = { off: o };
            if (v.isNull()) { ent.k = 'null'; out.push(ent); continue; }
            var done = false;
            try {
                var size = base.add(o + 16).readU64().toNumber();
                var cap = base.add(o + 24).readU64().toNumber();
                if (cap > 15 && size <= cap && size < 65536 && size > 0) {
                    ent.k = 'string'; ent.size = size;
                    ent.s = v.readUtf8String(Math.min(size, 300));
                    out.push(ent); done = true;
                } else if (cap === 15 && size > 0 && size <= 15) {
                    var s2 = base.add(o).readUtf8String(size);
                    if (/^[\x20-\x7e]+$/.test(s2)) {
                        ent.k = 'sso'; ent.s = s2;
                        out.push(ent); done = true;
                    }
                }
            } catch (e) { /* 非 string 形状 */ }
            if (done) continue;
            try {
                var st = v.readUtf8String(96);
                if (st && /^[\x20-\x7e]{6,}/.test(st)) {
                    ent.k = 'ptr-ascii'; ent.s = st.slice(0, 160);
                    out.push(ent); continue;
                }
                var w = v.readUtf16String(96);
                if (w && /^[\x20-\x7e]{6,}/.test(w)) {
                    ent.k = 'ptr-wide'; ent.s = w.slice(0, 160);
                    out.push(ent); continue;
                }
                ent.k = 'ptr'; ent.s = '' + v;
            } catch (e) {
                ent.k = 'int?'; ent.s = '' + v;
            }
            out.push(ent);
        }
        return out;
    }

    if (found.newVE_export_start) {
        Interceptor.attach(found.newVE_export_start, {
            onEnter: function (args) {
                var id = ++seq;
                var meta = { t: 'end', id: id, api: 'newVE_export_start',
                             tid: Process.getCurrentThreadId(),
                             ret: modOff(this.returnAddress),
                             stack: backtrace(this.context).slice(0, 8) };
                try { meta.thisPtr = args[0].toString(); } catch (e) {}
                // dump ExportConfig (args[2]) 上限 64KB (引用可能指向栈, 防拉满栈)
                try {
                    var cfg = args[2];
                    meta.cfgPtr = cfg.toString();
                    meta.cfgBytes = streamDump(id, cfg, 64 * 1024);
                } catch (e) { meta.cfgBytes = 0; }
                // 读输出路径字符串 (args[1]) — 不在 cfg dump 范围, 直接读
                try {
                    meta.outArg = readStdString(args[1], ptr('0'), 0).s;
                } catch (e) { meta.outArg = '(err ' + e + ')'; }
                // 顺带 dump this 对象头部 64KB (NewVEWrapper 内含引擎状态, 供 P2 参考)
                try {
                    var id2 = ++seq;
                    streamDump(id2, args[0], 64 * 1024);
                    meta.thisDumpId = id2;
                } catch (e) {}
                try { meta.extraArg = readStdString(args[5], ptr('0'), 0).s; } catch (e) {}
                send(meta);
            }
        });
    }
    if (found.newVE_export_cancel) {
        Interceptor.attach(found.newVE_export_cancel, {
            onEnter: function (args) {
                send({ t: 'event', api: 'newVE_export_cancel',
                       thisPtr: args[0].toString(),
                       stack: backtrace(this.context).slice(0, 6) });
            }
        });
    }

    // 导出请求族: 统一 dump. exportStart 是主目标; cancel/composition 兜底观察
    // 5.9 是否走旧 API (真导出触发时若两路都响, 对比即可分辨实际链路).
    ['exportStart', 'exportCancel', 'exportCompFileSync2', 'exportCompFileSync'].forEach(function (name) {
        var addr = found[name];
        if (!addr) return;
        Interceptor.attach(addr, {
            onEnter: function (args) {
                var id = ++seq;
                function fail(step, e) {
                    send({ t: 'hookerr', id: id, api: name, step: step, msg: '' + e,
                           stack: (e && e.stack) ? ('' + e.stack) : null });
                }
                var meta = { t: 'end', id: id, api: name, reqPtr: 'null' };
                var req = null, reqOK = false;
                try { meta.tid = Process.getCurrentThreadId(); } catch (e) { fail('tid', e); }
                try { meta.ret = modOff(this.returnAddress); } catch (e) { fail('ret', e); }
                try { meta.stack = backtrace(this.context); } catch (e) { fail('bt', e); }
                try {
                    req = args[0].readPointer();       // shared_ptr 按值 → RCX 指向 {ReqStruct*, ctrl}
                    meta.reqPtr = req.toString();
                    if (!req.isNull()) {
                        reqOK = true;
                        meta.bytes = streamDump(id, req);
                        // 结构体漫游: 前 2KB 字段分类 + RTTI 类名
                        try {
                            send({ t: 'struct', id: id, api: name,
                                   rtti: rttiName(req),
                                   fields: structWalk(req, 2048) });
                        } catch (e) { fail('walk', e); }
                    } else { meta.bytes = 0; }
                } catch (e) {
                    fail('req', e);
                    meta.bytes = 0;
                }
                try { meta.sid = args[3].toInt32(); } catch (e) { /* 无尾参的 API */ }
                // P2-A: 原位改写 +0x50 输出路径 (MSVC string: ptr@0 size@16 cap@24;
                // 堆串才可原位改写 — 只覆盖 ≤cap 字节, 指针不动, 引擎照常 free)
                if (PATCH_PATH && reqOK) {
                    try {
                        var strObj = req.add(0x50);
                        var osize = strObj.add(16).readU64().toNumber();
                        var ocap = strObj.add(24).readU64().toNumber();
                        meta.patched = { origSize: osize, cap: ocap };
                        if (ocap > 15 && PATCH_PATH.length <= ocap) {
                            var obuf = strObj.readPointer();
                            meta.patched.from = obuf.readUtf8String(osize);
                            obuf.writeUtf8String(PATCH_PATH);
                            strObj.add(16).writeU64(PATCH_PATH.length);
                            meta.patched.to = PATCH_PATH;
                        } else {
                            meta.patched.err = ocap <= 15 ? 'SSO 内联串不可原位改写'
                                                          : '新路径超长 cap=' + ocap;
                        }
                    } catch (e) { meta.patched = { err: '' + e }; }
                }
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

    // === CreateFileW  chokepoint ===
    // 真实渲染入口在更深的 RPC 分发层 (ExportClient::exportStart 与
    // NewVEWrapper::export_start 都只在面板开/收尾触发). 但导出必然创建
    // .__jianying_export_temp_folder__ 下的临时文件 — 按路径过滤 CreateFileW
    // + 回溯调用栈, 直接定位真正启动导出的函数, 与调用层数无关.
    try {
        // Frida 17 移除了 Module.getExportByName(module, name) 静态方法, 双写法兜底
        var createFileW = null;
        try { createFileW = Module.getGlobalExportByName('CreateFileW'); } catch (e1) {}
        if (!createFileW) {
            try {
                var kb = Process.findModuleByName('kernelbase.dll');
                if (kb) createFileW = kb.getExportByName('CreateFileW');
            } catch (e2) {}
        }
        if (!createFileW) throw new Error('CreateFileW 未找到');
        Interceptor.attach(createFileW, {
            onEnter: function (args) {
                try {
                    var path = args[0].readUtf16String();
                    if (path && path.indexOf('jianying_export_temp_folder') >= 0) {
                        send({ t: 'event', api: 'CreateFileW', path: path,
                               stack: backtrace(this.context).slice(0, 14) });
                    }
                } catch (e) { /* 路径读不出, 忽略 */ }
            }
        });
    } catch (e) {
        send({ t: 'event', api: 'createfilew-hook-failed', path: '' + e });
    }

    // === 其余导出家族 log-only (不带 dump, 摸清确认点击时的完整调用序列) ===
    var logOnly = [
        ['?exportAudioStart@ExportClient@@', 'exportAudioStart'],
        ['?endComposeCoverCmd@CoverClient@@', 'endComposeCover'],
        ['?GetLastExportInfo@Muxer@lvve@@', 'getLastExportInfo'],
        ['?cancelExportSegment@SingleSegmentPlayClient@@', 'cancelExportSegment'],
    ];
    logOnly.forEach(function (w) {
        var addr = found[w[1]];
        if (!addr) return;
        Interceptor.attach(addr, {
            onEnter: function () {
                send({ t: 'event', api: w[1], stack: backtrace(this.context).slice(0, 6) });
            }
        });
    });

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
