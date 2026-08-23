# deploy-render.ps1 — 把源码目录的渲染节点所需文件同步到 C:\autocut-render-node
# 用法: 在源码目录 (C:\Users\Administrator\autocut) 下执行
#   powershell -ExecutionPolicy Bypass -File deploy-render.ps1
#
# 渲染节点 (render_service) 运行所需的最小文件集:
#   render_service.py / render_driver.py / render_monitor.py / upgrade_watchdog.py
#   config.py / task_store.py  (render_service 直接 import)
#   requirements.txt           (首次部署装依赖用)
#   .env                       (渲染侧配置: JY_APP_BASE / DESKTOP_NAMES / RENDER_SERVICE_TOKEN 等;
#                               目标目录已有 .env 时不覆盖 —— 那边是渲染机专属配置)
#   calib.json                 (坐标校准; 存在才拷, 目标已有则覆盖为最新)

$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$dst = 'C:\autocut-render-node'

if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Path $dst | Out-Null }

$files = @(
    'render_service.py',
    'render_driver.py',
    'render_monitor.py',
    'upgrade_watchdog.py',
    'config.py',
    'task_store.py',
    'requirements.txt'
)

foreach ($f in $files) {
    $s = Join-Path $src $f
    if (-not (Test-Path $s)) { Write-Warning "源目录缺少 $f, 跳过"; continue }
    Copy-Item $s -Destination $dst -Force
    Write-Host "已更新 $f"
}

# .env: 目标已有则保留 (渲染机专属), 没有才带一份模板过去
$dstEnv = Join-Path $dst '.env'
if (-not (Test-Path $dstEnv)) {
    Copy-Item (Join-Path $src '.env') -Destination $dstEnv -ErrorAction SilentlyContinue
    if ($?) { Write-Host '已复制 .env (首次)' }
} else {
    Write-Host '.env 目标已存在, 保留渲染机现有配置 (不覆盖)'
}

# 校准文件: 存在才同步 (每次覆盖 — 校准重新做过就该用新的)
$calib = Join-Path $src 'calib.json'
if (Test-Path $calib) {
    Copy-Item $calib -Destination $dst -Force
    Write-Host '已更新 calib.json'
} else {
    Write-Host '源目录无 calib.json (未做过校准?), 跳过'
}

Write-Host ''
Write-Host '同步完成. 请重启渲染服务:'
Write-Host '  任务管理器结束 pythonw/render_service 进程, 然后运行 start_render_service.bat'
Write-Host '  (或在 C:\autocut-render-node 下: pythonw render_service.py)'
