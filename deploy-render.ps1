# deploy-render.ps1 — Sync render-node files from source dir to C:\autocut-render-node
# Usage (from source dir, e.g. C:\Users\Administrator\autocut):
#   powershell -ExecutionPolicy Bypass -File deploy-render.ps1
#
# Minimal file set required by render_service:
#   render_service.py / render_driver.py / render_monitor.py / upgrade_watchdog.py
#   config.py / task_store.py  (directly imported by render_service)
#   requirements.txt           (for first-time dependency install)
#   .env                       (render-side config: JY_APP_BASE / DESKTOP_NAMES / RENDER_SERVICE_TOKEN;
#                               NOT overwritten if target already has one)
#   calib.json                 (coordinate calibration; overwritten with latest if present)

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
    if (-not (Test-Path $s)) { Write-Warning "MISSING in source: $f (skipped)"; continue }
    Copy-Item $s -Destination $dst -Force
    Write-Host "UPDATED  $f"
}

# .env: keep existing target config (render-machine specific); copy only on first deploy
$dstEnv = Join-Path $dst '.env'
if (-not (Test-Path $dstEnv)) {
    Copy-Item (Join-Path $src '.env') -Destination $dstEnv -ErrorAction SilentlyContinue
    if ($?) { Write-Host 'COPIED  .env (first deploy)' }
} else {
    Write-Host 'KEPT    .env (target config preserved, not overwritten)'
}

# Calibration: sync latest if present
$calib = Join-Path $src 'calib.json'
if (Test-Path $calib) {
    Copy-Item $calib -Destination $dst -Force
    Write-Host 'UPDATED  calib.json'
} else {
    Write-Host 'SKIPPED  calib.json (not found in source)'
}

Write-Host ''
Write-Host 'DEPLOY DONE. Now restart the render service:'
Write-Host '  1. Kill pythonw / render_service process in Task Manager'
Write-Host '  2. Run start_render_service.bat (or: pythonw render_service.py in C:\autocut-render-node)'
