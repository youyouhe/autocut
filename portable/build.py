#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py - Win10 免安装渲染节点打包脚本

在 Win10 本机 (autocut 仓库根目录) 运行:
    cd C:\\Users\\Administrator\\autocut
    python portable\\build.py

产出: portable\\autocut-render-node-portable.zip

做什么:
  1. 下载 Python 3.12.8 embeddable (带缓存) -> python/
  2. patch python312._pth 启用 site-packages
  3. 装 pip (get-pip.py)
  4. pip install 依赖到 embeddable site-packages (frida/flask/waitress/requests/dotenv)
  5. 下载 ffmpeg 静态构建 (带缓存) -> ffmpeg/
  6. 拷 6 个项目脚本 -> app/
  7. 拷模板 (config.env / start.bat / calibrate.bat / stop.bat / fix_update.bat / start_here.html / README.md)
  8. 校验剪映官方安装器 -> capcut-installer/ (缺失则中止)
  9. import 体检 (frida/flask/waitress/requests/dotenv)
 10. 打包 zip
 11. 清理 _build/

全程幂等: portable/_cache/ 缓存下载物, 可重复跑。

注意: 本脚本本身用系统 Python 跑, 它下载的 embeddable Python 是给客户机用的。
frida 是平台特定二进制 wheel, 必须在 Win/amd64 上运行本脚本才能拉到正确的 _frida.pyd。
"""

import os
import sys
import shutil
import zipfile
import subprocess
import urllib.request

# ============================================================
# 配置常量 (集中在此, 便于换源)
# ============================================================

# 路径
HERE = os.path.dirname(os.path.abspath(__file__))            # portable/
REPO_ROOT = os.path.dirname(HERE)                            # autocut/
ASSETS = os.path.join(HERE, 'assets')
CACHE = os.path.join(HERE, '_cache')
BUILD = os.path.join(HERE, '_build')
DIST_NAME = 'autocut-render-node'
DIST = os.path.join(BUILD, DIST_NAME)
OUT_ZIP = os.path.join(HERE, 'autocut-render-node-portable.zip')

# 下载源
PY_EMBED_URL = 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip'
GET_PIP_URL = 'https://bootstrap.pypa.io/get-pip.py'
FFMPEG_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

# 依赖 (render 节点需要的全部第三方包; 版本钉死保证可复现)
# frida 是二进制 wheel, 必须本机是 win-amd64 才能拉到 _frida.pyd
DEPS = [
    'frida==17.17.0',
    'flask==3.1.2',
    'flask-cors==6.0.2',
    'waitress==3.0.2',
    'requests==2.32.5',
    'python-dotenv==1.1.0',
    # upgrade_watchdog 的摘/上 deny ACL 用 (没有也能跑, 回退 icacls 摘除 + 跳过重上)
    'pywin32==311',
]

# 要拷进 app/ 的项目脚本 (从仓库根)
APP_SCRIPTS = [
    'render_service.py',
    'render_driver.py',
    'render_monitor.py',
    'hook_focus.js',
    'config.py',
    'task_store.py',
    'upgrade_watchdog.py',
]


# ============================================================
# 小工具
# ============================================================

def log(msg):
    print('[build] ' + msg, flush=True)


def die(msg):
    log('错误: ' + msg)
    sys.exit(1)


def download(url, dest):
    """下载 url 到 dest (覆盖). 带简单进度提示."""
    log('下载 %s' % url)
    tmp = dest + '.part'
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, dest)
    log('  -> %s (%.1f MB)' % (dest, os.path.getsize(dest) / 1048576.0))


def cached_download(url, filename):
    """缓存下载: CACHE/filename 存在则直接返回路径, 否则下载."""
    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, filename)
    if os.path.exists(dest):
        log('缓存命中: %s' % filename)
        return dest
    download(url, dest)
    return dest


def safe_zip_extract(zip_path, dest_dir):
    """解压 zip 到 dest_dir (同名文件覆盖)."""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)


# ============================================================
# 步骤
# ============================================================

def step_prep():
    """准备干净的 _build/autocut-render-node/ 目录."""
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(DIST)
    log('工作目录: %s' % DIST)


def step_python():
    """下载 Python embeddable, 解压到 python/, patch _pth 启用 site."""
    py_dir = os.path.join(DIST, 'python')
    z = cached_download(PY_EMBED_URL, 'python-3.12.8-embed-amd64.zip')
    log('解压 Python embeddable -> python/')
    safe_zip_extract(z, py_dir)

    # patch python312._pth: 取消 import site 注释, 加 site-packages
    pth = os.path.join(py_dir, 'python312._pth')
    patch = os.path.join(ASSETS, 'python312._pth.patch')
    with open(patch, 'r', encoding='utf-8') as f:
        patch_content = f.read()
    with open(pth, 'w', encoding='utf-8') as f:
        f.write(patch_content)
    log('patch python312._pth (启用 site-packages)')

    # 装 pip
    get_pip = cached_download(GET_PIP_URL, 'get-pip.py')
    py_exe = os.path.join(py_dir, 'python.exe')
    log('安装 pip (get-pip.py)')
    r = subprocess.run([py_exe, get_pip, '--no-warn-script-location'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(r.stdout)
        log(r.stderr)
        die('get-pip.py 失败')
    log('pip 安装完成')
    return py_exe, py_dir


def step_deps(py_exe, py_dir):
    """pip install 依赖到 embeddable 的 site-packages."""
    sp = os.path.join(py_dir, 'Lib', 'site-packages')
    os.makedirs(sp, exist_ok=True)
    log('pip install %d 个依赖 -> %s' % (len(DEPS), os.path.relpath(sp, DIST)))
    cmd = [py_exe, '-m', 'pip', 'install',
           '--no-warn-script-location',
           '--target=' + sp] + DEPS
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(r.stdout[-2000:])
        log(r.stderr[-2000:])
        die('pip install 失败 (检查网络 / 是否在 win-amd64 上跑以拉 frida 二进制 wheel)')
    # frida 的二进制 _frida.pyd 装在 frida/ 包目录下 (frida\_frida.pyd), 不是 site-packages 顶层
    frida_pyd = os.path.join(sp, 'frida', '_frida.pyd')
    if os.path.exists(frida_pyd):
        log('  frida\\_frida.pyd %.1f MB (二进制 wheel 到位)' % (os.path.getsize(frida_pyd) / 1048576.0))
    else:
        die('frida 安装了但找不到 frida\\_frida.pyd — 平台不匹配? 必须在 win-amd64 上跑本脚本')

    # 清掉 pip 装的 console 入口脚本 (bin/), embeddable 部署用不到, 纯属体积
    bin_dir = os.path.join(sp, 'bin')
    if os.path.isdir(bin_dir):
        shutil.rmtree(bin_dir)
    # 清掉 pip 自身包 (embeddable 不需要, 省 ~5MB). 保留 .dist-info:
    # flask/flask-cors/waitress 等在 import 时用 importlib.metadata 读版本号,
    # 删掉 dist-info 会导致 importlib.metadata.PackageNotFoundError.
    pip_pkg = os.path.join(sp, 'pip')
    if os.path.isdir(pip_pkg):
        shutil.rmtree(pip_pkg)
    for d in list(os.listdir(sp)):
        full = os.path.join(sp, d)
        if os.path.isdir(full) and d.lower() == 'pip-26.2.1.dist-info':
            shutil.rmtree(full)
    # 清 __pycache__ 减体积 (不影响 import, 首次 import 自动重建)
    for root, dirs, _ in os.walk(sp, topdown=False):
        if '__pycache__' in dirs:
            shutil.rmtree(os.path.join(root, '__pycache__'))
    log('依赖安装完成 (已清 bin/ pip/ __pycache__)')


def step_ffmpeg():
    """下载 ffmpeg 静态构建, 解出 bin/ffmpeg.exe + ffprobe.exe -> ffmpeg/."""
    ff_dir = os.path.join(DIST, 'ffmpeg')
    os.makedirs(ff_dir, exist_ok=True)
    if os.path.exists(os.path.join(ff_dir, 'ffmpeg.exe')) and \
       os.path.exists(os.path.join(ff_dir, 'ffprobe.exe')):
        log('ffmpeg/ffprobe 已存在, 跳过')
        return
    z = cached_download(FFMPEG_URL, 'ffmpeg-release-essentials.zip')
    tmp_extract = os.path.join(CACHE, '_ffmpeg_extract')
    if os.path.exists(tmp_extract):
        shutil.rmtree(tmp_extract)
    log('解压 ffmpeg (取 bin/ 下两个 exe)')
    safe_zip_extract(z, tmp_extract)
    # 找 bin/ffmpeg.exe (gyan.dev 的包根目录名含版本号, bin 在下一层)
    found_ff = None
    found_fp = None
    for root, dirs, files in os.walk(tmp_extract):
        for fn in files:
            low = fn.lower()
            full = os.path.join(root, fn)
            if low == 'ffmpeg.exe' and not found_ff:
                found_ff = full
            elif low == 'ffprobe.exe' and not found_fp:
                found_fp = full
    if not found_ff or not found_fp:
        die('ffmpeg zip 里找不到 ffmpeg.exe / ffprobe.exe (下载源结构变了?)')
    shutil.copy2(found_ff, os.path.join(ff_dir, 'ffmpeg.exe'))
    shutil.copy2(found_fp, os.path.join(ff_dir, 'ffprobe.exe'))
    shutil.rmtree(tmp_extract)
    log('ffmpeg/ffprobe 就位')


def step_app_scripts():
    """拷 6 个项目脚本 -> app/."""
    app_dir = os.path.join(DIST, 'app')
    os.makedirs(app_dir, exist_ok=True)
    for s in APP_SCRIPTS:
        src = os.path.join(REPO_ROOT, s)
        if not os.path.exists(src):
            die('找不到项目脚本: %s (在仓库根 %s)' % (s, REPO_ROOT))
        shutil.copy2(src, os.path.join(app_dir, s))
    log('拷入 %d 个项目脚本 -> app/' % len(APP_SCRIPTS))


def step_templates():
    """拷模板: config.env / start.bat / calibrate.bat / stop.bat / fix_update.bat / start_here.html / README.md."""
    # config.env
    shutil.copy2(os.path.join(ASSETS, 'config.env.template'),
                 os.path.join(DIST, 'config.env'))
    # .bat (template -> 实名). 模板是 UTF-8; 构建时转成 GBK 并去掉 chcp 65001 行:
    # UTF-8 中文 .bat + chcp 65001 在 Win10 上有 cmd 解析偏移 bug (行首被截半个多字节字,
    # 报 "'在' is not recognized" 之类, 间歇性). 目标机均为简体中文 Win10 (ANSI=CP936),
    # GBK 编码 + 默认代码页最稳.
    for name in ('start', 'calibrate', 'stop', 'fix_update', 'open_jianying'):
        with open(os.path.join(ASSETS, name + '.bat.template'),
                  'r', encoding='utf-8') as f:
            txt = f.read()
        lines = [ln for ln in txt.splitlines()
                 if ln.strip().lower() != 'chcp 65001 >nul']
        out = '\r\n'.join(lines) + '\r\n'
        with open(os.path.join(DIST, name + '.bat'), 'w',
                  encoding='gbk', newline='') as f:
            f.write(out)
    # html + readme 直拷
    shutil.copy2(os.path.join(ASSETS, 'start_here.html'),
                 os.path.join(DIST, 'start_here.html'))
    shutil.copy2(os.path.join(ASSETS, 'README.md'),
                 os.path.join(DIST, 'README.md'))
    log('拷入模板 (config.env / start.bat / calibrate.bat / stop.bat / fix_update.bat / start_here.html / README.md) [.bat 转GBK+去chcp]')


def step_capcut_installer():
    """校验剪映官方安装器, 拷入 capcut-installer/.

    匹配规则放宽: 文件名含 jianying / 剪映 / capcut 之一即可 (官方安装器
    中文名「剪映专业版5.9.exe」不含 'jianying' 英文串).
    """
    src_dir = os.path.join(HERE, 'capcut-installer')
    installers = []
    if os.path.isdir(src_dir):
        for f in os.listdir(src_dir):
            low = f.lower()
            if low.endswith('.exe') and ('jianying' in low or '剪映' in f or 'capcut' in low):
                installers.append(f)
    if not installers:
        die('capcut-installer/ 里找不到剪映安装器 (JianyingPro*.exe 或 剪映专业版*.exe)。\\n'
            '  请到 https://www.jianying.com/ 下载 Windows 版安装包,\\n'
            '  放到 portable/capcut-installer/ 后重新运行本脚本。\\n'
            '  不产出缺剪映的包。')
    dst_dir = os.path.join(DIST, 'capcut-installer')
    os.makedirs(dst_dir, exist_ok=True)
    for f in installers:
        shutil.copy2(os.path.join(src_dir, f), os.path.join(dst_dir, f))
        log('拷入剪映安装器: %s (%.0f MB)' % (f, os.path.getsize(os.path.join(src_dir, f)) / 1048576.0))


def step_smoke(py_exe):
    """import 体检: 用 embeddable python 验证 render 栈依赖可 import (不 import render_service, 避免真连剪映)."""
    log('import 体检...')
    code = (
        "import frida, flask, flask_cors, waitress, requests, dotenv; "
        "import json, zipfile, shutil, subprocess, threading, queue, uuid; "
        "print('smoke ok: frida', frida.__version__, '| flask', flask.__version__)"
    )
    r = subprocess.run([py_exe, '-c', code], capture_output=True, text=True,
                       cwd=os.path.join(DIST, 'app'))
    if r.returncode != 0:
        log(r.stdout)
        log(r.stderr)
        die('import 体检失败 — 依赖没装全')
    log('  ' + r.stdout.strip())


def step_zip():
    """打包 _build/autocut-render-node/ -> autocut-render-node-portable.zip."""
    log('打包 zip...')
    if os.path.exists(OUT_ZIP):
        os.remove(OUT_ZIP)
    # zipfromdir 风格: 顶层目录为 autocut-render-node/
    with zipfile.ZipFile(OUT_ZIP, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(DIST):
            for fn in files:
                full = os.path.join(root, fn)
                # arcname 相对 _build/, 即 autocut-render-node/...
                arc = os.path.relpath(full, BUILD)
                # zip 用正斜杠
                arc = arc.replace(os.sep, '/')
                zf.write(full, arc)
    size_mb = os.path.getsize(OUT_ZIP) / 1048576.0
    log('产出: %s (%.0f MB)' % (os.path.relpath(OUT_ZIP, REPO_ROOT), size_mb))


def step_cleanup():
    """清理 _build/."""
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    log('清理 _build/')


# ============================================================
# main
# ============================================================

def main():
    log('=== Win10 免安装渲染节点打包 ===')
    log('仓库根: %s' % REPO_ROOT)
    log('产出目录: %s' % DIST)

    step_prep()
    py_exe, py_dir = step_python()
    step_deps(py_exe, py_dir)
    step_ffmpeg()
    step_app_scripts()
    step_templates()
    step_capcut_installer()
    step_smoke(py_exe)
    step_zip()
    step_cleanup()

    log('=== 完成 ===')
    log('把 %s 发给客户: 解压 -> 双击 start_here.html -> 按 7 步操作。' %
        os.path.relpath(OUT_ZIP, REPO_ROOT))


if __name__ == '__main__':
    main()
