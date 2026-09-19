#!/usr/bin/env bash
# stop_web.sh — 停止 web 后端. 傻瓜用法: ./stop_web.sh
# (转发到 start_web.sh 的同一套逻辑, 优先按 .web.pid, 兜底按端口找进程, 会等端口真正释放)
exec "$(cd "$(dirname "$0")" && pwd)/start_web.sh" stop
