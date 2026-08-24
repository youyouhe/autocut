#!/usr/bin/env bash
# start_web.sh — Linux web 后端启动 (render_server:app, waitress)
#
# 部署在 Linux 服务器上。前端 (build 后的 static) 同源托管, render 转发到 Win10 的
# render_service (RENDER_SERVICE_URL 指向 Win10 IP:9020)。
#
# 用法:
#   ./start_web.sh            # 前台运行
#   ./start_web.sh --daemon   # 后台运行 (nohup)
#
# 依赖: pip install flask waitress requests werkzeug
# env (可放 .env 或 systemd Environment): 见 config.py (SECRET_KEY / INTERNAL_TOKEN /
#   RENDER_SERVICE_URL / RENDER_SERVICE_TOKEN / QWEN_API_KEY / ASR_API_KEY 等)
set -e
WD="$(cd "$(dirname "$0")" && pwd)"
cd "$WD"

# 加载 .env (KEY=VALUE 行)
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

HOST="${RENDER_SERVER_HOST:-0.0.0.0}"
PORT="${RENDER_SERVER_PORT:-9010}"

PY="${PYTHON:-python3}"
if [ "$1" = "--daemon" ]; then
    nohup "$PY" -m waitress --host="$HOST" --port="$PORT" --threads=16 \
        --channel-timeout=900 render_server:app \
        > web.log 2> web.err.log &
    echo "web backend started (daemon) on $HOST:$PORT, pid=$!"
else
    exec "$PY" -m waitress --host="$HOST" --port="$PORT" --threads=16 \
        --channel-timeout=900 render_server:app
fi
