# test_5x.py — 连续跑 5 次 render-draft, 验证稳定性
# 用法: python test_5x.py [草稿文件夹路径] [--desktop]
import subprocess, sys, time, os
HERE = os.path.dirname(os.path.abspath(__file__))
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

import config

_args = [a for a in sys.argv[1:] if not a.startswith('--')]
DRAFT = _args[0] if _args else os.path.join(config.DRAFT_ROOT, '8月11日')
DESKTOP = '--desktop' in sys.argv  # 加 --desktop 跑真后台模式
results = []
for i in range(1, 6):
    print('======== 第 %d/5 次 %s ========' % (i, '(desktop)' if DESKTOP else ''), flush=True)
    t0 = time.time()
    cmd = [sys.executable, os.path.join(HERE, 'render_driver.py'), 'render-draft', DRAFT]
    if DESKTOP: cmd.append('--desktop')
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
    dt = time.time() - t0
    ok = (r.returncode == 0)
    # 找输出 mp4 (最新的 rd*.mp4)
    vids = config.VIDEOS_DIR
    mps = sorted([f for f in os.listdir(vids) if f.startswith('rd') and f.endswith('.mp4')],
                 key=lambda f: os.path.getmtime(os.path.join(vids, f)), reverse=True)
    out = mps[0] if mps else '?'
    results.append((i, ok, dt, out, r.returncode))
    print('  结果: ok=%s 耗时=%.0fs exit=%d 输出=%s' % (ok, dt, r.returncode, out), flush=True)
    # 存每次完整 render_driver 输出, 方便诊断失败
    if r.stdout:
        with open(os.path.join(HERE, 'render_%d.log' % i), 'w', encoding='utf-8') as fh:
            fh.write(r.stdout)
        # 失败时打印关键段 (点结果卡片 到 点导出)
        if not ok:
            lines = r.stdout.splitlines()
            print('  --- 关键步骤 (结果卡片->导出) ---', flush=True)
            for l in lines:
                if any(k in l for k in ['点结果', '编辑器就绪', '编辑器无', '点导出', 'modal win', '导出窗口', 'click(277', 'click(1136', 'confirm', 'win=', 'origin']):
                    print('    ' + l, flush=True)
    if not ok:
        print('  *** 失败, 继续下一次 ***', flush=True)

print('', flush=True)
print('======== 5 次汇总 ========', flush=True)
n_ok = sum(1 for r in results if r[1])
for (i, ok, dt, out, rc) in results:
    print('  #%d: %s  %.0fs  %s  (exit %d)' % (i, '✓' if ok else '✗', dt, out, rc), flush=True)
print('成功 %d/5' % n_ok, flush=True)
