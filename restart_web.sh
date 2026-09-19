#!/usr/bin/env bash
# restart_web.sh — 重启 web 后端 (先停干净再后台启动). 傻瓜用法: ./restart_web.sh
# 改完代码想生效就跑这个; 它自带等端口释放 + 启动等端口起来, 起失败会打日志给你看.
exec "$(cd "$(dirname "$0")" && pwd)/start_web.sh" restart
