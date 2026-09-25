#!/bin/sh
# Volume 挂载后 /data 的属主通常是 root，而容器以非 root 运行，会导致数据库无法写入。
# 启动时先把挂载点改归 bot 用户，再降权执行主程序。
set -e

USER_NAME="${APP_USER:-bot}"

mkdir -p /data
chown -R "$USER_NAME":"$USER_NAME" /data 2>/dev/null || true

mkdir -p /tmp/mplconfig
chown -R "$USER_NAME":"$USER_NAME" /tmp/mplconfig 2>/dev/null || true

exec gosu "$USER_NAME":"$USER_NAME" "$@"
