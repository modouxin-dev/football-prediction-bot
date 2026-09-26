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

if ! sudo -u "$USER_NAME" touch "$DATA_ROOT/.mount_test"; then
    echo "[CRITICAL] $DATA_ROOT is NOT writable by $USER_NAME. Data will be lost on reboot!"
fi
rm -f "$DATA_ROOT/.mount_test"

echo "[Entrypoint] Audit complete. Handing over to application..."
exec gosu "$USER_NAME":"$USER_NAME" "$@"
