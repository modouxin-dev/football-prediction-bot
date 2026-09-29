#!/bin/sh
set -e

USER_NAME="${APP_USER:-bot}"
DATA_ROOT="/data"
MPL_CONFIG="/tmp/mplconfig"

echo "[Entrypoint] Starting environment audit..."
mkdir -p "$DATA_ROOT" "$MPL_CONFIG"

echo "[Entrypoint] Fixing permissions for $USER_NAME..."
chown -R "$USER_NAME":"$USER_NAME" "$DATA_ROOT" "$MPL_CONFIG"

for dir in cache charts exports backups; do
    mkdir -p "$DATA_ROOT/$dir"
    chown "$USER_NAME":"$USER_NAME" "$DATA_ROOT/$dir"
done

# 可写性探测必须用 gosu：镜像里没有 sudo，用 sudo 会固定返回 127
# （"sudo: not found"），被误判成「不可写」，每次启动都打印一条假的 CRITICAL。
# gosu 是镜像自带的降权工具，最后一步启动应用用的也是它。
if ! gosu "$USER_NAME":"$USER_NAME" touch "$DATA_ROOT/.mount_test"; then
    echo "[CRITICAL] $DATA_ROOT is NOT writable by $USER_NAME. Data will be lost on reboot!"
fi
rm -f "$DATA_ROOT/.mount_test"

echo "[Entrypoint] Audit complete. Handing over to application..."
exec gosu "$USER_NAME":"$USER_NAME" "$@"
