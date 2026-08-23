@echo off
REM start_render_service.bat — Win10 渲染节点启动 (独立 render_service)
REM 用法: 双击或命令行运行。读 .env 里的 render 相关 env (JY_APP_BASE / JY_DRAFT_ROOT /
REM        VIDEOS_DIR / DESKTOP_NAMES / RENDER_SERVICE_HOST / RENDER_SERVICE_PORT /
REM        RENDER_SERVICE_TOKEN)。
REM 部署在 Win10 上, 与 Linux web 后端分离。web 后端通过 RENDER_SERVICE_URL 指向本机。
setlocal
set PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe
set WD=%~dp0
if not exist "%PY%" set PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe

REM 加载 .env (简单 KEY=VALUE 行; 含 = 的值取首个分隔)
if exist "%WD%.env" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%WD%.env") do (
        set "%%a=%%b"
    )
)

REM 重定向到日志: pythonw 无控制台, 继承的句柄在批处理退出后失效会导致 print flush 抛 OSError.
start "render_service" /D "%WD%" "%PY%" render_service.py > "%WD%render_service.log" 2>&1
echo render_service started (pythonw render_service.py)
echo 日志: render_service.log  (前台运行: python render_service.py)
endlocal
