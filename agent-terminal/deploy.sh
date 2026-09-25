#!/usr/bin/env bash
# 一键部署：按固定顺序完成沙盒构建、任务执行与 webhook 注册。
# 只校验凭据「是否存在」，绝不打印其值。
set -Eeuo pipefail

cd "$(dirname "$0")"

echo "[deploy] 1/6 校验环境变量（只检查存在性，不打印）"
missing=0
[ -z "${GH_TOKEN:-}" ] && { echo "  ✗ GH_TOKEN 未设置"; missing=1; } || echo "  ✓ GH_TOKEN 已配置"
[ -z "${TG_ALLOWED_CHAT_IDS:-}" ] && { echo "  ✗ TG_ALLOWED_CHAT_IDS 未设置"; missing=1; } || echo "  ✓ TG_ALLOWED_CHAT_IDS 已配置"
if [ -n "${TELEGRAM_TOKEN:-}" ]; then echo "  ✓ TELEGRAM_TOKEN 已配置"; else echo "  - TELEGRAM_TOKEN 未设置（跳过 webhook 注册）"; fi
[ "$missing" -eq 1 ] && { echo "[deploy] 中止：缺少必需变量"; exit 1; }

echo "[deploy] 2/6 语法自检"
bash -n worker/run_task.sh && echo "  ✓ run_task.sh"
bash -n worker/ci/test.sh && echo "  ✓ test.sh"
bash -n worker/ci/secret-scan.sh && echo "  ✓ secret-scan.sh"
python3 -c "import ast,sys; ast.parse(open('gateway/app.py').read())" && echo "  ✓ gateway/app.py"

echo "[deploy] 3/6 构建沙盒镜像"
docker compose build worker

echo "[deploy] 4/6 执行固定任务 /test（一次性容器，结束即销毁）"
docker compose run --rm worker run_tests

echo "[deploy] 5/6 注册 Telegram Webhook（HTTPS）"
if [ -n "${TELEGRAM_TOKEN:-}" ] && [ -n "${WEBHOOK_URL:-}" ]; then
    curl -sS -X POST \
        "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
        -d "url=${WEBHOOK_URL}" \
        -d "allowed_updates=message" \
        | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  webhook:", d.get("description", d))'
else
    echo "  - 未提供 TELEGRAM_TOKEN 或 WEBHOOK_URL，跳过"
fi

echo "[deploy] 6/6 完成"
echo "  后续：由沙盒推送 sandbox/<RUN_ID> 分支并开 PR，人工审核后再合并。"
