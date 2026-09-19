#!/usr/bin/env python3
"""re_probe_p1.py — P1 侦察驱动: 旁路 hook exportStart 请求结构 + 正常跑一次 UI 渲染.

不改动生产点击链: 本脚本用第二个独立 frida 会话把 hook_export_trace.js 注入剪映,
再以 render_service 同款参数子进程跑 render_driver render-draft, 真实导出发生时
被动 dump ExportStartReqStruct 到 re_probe_out/<ts>/, 供 Linux 侧重建 5.9 字段布局.

用法 (渲染节点, portable 包的 app 目录下):
    python re_probe_p1.py <草稿文件夹路径> [--name 名字] [--timeout 1800] [--no-desktop]

产出 (re_probe_out/<ts>/):
    meta.json       每次 exportStart 的元数据 (sid/调用点/回溯栈)
    req_<id>.bin    ReqStruct 原始内存 dump
    strings.txt     dump 内 ASCII/UTF-16 字符串及偏移 (字段定位的主线索)
    summary.txt     汇总报告
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(HERE, 're_probe_out')
TRACE_JS = os.path.join(HERE, 'hook_export_trace.js')


def log(m):
    print('[probe] %s' % m, flush=True)


def extract_strings(data, min_len=6):
    """dump 内提字符串: ASCII 与 UTF-16LE 各扫一遍, 返回 [(偏移, 类型, 内容)]."""
    out = []
    for m in re.finditer(rb'[\x20-\x7e]{%d,}' % min_len, data):
        out.append((m.start(), 'a', m.group().decode('ascii')))
    # UTF-16LE: 可见字符 + \x00 交替
    for m in re.finditer((rb'(?:[\x20-\x7e]\x00){%d,}' % min_len), data):
        out.append((m.start(), 'w', m.group().decode('utf-16-le')))
    out.sort()
    return out


def summarize_strings(strings):
    """挑出对字段定位最有用的: 路径 / URL / 疑似配置 KEY / 数字文件名."""
    keep = []
    for off, kind, s in strings:
        if re.search(r'[\\/]|://|^[A-Za-z][A-Za-z0-9_./-]{4,}$', s):
            keep.append((off, kind, s))
    return keep


class TraceCollector:
    def __init__(self, outdir):
        self.outdir = outdir
        self.dumps = {}          # id -> file handle info
        self.metas = {}
        self.events = []

    def on_message(self, message, data):
        if message['type'] != 'send':
            if message['type'] == 'error':
                log('JS 异常: %s | stack: %s' % (
                    message.get('description'), message.get('stack')))
            return
        p = message['payload']
        t = p.get('t')
        if t == 'hookerr':
            log('HOOK 分步错误 id=%s api=%s step=%s: %s\n  stack: %s' % (
                p.get('id'), p.get('api'), p.get('step'), p.get('msg'),
                p.get('stack')))
            return
        if t == 'symbols':
            log('符号定位: 命中=%s 缺失=%s' % (p['found'], p['missing']))
        elif t == 'ready':
            log('hook 已就位 (videoeditor.dll)')
        elif t == 'chunk':
            fid = p['id']
            info = self.dumps.get(fid)
            if info is None:
                path = os.path.join(self.outdir, 'req_%d.bin' % fid)
                info = self.dumps[fid] = {'path': path, 'f': open(path, 'wb'),
                                          'expect_off': 0}
            if p['off'] != info['expect_off']:
                log('警告: dump %d 块乱序 off=%d 期望=%d' % (fid, p['off'], info['expect_off']))
            info['f'].write(data)
            info['expect_off'] = p['off'] + len(data)
        elif t == 'struct':
            import json as _json
            path = os.path.join(self.outdir, 'struct_%s_%s.json' % (p.get('id'), p.get('api')))
            with open(path, 'w', encoding='utf-8') as f:
                _json.dump(p, f, ensure_ascii=False, indent=1)
            log('结构体漫游已存: %s (RTTI=%s, %d 字段)' % (
                os.path.basename(path), p.get('rtti'), len(p.get('fields') or [])))
        elif t == 'end':
            self.metas[p['id']] = p
            if 'f' in self.dumps.get(p['id'], {}):
                self.dumps[p['id']]['f'].close()
            log('=== %s #%d 触发! sid=%s ReqStruct=%s 大小=%s 调用点=%s' % (
                p['api'], p['id'], p.get('sid'), p.get('reqPtr'),
                p.get('bytes'), p.get('ret')))
        elif t == 'event':
            self.events.append(p)
            extra = (' path=%s' % p['path']) if p.get('path') else ''
            log('event: %s%s' % (p['api'], extra))
            if p.get('stack'):
                log('  栈: %s' % ' <- '.join(p['stack'][:10]))

    def finalize(self):
        strings_report = []
        for fid, meta in sorted(self.metas.items()):
            path = os.path.join(self.outdir, 'req_%d.bin' % fid)
            if not os.path.exists(path):
                continue
            data = open(path, 'rb').read()
            strings = extract_strings(data)
            keep = summarize_strings(strings)
            with open(os.path.join(self.outdir, 'strings_%d.txt' % fid), 'w',
                      encoding='utf-8') as f:
                f.write('# req_%d.bin  %d bytes  api=%s sid=%s\n' % (
                    fid, len(data), meta.get('api'), meta.get('sid')))
                f.write('# 调用点: %s\n# 栈: %s\n\n' % (
                    meta.get('ret'), ' <- '.join(meta.get('stack', [])[:8])))
                for off, kind, s in keep:
                    f.write('%8d  %s  %s\n' % (off, kind, s))
            strings_report.append({'id': fid, 'bytes': len(data),
                                   'strings_total': len(strings),
                                   'strings_kept': len(keep)})
        with open(os.path.join(self.outdir, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump({'metas': self.metas, 'events': self.events,
                       'dumps': strings_report}, f, ensure_ascii=False, indent=2)
        log('产出目录: %s (%d 个 dump)' % (self.outdir, len(self.metas)))


def wait_jianying_pid(timeout=300):  # noqa: 保留备用 (当前主流程改由驱动日志驱动 attach)
    """等 render_driver 子进程把剪映拉起来, 拿主进程 PID."""
    import render_driver as rd
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = rd.find_main_pid()
        if pid:
            return pid
        time.sleep(2)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('draft_dir', help='草稿文件夹路径 (同 render-draft 参数)')
    ap.add_argument('--name', default=None, help='注入后草稿名 (默认 rd+毫秒)')
    ap.add_argument('--timeout', type=int, default=1800, help='整体墙钟上限秒')
    ap.add_argument('--no-desktop', action='store_true', help='前台模式 (默认独立桌面)')
    ap.add_argument('--patch-out', default=None,
                    help='P2-A: 改写 exportStart 请求 +0x50 输出路径到此路径 (mp4 全路径)')
    args = ap.parse_args()
    if not os.path.isdir(args.draft_dir):
        log('草稿目录不存在: %s' % args.draft_dir)
        return 2
    if not os.path.exists(TRACE_JS):
        log('缺少 %s' % TRACE_JS)
        return 2
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    outdir = os.path.join(OUT_ROOT, time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(outdir, exist_ok=True)

    import frida
    import render_driver as rd

    argv = ['render-draft', args.draft_dir]
    if args.name:
        argv.append(args.name)
    if not args.no_desktop:
        argv += ['--desktop', '--desktop-name', rd.JY_DESKTOP]
    cmd = [sys.executable, os.path.join(HERE, 'render_driver.py')] + argv
    log('启动渲染: %s' % ' '.join(argv))

    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace')

    # 实时转发驱动日志 (教训: 先等 PID 再转发会把 attach 失败等真实报错
    # 扣在管道里, 用户面对 5 分钟黑屏无从判断 — 首轮侦察 18:33 栽在这).
    driver_pid = []  # 捕获驱动自己 attach 成功的 PID
    def _pump():
        for line in proc.stdout:
            sys.stdout.write('[drv] %s' % line)
            sys.stdout.flush()
            m = re.search(r'主进程 PID=(\d+)', line)
            if m:
                driver_pid.append(int(m.group(1)))

    import threading
    t = threading.Thread(target=_pump, daemon=True)
    t.start()

    # 等驱动自己完成 attach (日志出现 '主进程 PID=' 后再缓 3s), 再挂旁路 hook.
    # 两个 frida 会话同时往刚启动的进程里注入会互踩 — 首轮失败根因候选.
    collector = TraceCollector(outdir)
    deadline = time.time() + args.timeout
    script = None
    session = None
    while time.time() < deadline and proc.poll() is None:
        if driver_pid and driver_pid[0]:
            time.sleep(3)
            pid = driver_pid[0]
            for attempt in range(3):
                try:
                    log('驱动已 attach (PID=%d), 挂旁路 trace hook (第 %d 次)...'
                        % (pid, attempt + 1))
                    session = frida.get_local_device().attach(pid)
                    script = session.create_script(
                        open(TRACE_JS, encoding='utf-8').read())
                    collector = TraceCollector(outdir)
                    script.on('message', collector.on_message)
                    script.load()
                    if args.patch_out:
                        pdir = os.path.dirname(os.path.abspath(args.patch_out))
                        os.makedirs(pdir, exist_ok=True)
                        script.exports_sync.setpatch(args.patch_out)
                        log('P2-A 补丁已启用: 输出路径 → %s' % args.patch_out)
                    break
                except Exception as e:
                    log('旁路 attach 失败: %r' % e)
                    session = None
                    script = None
                    time.sleep(5)
            break
        time.sleep(2)

    if script is None and proc.poll() is None:
        log('警告: 未能挂旁路 hook (驱动未报 attach 或连败), 渲染继续但无 dump')

    deadline = time.time() + args.timeout
    try:
        while proc.poll() is None and time.time() < deadline:
            time.sleep(2)
        if proc.poll() is None:
            log('墙钟超时, 杀渲染子进程')
            proc.kill()
    finally:
        if script is not None:
            collector.finalize()
        log('渲染子进程 exit=%s' % proc.returncode)
        n = len(collector.metas)
        patched = [m for m in collector.metas.values() if m.get('patched')]
        for m in patched:
            log('P2-A 补丁记录 id=%s: %s' % (m['id'], json.dumps(
                m.get('patched'), ensure_ascii=False)))
        if n:
            log('P1/P2 成功: 捕获 %d 次 exportStart 族调用' % n)
            if args.patch_out and not patched:
                log('注意: 启用了补丁但没有 patched 记录 — 检查 setpatch 是否生效')
            log('下一步: ①检查补丁路径是否真的落了 mp4 ②把 re_probe_out 传回 Linux')
        else:
            log('P1 无捕获 — 检查: ①videoeditor.dll 是否挂钩成功(symbols 行) '
                '②渲染是否真的走到导出 ③上面 [drv] 行里驱动的真实报错')
        return 0 if n and proc.returncode == 0 else (0 if n else 1)


if __name__ == '__main__':
    sys.exit(main())
