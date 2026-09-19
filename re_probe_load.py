#!/usr/bin/env python3
"""re_probe_load.py — P1b 侦察驱动: 正常跑一次 render-draft (含点卡片),
旁路 hook 草稿装载链的导出符号, 抓 app 打开草稿的调用序列 + 草稿 JSON.

用法 (渲染节点):
    python re_probe_load.py <草稿文件夹路径> [--timeout 1800]
产出: re_probe_out/<ts>/load_trace.jsonl + 控制台实时序列
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(HERE, 're_probe_out')
TRACE_JS = os.path.join(HERE, 'hook_load_trace.js')


def log(m):
    print('[loadprobe] %s' % m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('draft_dir')
    ap.add_argument('--timeout', type=int, default=1800)
    ap.add_argument('--js', default=TRACE_JS, help='hook JS 路径 (可换 hook_invoke_trace.js 等)')
    args = ap.parse_args()
    if not os.path.isdir(args.draft_dir):
        log('草稿目录不存在: %s' % args.draft_dir)
        return 2
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    outdir = os.path.join(OUT_ROOT, time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(outdir, exist_ok=True)
    jsonl_path = os.path.join(outdir, 'load_trace.jsonl')
    jsonl = open(jsonl_path, 'w', encoding='utf-8')
    seq = [0]

    import frida
    import render_driver as rd

    argv = ['render-draft', args.draft_dir, '--desktop', '--desktop-name', rd.JY_DESKTOP]
    # P3 api 模式会抢在面板阶段结束流程, 装载侦察只关心点卡片阶段 — 强制 ui 模式
    # 无所谓: 点卡片在 api 阶段之前, 但为避免 api 补丁干扰, 统一 ui 环境变量.
    env = dict(os.environ, RENDER_EXPORT_MODE='ui')
    cmd = [sys.executable, os.path.join(HERE, 'render_driver.py')] + argv
    log('启动渲染 (RENDER_EXPORT_MODE=ui): %s' % ' '.join(argv))
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding='utf-8', errors='replace')

    driver_pid = []

    def _pump():
        for line in proc.stdout:
            sys.stdout.write('[drv] %s' % line)
            sys.stdout.flush()
            m = re.search(r'主进程 PID=(\d+)', line)
            if m:
                driver_pid.append(int(m.group(1)))

    threading.Thread(target=_pump, daemon=True).start()

    def find_jy_pids():
        """tasklist 轮询 JianyingPro.exe — 进程出生即发现, 不等窗口 (跨桌面枚举不到窗口)."""
        try:
            out = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq JianyingPro.exe', '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return []
        return [int(m.group(1)) for m in re.finditer(r'"JianyingPro\.exe","(\d+)"', out)]

    attached = set()
    session = None
    deadline = time.time() + args.timeout
    while time.time() < deadline and proc.poll() is None:
        if session is None:
            # 进程出生即 attach (卡片点击发生在启动后 ~33s, 必须抢先)
            for pid in find_jy_pids():
                if pid in attached:
                    continue
                attached.add(pid)
                try:
                    log('发现剪映进程 PID=%d, 立即 attach 挂装载链 trace...' % pid)
                    s = frida.get_local_device().attach(pid)
                    script = s.create_script(open(args.js, encoding='utf-8').read())

                    def on_msg(message, data):
                        if message['type'] == 'error':
                            log('JS 异常: %s | %s' % (message.get('description'),
                                                      message.get('stack')))
                            return
                        if message['type'] != 'send':
                            return
                        p = message['payload']
                        t = p.get('t')
                        if t == 'symbols':
                            log('符号命中=%s 缺失=%s' % (p['found'], p['missing']))
                        elif t == 'ready':
                            log('装载链 hook 就位 (PID 分支)')
                        elif t in ('draft', 'persistent'):
                            seq[0] += 1
                            jsonl.write(json.dumps(p, ensure_ascii=False) + '\n')
                            jsonl.flush()
                            log('★ %s ptr=%s (已登记追踪)' % (t, p.get('ptr')))
                        elif t == 'hit':
                            seq[0] += 1
                            jsonl.write(json.dumps(p, ensure_ascii=False) + '\n')
                            jsonl.flush()
                            log('★★★ 命中! %s arg[%d]%s 收到指针 %s (tid=%s ret=%s)' % (
                                p['api'], p['argIdx'],
                                ' 解引用' if p.get('deref') else '', p['ptr'],
                                p.get('tid'), p.get('ret')))
                            log('    栈: %s' % ' <- '.join(p.get('stack', [])[:8]))
                        elif t in ('load', 'invoke'):
                            seq[0] += 1
                            jsonl.write(json.dumps(p, ensure_ascii=False) + '\n')
                            jsonl.flush()
                            label = p.get('api') or ('invoke svc=%r api=%r' % (p.get('svc'), p.get('api')))
                            log('#%d %s (tid=%s ret=%s)' % (seq[0], label,
                                                            p.get('tid'), p.get('ret')))
                            for k in ('str1', 'str2', 'deref1'):
                                if p.get(k):
                                    log('    %s=%r' % (k, p[k][:180]))
                            log('    栈: %s' % ' <- '.join(p.get('stack', [])[:6]))
                    script.on('message', on_msg)
                    script.load()
                    session = s
                    log('hook 已挂 (%d 个进程已 attach: %s)' % (len(attached), sorted(attached)))
                    break
                except Exception as e:
                    log('attach PID=%d 失败: %r' % (pid, e))
        if session is not None:
            break
        time.sleep(0.5)

    while proc.poll() is None and time.time() < deadline:
        time.sleep(2)
    if proc.poll() is None:
        proc.kill()
    jsonl.close()
    log('渲染子进程 exit=%s' % proc.returncode)
    log('装载 trace 已存: %s (%d 条)' % (jsonl_path, seq[0]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
