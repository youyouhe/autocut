import os, time, datetime

VIDEOS = r'C:\Users\Administrator\Videos'
TEMP = os.path.join(VIDEOS, '.__jianying_export_temp_folder__')
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'render_monitor.log')

def log(msg):
    line = '%s %s' % (datetime.datetime.now().strftime('[%H:%M:%S]'), msg)
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

def list_mp4(folder):
    out = {}
    try:
        for n in os.listdir(folder):
            if n.lower().endswith('.mp4'):
                p = os.path.join(folder, n)
                try:
                    st = os.stat(p)
                    out[n] = (st.st_size, st.st_mtime)
                except: pass
    except: pass
    return out

def list_temp():
    out = {}
    try:
        for n in os.listdir(TEMP):
            p = os.path.join(TEMP, n)
            try:
                st = os.stat(p)
                if st.st_size > 0:
                    out[n] = st.st_size
            except: pass
    except: pass
    return out

# 基线
baseline_mp4 = list_mp4(VIDEOS)
log('MONITOR START')
log('baseline Videos mp4 count=%d, newest=%s' % (len(baseline_mp4),
    max(baseline_mp4.items(), key=lambda x: x[1][1])[0] if baseline_mp4 else 'none'))
log('watching: %s' % TEMP)
log('watching: %s' % VIDEOS)

state = 'idle'   # idle -> rendering -> done
stable_count = 0
last_temp_size = 0
last_new_mp4 = None

try:
    while True:
        time.sleep(1.5)
        temp = list_temp()
        mp4 = list_mp4(VIDEOS)

        # 找比基线新的 mp4
        new_mp4s = {n:s for n,s in mp4.items() if n not in baseline_mp4 or baseline_mp4.get(n,(0,0))[1] != s[1]}
        # 实际上找基线里没有的名字
        brand_new = {n:s for n,s in mp4.items() if n not in baseline_mp4}

        temp_total = sum(temp.values())

        if state == 'idle':
            if temp or brand_new:
                state = 'rendering'
                stable_count = 0
                log('>>> RENDER STARTED (temp_files=%d brand_new_mp4=%d)' % (len(temp), len(brand_new)))
                if temp: log('    temp: %s' % temp)
                if brand_new: log('    new mp4: %s' % {n:s[0] for n,s in brand_new.items()})
                last_temp_size = temp_total

        if state == 'rendering':
            # 报告进度 (temp 或 new mp4 增长)
            if temp_total != last_temp_size:
                log('    rendering... temp_total=%d bytes' % temp_total)
                last_temp_size = temp_total
            # 检查完成: temp 空 且 有新 mp4 且 其大小稳定
            newest = None
            if brand_new:
                newest = max(brand_new.items(), key=lambda x: x[1][1])
            if not temp and newest:
                # temp 清空了, 检查 newest mp4 大小稳定
                cur_size = newest[1][0]
                if cur_size == getattr(state, 'stable_size', None) or last_new_mp4 == newest[0]:
                    stable_count += 1
                else:
                    stable_count = 0
                last_new_mp4 = newest[0]
                if stable_count >= 2:
                    log('<<< RENDER DONE  output=%s size=%d' % (newest[0], newest[1][0]))
                    state = 'idle'
                    stable_count = 0
                    last_new_mp4 = None
                    # 更新基线 (把这个 mp4 算入基线, 避免重复触发)
                    baseline_mp4[newest[0]] = newest[1]
except KeyboardInterrupt:
    log('MONITOR STOPPED')
