#!/usr/bin/env bash
# 固定测试流程：安装依赖并执行 pytest。
# 只输出「通过/失败 + 用例计数」，不打印环境变量、Token 或绝对路径。
set -Eeuo pipefail

WORKDIR="${WORKDIR:-/workspace/source}"
cd "$WORKDIR"

echo "[test] 安装依赖"
python3 -m pip install --quiet --disable-pip-version-check \
    -r requirements.txt -r requirements-dev.txt 2>&1 \
    | grep -vE "WARNING: Running pip as the 'root' user|incompatible" || true

echo "[test] 执行 pytest"
# 捕获输出，只回传最后一行统计，避免把内部路径/敏感值带出去
set +e
OUTPUT="$(python3 -m pytest -q 2>&1)"
STATUS=$?
set -e

echo "$OUTPUT" | tail -n 1

if [ "$STATUS" -ne 0 ]; then
    echo "[test] 结果：失败"
    exit "$STATUS"
fi

echo "[test] 结果：通过"
