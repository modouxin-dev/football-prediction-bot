#!/bin/bash
# 单容器同时跑「机器人」与「Web 看板」。
#
# 为什么不拆成两个 Railway 服务：
# Railway 的 Volume **无法跨服务共享**（官方明确：each volume is attached to
# exactly one service）。拆开后看板服务拿到的是一个全新的空卷，读不到机器人
# 写的 football.db，看板永远显示「暂无数据」。所以两者必须同容器，共用 /data。
#
# 为什么必须是 bash（不能是 /bin/sh）：
# 下面用 `wait -n` 等待「任意一个」子进程退出。`wait -n` 是 bash 内建，
# Debian 的 /bin/sh 是 dash，不支持 -n，会直接报 `wait: -n: invalid option`
# 并让整个启动脚本失效 —— 表现就是容器起来了、看板却永远打不开。
# Dockerfile 已显式安装 bash 以保证它存在。
#
# 为什么要有守护逻辑（本文件最初没有）：
# 原实现把 uvicorn 用 `&` 丢到后台、机器人用 exec 放前台。uvicorn 崩溃时
# 容器不会退出、Railway 不会重启 —— 机器人照常推送，看板却一直打不开，
# 且没有任何告警。现在任一子进程退出就结束容器，交给 Railway 重启。
set -eu

PORT="${PORT:-8080}"
PIDS=()

shutdown() {
    # 收到 SIGTERM/SIGINT（Railway 停止或重新部署）时转发给子进程，
    # 让 uvicorn 与机器人有机会关闭连接、刷写 SQLite。
    echo "[start] received signal, shutting down" >&2
    if [ "${#PIDS[@]}" -gt 0 ]; then
        kill "${PIDS[@]}" 2>/dev/null || true
        wait 2>/dev/null || true
    fi
    exit 0
}
trap shutdown TERM INT

# ENABLE_WEB=0 可关掉看板（默认开启）
if [ "${ENABLE_WEB:-1}" = "1" ]; then
    echo "[start] starting web dashboard on :${PORT}"
    # 2>&1：uvicorn 的 INFO 日志默认写 stderr，Railway 会把 stderr 一律标 [err]，
    # 淹没真正的 ERROR。合并到 stdout 后日志分级才准确（真错误仍含 ERROR/Traceback）。
    python -m uvicorn api:app --host 0.0.0.0 --port "${PORT}" 2>&1 &
    PIDS+=("$!")
fi

echo "[start] starting bot"
python main.py &
PIDS+=("$!")

# 任一子进程退出即返回非零，容器随之退出，由 Railway 重新拉起。
# 退出码用 1：Railway 对非零退出会重启，对 0 视为正常结束不再拉起。
#
# 注意 `|| status=$?`：`wait -n` 返回的是那个已退出子进程的非零码，
# 在 `set -e` 下会当场终止本脚本，下面的清理与日志就永远不会执行。
# 放进 `||` 之后 set -e 不再介入，行为才符合预期。
status=0
wait -n "${PIDS[@]}" || status=$?
echo "[start] a process exited (code ${status}), stopping container" >&2
# 另一个进程可能还在跑（例如看板崩了、机器人仍活着）。先把它一起收掉，
# 避免留下孤儿进程继续读写 /data（SQLite 并发写入有风险）。
kill "${PIDS[@]}" 2>/dev/null || true
exit 1
