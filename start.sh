#!/bin/sh
# 单容器同时跑「机器人」与「Web 看板」。
#
# 为什么不拆成两个 Railway 服务：
# Railway 的 Volume **无法跨服务共享**（官方明确：each volume is attached to
# exactly one service）。拆开后看板服务拿到的是一个全新的空卷，读不到机器人
# 写的 football.db，看板永远显示「暂无数据」。所以两者必须同容器，共用 /data。
set -eu

PORT="${PORT:-8000}"

# ENABLE_WEB=0 可关掉看板（默认开启）
if [ "${ENABLE_WEB:-1}" = "1" ]; then
    echo "[start] starting web dashboard on :${PORT}"
    # 后台运行；日志直接打到 stdout，Railway 会一并采集
    # 2>&1：uvicorn 的 INFO 日志默认写 stderr，Railway 会把 stderr 一律标 [err]，
    # 淹没真正的 ERROR。合并到 stdout 后日志分级才准确（真错误仍含 ERROR/Traceback）。
    python -m uvicorn api:app --host 0.0.0.0 --port "${PORT}" 2>&1 &
fi

# 机器人放前台：它挂了容器就退出，Railway 会自动重启（与原来的行为一致）
echo "[start] starting bot"
exec python main.py
