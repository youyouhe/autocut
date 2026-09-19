#!/usr/bin/env bash
# start_web.sh — Linux web 后端一键启停 (render_server:app, waitress)
#
# 傻瓜用法 (在 autocut 目录下):
#   ./start_web.sh            # 启动(后台守护). 已在跑就直接提示, 绝不重复启第二份
#   ./start_web.sh stop       # 停止
#   ./start_web.sh restart    # 重启 = 先停干净再启动
#   ./start_web.sh status     # 看是否在跑 / pid / 端口
#   ./start_web.sh run        # 前台运行(调试用, 日志打屏上; Ctrl+C 停)
#
# 兼容旧写法: ./start_web.sh --daemon  ≡ 默认启动. (旧: 无参=前台, 现在无参=后台守护, 前台请用 run)
#
# 它自己会处理掉最容易翻车的几件事:
#   - 找对 python: 优先用本仓库 .venv, 不拿系统裸 python3 (缺 waitress 起不来)
#   - 幂等: 已在监听就不重复启, 避免搞出双份进程
#   - 写 .web.pid, 停止时先按 pid 优雅 kill, 兜底按端口找进程杀, 并等端口真正释放
#   - 起失败(如 capcut_server 导入错)会把日志尾部打给你看, 不会干瞪眼
#
# 日志: web.log / web.err.log ; 部署/依赖/env 见 config.py 与 AGENTS.md
set -u
WD="$(cd "$(dirname "$0")" && pwd)"
cd "$WD"

# ---- 加载 .env (KEY=VALUE 行), 拿端口/主机等 ----
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi
HOST="${RENDER_SERVER_HOST:-0.0.0.0}"
PORT="${RENDER_SERVER_PORT:-9010}"
THREADS="${RENDER_SERVER_THREADS:-16}"
PY="${PYTHON:-}"
PIDFILE="$WD/.web.pid"
LOG="$WD/web.log"
ERRLOG="$WD/web.err.log"
WAITRESS_ARGS=(--host="$HOST" --port="$PORT" --threads="$THREADS" \
               --channel-timeout=900 render_server:app)

usage() {
    sed -n '2,16p' "$0"
    exit "${1:-0}"
}

# ---- python 解析: 仓库 .venv 优先 ----
pick_python() {
    [ -n "$PY" ] && { echo "$PY"; return; }
    [ -x "$WD/.venv/bin/python" ] && { echo "$WD/.venv/bin/python"; return; }
    [ -x "$WD/.venv/Scripts/python.exe" ] && { echo "$WD/.venv/Scripts/python.exe"; return; }
    echo "python3"
}
PY="$(pick_python)"

# ---- 端口上是否有监听 / 是谁的 pid / 是不是我们的 render_server ----
listener_pid() {
    ss -ltnp 2>/dev/null | awk -v p=":$PORT" '
        $4 ~ (p "$") {
            if (match($0, /pid=[0-9]+/)) { print substr($0, RSTART + 4, RLENGTH - 4); exit }
        }'
}
pidfile_pid() { [ -f "$PIDFILE" ] && cat "$PIDFILE" 2>/dev/null; }
alive()      { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
is_ours() { # 只认命令行里带 render_server:app 的进程, 别误杀别人的
    [ -r "/proc/$1/cmdline" ] || return 1
    case "$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)" in *render_server:app*) return 0;; esac
    return 1
}

cmd_status() {
    local pid pf
    pid="$(listener_pid)"
    pf="$(pidfile_pid)"
    if [ -n "$pid" ]; then
        echo "运行中: pid=$pid  监听 $HOST:$PORT"
        echo "  本机访问: http://127.0.0.1:$PORT/"
        echo "  实时日志: tail -f $LOG"
        return 0
    fi
    if [ -n "$pf" ] && alive "$pf"; then
        echo "运行中(pidfile 命中, 端口暂未探到): pid=$pf"
        return 0
    fi
    echo "未运行 ($HOST:$PORT 无监听)"
    [ -n "$pf" ] && echo "  (清理陈旧 pidfile: rm -f $PIDFILE)"
    return 1
}

start_server() {
    local pid i
    pid="$(listener_pid)"
    if [ -n "$pid" ]; then
        if is_ours "$pid"; then
            echo "[web] 已在运行 pid=$pid ($HOST:$PORT), 不重复启动."
            return 0
        fi
        echo "[web] 端口 $PORT 被别的进程占着(pid=$pid), 不敢重复启. 请先查: ss -ltnp | grep :$PORT"
        return 1
    fi
    # 陈旧 pidfile(进程已死)顺手清掉
    pid="$(pidfile_pid)"
    if [ -n "$pid" ] && ! alive "$pid"; then rm -f "$PIDFILE"; fi

    if ! "$PY" -c "import waitress" 2>/dev/null; then
        echo "[web] 用的 python 缺 waitress: $PY"
        echo "      请先装依赖: $WD/.venv/bin/pip install -r $WD/requirements.txt   (若 .venv 在别处就装到那)"
        return 1
    fi

    echo "[web] 启动中 (python=$PY, $HOST:$PORT) ..."
    nohup "$PY" -m waitress "${WAITRESS_ARGS[@]}" >"$LOG" 2>"$ERRLOG" &
    echo $! >"$PIDFILE"

    for i in $(seq 1 30); do
        pid="$(listener_pid)"
        if [ -n "$pid" ]; then
            echo "[web] 启动完成 pid=$pid"
            echo "      本机访问: http://127.0.0.1:$PORT/   局域网: http://<本机IP>:$PORT/"
            echo "      日志: $LOG (错误: $ERRLOG)"
            return 0
        fi
        alive "$(cat "$PIDFILE" 2>/dev/null)" || break   # 进程死了 = 导入错之类, 提前报
        sleep 1
    done
    echo "[web] 启动失败: 30 秒内 $PORT 没监听上. 最近日志:"
    echo "--- $ERRLOG ---";  tail -n 15 "$ERRLOG" 2>/dev/null
    echo "--- $LOG ---";     tail -n 15 "$LOG" 2>/dev/null
    return 1
}

stop_server() {
    local pid pf target i
    pid="$(listener_pid)"
    pf="$(pidfile_pid)"
    if [ -z "$pid" ] && { [ -z "$pf" ] || ! alive "$pf"; }; then
        echo "[web] 本来就没在运行."
        rm -f "$PIDFILE"
        return 0
    fi
    if [ -n "$pid" ] && ! is_ours "$pid"; then
        echo "[web] 端口 $PORT 的占用进程(pid=$pid)不是本项目 render_server, 为避免误杀请人工处理: ss -ltnp | grep :$PORT"
        return 1
    fi
    target="${pid:-$pf}"
    echo "[web] 停止 pid=$target ..."
    kill "$target" 2>/dev/null
    for i in $(seq 1 15); do
        if [ -z "$(listener_pid)" ] && ! alive "$target"; then
            echo "[web] 已停止, 端口 $PORT 已释放."
            rm -f "$PIDFILE"
            return 0
        fi
        sleep 1
    done
    echo "[web] 优雅停止超时, 强制 kill pid=$target"
    kill -9 "$target" 2>/dev/null
    sleep 1
    if [ -n "$(listener_pid)" ]; then
        echo "[web] 警告: 端口 $PORT 仍未释放, 请人工查: ss -ltnp | grep :$PORT"
        return 1
    fi
    rm -f "$PIDFILE"
    echo "[web] 已强制停止, 端口 $PORT 已释放."
    return 0
}

run_foreground() {
    if pid="$(listener_pid)" && [ -n "$pid" ]; then
        echo "[web] 已在运行 pid=$pid, 前台模式不重复启. 想重启用: $0 restart"
        return 1
    fi
    echo "[web] 前台运行 (Ctrl+C 停). python=$PY $HOST:$PORT"
    exec "$PY" -m waitress "${WAITRESS_ARGS[@]}"
}

case "${1:-}" in
    ""|start|up|--daemon)   start_server ;;
    stop|down)              stop_server ;;
    restart)                stop_server && start_server ;;
    status)                 cmd_status ;;
    run|fg|foreground)      run_foreground ;;
    -h|--help|help)         usage 0 ;;
    *)                      echo "[web] 未知命令: $1"; usage 1 ;;
esac
