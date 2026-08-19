# restart_server.ps1 — 安全重启 render_server (端口取自 .env, 由 config 读取)
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File restart_server.ps1
$ErrorActionPreference = 'SilentlyContinue'
$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe'
$wd = 'C:\Users\Administrator\autocut'

# 端口从 .env 读 (没有则用 config 默认 9002)
$port = 9002
Get-Content (Join-Path $wd '.env') | ForEach-Object {
    if ($_ -match '^RENDER_SERVER_PORT=(\d+)') { $port = $Matches[1] }
}

# 杀掉占用该端口的【全部】进程. 关键: Windows 下 SO_REUSEADDR 允许多个进程同时 LISTEN
# 同一端口(端口劫持共存), 只杀第一个会留下幽灵旧进程 —— 请求随机落到旧代码上, 表现为
# "改了代码重启了却不生效". 必须枚举所有监听者逐个杀, 杀完确认端口真空.
$listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
foreach ($c in $listeners) {
    Write-Host "killing listener pid=$($c.OwningProcess) ($($c.LocalAddress):$port)"
    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$left = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($left) {
    # 端口没清干净就绝不再启动新进程 —— 否则又造出一个幽灵共存者, 请求随机分流到新旧进程.
    Write-Host "FAIL: port $port still occupied by $($left.OwningProcess -join ',') - abort restart"
    exit 1
}

Start-Process -FilePath $py -ArgumentList 'render_server.py' -WorkingDirectory $wd `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $wd 'server.log') `
    -RedirectStandardError (Join-Path $wd 'server.err.log')
Start-Sleep -Seconds 8
$n = (Get-NetTCPConnection -LocalPort $port -State Listen | Select-Object -First 1).OwningProcess
if ($n) { Write-Host "OK: render_server pid=$n listening on $port" }
else { Write-Host "FAIL: nothing listening on $port"; Get-Content (Join-Path $wd 'server.err.log') -Tail 10 }
