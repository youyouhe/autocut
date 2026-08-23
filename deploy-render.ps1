# deploy-render.ps1 — Sync render-node files from source dir to the portable node package
# Usage (from source dir, e.g. C:\Users\Administrator\autocut):
#   powershell -ExecutionPolicy Bypass -File deploy-render.ps1
#
# IMPORTANT: C:\autocut-render-node is a PORTABLE package (built by portable/build.py):
#   - bundled python\ + ffmpeg\, service code runs from the app\ subdirectory
#   - start.bat launches app\render_service.py with the bundled python
#   => code files must be deployed to C:\autocut-render-node\app  (NOT the package root!)
#   => .env is generated from config.env by start.bat — never touch it here

$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$dst = 'C:\autocut-render-node\app'

if (-not (Test-Path $dst)) {
    Write-Error "Target $dst not found. Is this really the portable render node? (expected app\ subdirectory)"
    exit 1
}

$files = @(
    'render_service.py',
    'render_driver.py',
    'render_monitor.py',
    'upgrade_watchdog.py',
    'config.py',
    'task_store.py'
)

foreach ($f in $files) {
    $s = Join-Path $src $f
    if (-not (Test-Path $s)) { Write-Warning "MISSING in source: $f (skipped)"; continue }
    Copy-Item $s -Destination $dst -Force
    Write-Host "UPDATED  app\$f"
}

# Calibration: sync latest if present (root of package, next to calibrate.bat)
$calib = Join-Path $src 'calib.json'
if (Test-Path $calib) {
    Copy-Item $calib -Destination 'C:\autocut-render-node\calib.json' -Force
    Write-Host 'UPDATED  calib.json'
} else {
    Write-Host 'SKIPPED  calib.json (not found in source)'
}

Write-Host ''
Write-Host 'NOTE: files previously copied to the package ROOT by an older version of this'
Write-Host '      script are unused (service runs from app\). You may delete them manually.'
Write-Host ''
Write-Host 'DEPLOY DONE. Now restart the render service:'
Write-Host '  1. Run stop.bat  (or close the render_service console window)'
Write-Host '  2. Run start.bat'
Write-Host '  3. Health check: http://127.0.0.1:9020/health'
