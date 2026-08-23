# upgrade_watchdog.py — 剪映强制升级看守 (render 节点常驻防线)
#
# 背景 (2026-08 实测): 服务端对老版本剪映强制下发升级包, 且本机若以管理员高完整性
# 运行 (UAC 关), 更新器能剥掉 Apps 目录上的 deny-create ACL —— 纯 ACL 防线会被绕过。
# 实测强更链路: 启动后 ~30s 把 732MB 安装器写进 User Data\Download\update.exe,
# ~45s 在 Apps 根建时间戳暂存目录 (<14位数字>_N), ~80s 新版本目录落地 + 复位根目录
# 启动器 JianyingPro.exe + 重写 packet.xml/Configure.ini。
#
# 本看守每 interval 秒扫一次这些签名, 命中即:
#   杀进程 -> 隔离暂存目录/新版本目录/复位启动器 -> 还原 packet.xml/Configure.ini
#   -> 重上 deny-create ACL (尽力而为)。
#
# 用法:
#   render_service 启动时自动拉起 (env UPGRADE_WATCHDOG=0 关闭);
#   也可独立跑: python upgrade_watchdog.py
import os
import re
import time
import subprocess
import threading

try:
    sys_path = os.path.dirname(os.path.abspath(__file__))
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import config
    APPS_BASE = config.JY_APP_BASE
except Exception:
    APPS_BASE = os.path.join(os.environ.get('USERPROFILE', ''), 'AppData', 'Local',
                             'JianyingPro', 'Apps')

DOWNLOAD_DIR = os.path.join(os.path.dirname(APPS_BASE), 'User Data', 'Download')

INTERVAL = float(os.environ.get('UPGRADE_WATCHDOG_INTERVAL', '3'))
COOLDOWN = 150  # 干预后冷却秒数 (> update.exe 新鲜窗口 120s, 避免空转重复干预)

STAGING_RE = re.compile(r'^\d{10,}(_\d+)?$')       # 时间戳暂存目录 20268230121552_1
VERSION_RE = re.compile(r'^\d+\.\d+\.\d+\.\d+$')   # 正式版本目录 5.9.0.11632
ROOT_KEEP_FILES = {'configure.ini', 'jianyingpropacket.xml', 'uninst.exe'}
UPDATER_PROCS = ('update.exe', 'veupdateshell.exe')

PACKET_XML = '''<?xml version="1.0" encoding="UTF-8" ?>
<packet>
    <file>
        <item path="\\\\JYPacket" filetype="4" />
    </file>
    <cid cid="jianyingpro_0" />
    <appver value="{minor}.{feature}" />
    <grayver value="" />
    <verify enable="1" />
    <full_appver value="{full}" />
    <malloc_scheme value="system" />
    <release_type value="release" />
</packet>'''


def _log(msg):
    print('[upgrade_watchdog %s] %s' % (time.strftime('%m-%d %H:%M:%S'), msg), flush=True)


def _pinned_version_dir():
    """当前锁定版本目录名 —— 以合法基线为准 (不能取磁盘最高版本号, 那可能是强更产物)."""
    baseline = sorted(d for d in _baseline_versions()
                      if os.path.isdir(os.path.join(APPS_BASE, d)))
    if baseline:
        return baseline[-1]
    # 基线目录全没了 (极端情况): 回退磁盘扫描
    try:
        vers = [d for d in os.listdir(APPS_BASE)
                if VERSION_RE.match(d) and os.path.isdir(os.path.join(APPS_BASE, d))]
    except OSError:
        return None
    return sorted(vers)[-1] if vers else None


# 看守启动时已存在的版本目录 = 合法基线. 之后冒出来的任何新版本目录都是强更产物,
# 一律隔离 (节点锁定单一版本是硬需求; 手动升级后需重跑 fix_update.bat 重建基线).
# 基线持久化在 .wd_baseline.json (否则看门狗重启会把强更残留也当合法);
# 可用 env JY_PIN_VERSION 显式钉死单一合法版本.
_BASELINE_VERS = None
BASELINE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '.wd_baseline.json')


def _baseline_versions():
    global _BASELINE_VERS
    if _BASELINE_VERS is not None:
        return _BASELINE_VERS
    import json
    pin = os.environ.get('JY_PIN_VERSION', '').strip()
    if pin:
        _BASELINE_VERS = {pin}
        _log('合法版本基线 (JY_PIN_VERSION 钉死): [%s]' % pin)
        return _BASELINE_VERS
    if os.path.isfile(BASELINE_FILE):
        try:
            with open(BASELINE_FILE, encoding='utf-8') as f:
                _BASELINE_VERS = set(json.load(f))
            _log('合法版本基线 (持久化): %s' % sorted(_BASELINE_VERS))
            return _BASELINE_VERS
        except Exception as e:
            _log('基线文件读不了 (%r), 用磁盘现状重建' % e)
    try:
        _BASELINE_VERS = set(d for d in os.listdir(APPS_BASE)
                             if VERSION_RE.match(d))
    except OSError:
        _BASELINE_VERS = set()
    try:
        with open(BASELINE_FILE, 'w', encoding='utf-8') as f:
            json.dump(sorted(_BASELINE_VERS), f)
    except OSError:
        pass
    _log('合法版本基线 (首次按磁盘现状建立): %s' % (sorted(_BASELINE_VERS) or '无'))
    if len(_BASELINE_VERS) > 1:
        _log('!! 警告: 磁盘上有 %d 个版本目录, 可能含强更残留且已被记为合法!'
             % len(_BASELINE_VERS))
        _log('!! 请人工确认后只保留目标版本, 再删掉 %s 重启看守' % BASELINE_FILE)
    return _BASELINE_VERS


def _detect(now):
    """返回 (级别, 描述). 级别: 'early'=仅杀更新器 (主程序会话保住), 'late'=全杀."""
    try:
        entries = os.listdir(APPS_BASE)
    except OSError as e:
        _log('扫不了 Apps 目录: %r' % e)
        return None
    baseline = _baseline_versions()
    for d in entries:
        if STAGING_RE.match(d):
            return 'late', '暂存目录 %s' % d
        if VERSION_RE.match(d) and d not in baseline and \
                os.path.isdir(os.path.join(APPS_BASE, d)):
            return 'late', '基线外新版本目录 %s' % d
    if 'JianyingPro.exe' in entries:  # 根目录启动器被复位 = 强更收尾动作
        return 'late', '根目录启动器被复位'
    # 新鲜落地的安装器: 安装器马上要被执行, 这是最早的拦截点
    try:
        ue = os.path.join(DOWNLOAD_DIR, 'update.exe')
        if os.path.isfile(ue) and now - os.path.getmtime(ue) < 120:
            return 'early', 'Download\\update.exe 刚下载 (%ds 前)' % int(
                now - os.path.getmtime(ue))
    except OSError:
        pass
    return None


def _kill_updaters_and_app():
    for name in ('JianyingPro.exe', 'update.exe', 'VEUpdateShell.exe'):
        r = subprocess.run(['taskkill', '/F', '/IM', name], capture_output=True, text=True)
        if r.returncode == 0:
            _log('已杀 %s' % name)


def _quarantine(path, name):
    dst = path + '.quarantine_wd'
    i = 2
    while os.path.exists(dst):
        dst = '%s.quarantine_wd%d' % (path, i)
        i += 1
    try:
        os.rename(path, dst)
        _log('已隔离 %s' % name)
    except OSError as e:
        _log('隔离 %s 失败: %r' % (name, e))


def _restore_meta(ver_dir):
    """把 packet.xml / Configure.ini 还原成锁定版本的值."""
    if not ver_dir:
        return
    parts = ver_dir.split('.')
    try:
        pkt = PACKET_XML.format(minor=parts[0], feature=parts[1],
                                full=ver_dir)
        open(os.path.join(APPS_BASE, 'JianyingProPacket.xml'), 'w',
             encoding='utf-8').write(pkt)
        open(os.path.join(APPS_BASE, 'Configure.ini'), 'w',
             encoding='utf-8').write('[jianyingpro]\r\nlast_version=%s\r\n' % ver_dir)
    except OSError as e:
        _log('还原配置失败: %r' % e)


def _reapply_acl():
    """重上 deny-create ACL (仅本层). 高完整性更新器会剥掉它, 但对普通权限组件仍有约束.

    注意必须用 win32 API 上裸掩码 0x2|0x4 —— icacls /deny 会把 SYNCHRONIZE 位也
    塞进掩码, 那会连目录枚举一起锁死 (find_jianying_exe 直接瞎掉). 没有 pywin32 就跳过.
    """
    try:
        import win32security
    except ImportError:
        return  # 宁可不上锁也别用 icacls 把枚举搞坏
    try:
        sd = win32security.GetFileSecurity(APPS_BASE, win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        i = dacl.GetAceCount() - 1
        while i >= 0:
            ace = dacl.GetAce(i)
            sid, typ = ace[2], ace[0][0]
            if typ == win32security.ACCESS_DENIED_ACE_TYPE and \
                    win32security.LookupAccountSid(None, sid)[0] == 'Everyone':
                dacl.DeleteAce(i)
            i -= 1
        everyone, _, _ = win32security.LookupAccountName(None, 'Everyone')
        # FILE_WRITE_DATA/ADD_FILE(0x2) | FILE_APPEND_DATA/ADD_SUBDIR(0x4), 无 SYNCHRONIZE
        dacl.AddAccessDeniedAce(win32security.ACL_REVISION, 0x2 | 0x4, everyone)
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(APPS_BASE, win32security.DACL_SECURITY_INFORMATION, sd)
        _log('已重上 deny-create ACL')
    except Exception as e:
        _log('重上 ACL 失败 (不影响看守): %r' % e)


def _remove_acl():
    """摘掉 deny-create ACE —— 否则我们自己的锁会挡住隔离改名 (rename 需要父目录写权)."""
    try:
        import win32security
        sd = win32security.GetFileSecurity(APPS_BASE, win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        i = dacl.GetAceCount() - 1
        n = 0
        while i >= 0:
            ace = dacl.GetAce(i)
            sid, typ = ace[2], ace[0][0]
            if typ == win32security.ACCESS_DENIED_ACE_TYPE and \
                    win32security.LookupAccountSid(None, sid)[0] == 'Everyone':
                dacl.DeleteAce(i)
                n += 1
            i -= 1
        if n:
            sd.SetSecurityDescriptorDacl(1, dacl, 0)
            win32security.SetFileSecurity(APPS_BASE,
                                          win32security.DACL_SECURITY_INFORMATION, sd)
        return
    except ImportError:
        pass
    # 无 pywin32 回退: icacls 摘除是安全的 (有毒的只是它的 /deny 添加语法)
    subprocess.run(['icacls', APPS_BASE, '/remove:d', 'Everyone'],
                   capture_output=True)


def intervene(level):
    """level='early': 只杀更新器进程, 保住主程序会话 (校准/渲染不被打断);
    level='late': 全杀 + 隔离 + 还原 (强更已开始落地)."""
    if level == 'late':
        _kill_updaters_and_app()
        time.sleep(1.5)  # 等句柄释放
    else:
        for name in ('update.exe', 'VEUpdateShell.exe'):
            r = subprocess.run(['taskkill', '/F', '/IM', name],
                               capture_output=True, text=True)
            if r.returncode == 0:
                _log('已杀 %s (主程序保留)' % name)
    _remove_acl()  # 先解锁再动目录, 干完由 _reapply_acl 重上
    ver_dir = _pinned_version_dir()
    baseline = _baseline_versions()
    try:
        entries = os.listdir(APPS_BASE)
    except OSError:
        return
    dirty = False
    for name in entries:
        p = os.path.join(APPS_BASE, name)
        if STAGING_RE.match(name):
            _quarantine(p, '暂存目录 ' + name)
            dirty = True
        elif name == 'JianyingPro.exe' and os.path.isfile(p):
            _quarantine(p, '根目录启动器')
            dirty = True
        elif (os.path.isdir(p) and VERSION_RE.match(name)
                and name not in baseline):
            # 基线外的新版本目录 = 强更产物
            _quarantine(p, '强更新版本目录 ' + name)
            dirty = True
        elif (os.path.isdir(p) and not VERSION_RE.match(name)
                and '.quarantine' not in name and not name.endswith('.bak')):
            # 未知形态目录也隔离
            _quarantine(p, '未知目录 ' + name)
            dirty = True
    if dirty:
        _log('Apps 目录已清理')
    # 配置可能被强更改写, 无条件还原 (幂等且开销可忽略); dirty 变量仅供日志语义
    _restore_meta(ver_dir)
    _reapply_acl()


# 标记放机器级固定位置 (Apps 的上级目录), 不跟模块走 —
# 同机多份副本时, 任一副本 render_driver 落的标记所有看守实例都能看到.
SESSION_MARKER = os.path.join(os.path.dirname(APPS_BASE), '.wd_session')
SESSION_TTL = 1800  # 标记新鲜度 (渲染/校准单会话上限远小于此)


def _session_active():
    """render_driver 落的会话标记 (30min 内) = 合法会话进行中, 暂停拦截.
    原理: 运行中的 5.9 锁住自己版本目录文件, 安装器写不进去; 会话结束后统一清理."""
    try:
        return time.time() - os.path.getmtime(SESSION_MARKER) < SESSION_TTL
    except OSError:
        return False


def _loop():
    _log('看守启动: %s (每 %.0fs 扫一次)' % (APPS_BASE, INTERVAL))
    cooldown_until = 0.0
    suppressed = False
    while True:
        now = time.time()
        try:
            if _session_active():
                if not suppressed:
                    _log('渲染/校准会话进行中, 暂停拦截 (标记 %s)' % SESSION_MARKER)
                    suppressed = True
                time.sleep(INTERVAL)
                continue
            if suppressed:
                _log('会话标记消失, 恢复拦截 (清理会话期间强更残留)')
                suppressed = False
                cooldown_until = 0.0  # 立即扫一轮, 清掉会话期间的残留
            if now >= cooldown_until:
                hit = _detect(now)
                if hit:
                    level, desc = hit
                    _log('!! 检测到强更动作 [%s]: %s — 开始拦截' % (level, desc))
                    intervene(level)
                    # early 短冷却持续压制更新器重生; late 长冷却越过 update.exe 新鲜窗口
                    cd = 20 if level == 'early' else COOLDOWN
                    cooldown_until = time.time() + cd
                    _log('干预完成, 冷却 %ds' % cd)
        except Exception as e:
            _log('err: %r' % e)
        time.sleep(INTERVAL)


def _acquire_single_instance():
    """命名互斥体防双看守 (standalone 与 render_service 内嵌同时跑会互相抢杀).
    拿到返回 True; 已有实例在跑返回 False."""
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, 'autocut_upgrade_watchdog')
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True  # 拿不到互斥体能力就放行 (宁多勿缺)


def start():
    """后台守护线程启动看守 (render_service 里调). 已有实例在跑则跳过."""
    if not _acquire_single_instance():
        print('[upgrade_watchdog] 已有看守实例在跑, 跳过', flush=True)
        return None
    t = threading.Thread(target=_loop, daemon=True, name='upgrade-watchdog')
    t.start()
    return t


if __name__ == '__main__':
    if not _acquire_single_instance():
        _log('已有看守实例在跑, 退出')
        raise SystemExit(0)
    _loop()
