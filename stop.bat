@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "KILLED=0"
if exist "%~dp0serve.pid" (
  set /p PID=<"%~dp0serve.pid"
  if defined PID (
    for /f "tokens=* delims= " %%x in ("!PID!") do set "PID=%%x"
    taskkill /PID !PID! /F >nul 2>&1
    if !errorlevel!==0 (
      echo [done] stopped PID=!PID!
      set "KILLED=1"
    )
  )
  del "%~dp0serve.pid" >nul 2>&1
)
if "!KILLED!"=="0" (
  for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9002" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
    if !errorlevel!==0 (
      echo [done] stopped PID=%%a port 9002
      set "KILLED=1"
    )
  )
)
if "!KILLED!"=="0" (
  echo [info] render_server not running
) else (
  echo render_server stopped
)
ping 127.0.0.1 -n 3 >nul
endlocal
