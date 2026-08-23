@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYW="
for %%P in (pythonw.exe) do set "PYW=%%~$PATH:P"
if not defined PYW (
  if exist "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe" (
    set "PYW=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe"
  )
)
if not defined PYW (
  echo [error] pythonw.exe not found
  pause
  exit /b 1
)

netstat -ano | findstr ":9010" | findstr "LISTENING" >nul
if !errorlevel!==0 (
  echo [info] render_server already running on port 9010
  echo        visit http://localhost:9010
  ping 127.0.0.1 -n 3 >nul
  exit /b 0
)

echo [start] render_server background no window
REM 重定向 stdout/stderr 到日志文件: pythonw 无控制台, 若仅继承 serve.bat 的控制台句柄,
REM 本批处理退出后句柄失效, print(..., flush=True) 会抛 OSError [Errno 22] 导致 /api/video/serve 500.
start "" /B "!PYW!" "%~dp0render_server.py" > "%~dp0server.log" 2>&1

set /a tries=0
:wait
ping 127.0.0.1 -n 2 >nul
netstat -ano | findstr ":9010" | findstr "LISTENING" >nul
if !errorlevel!==0 goto ok
set /a tries+=1
if !tries! lss 15 goto wait
echo [warn] service may not have started, check server.log server.err
exit /b 1

:ok
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9010" ^| findstr "LISTENING"') do (
  echo %%a> "%~dp0serve.pid"
  goto havepid
)
:havepid
echo [done] render_server started
echo        visit http://localhost:9010
echo        stop: run stop.bat
ping 127.0.0.1 -n 3 >nul
endlocal
