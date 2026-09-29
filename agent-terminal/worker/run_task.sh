#!/usr/bin/env bash
# 一次性沙盒任务执行器。
#
# 约束（不可越过）：
#   1. 源仓库与目标仓库写死在白名单里，禁止任何用户输入指定。
#   2. 只读克隆源仓库，不回写。
#   3. 产物只推到目标仓库的 sandbox/<RUN_ID> 临时分支，再开 PR，不直接合入主干。
#   4. Token 只从环境变量读取，不写入 URL、日志、提交记录或产物。
#   5. 只向 stdout 输出脱敏回执（status/commit/tests/secret_scan/pr_url）。
set -Eeuo pipefail

# ---- 白名单（写死） ----
readonly SRC_REPO="modouxin-dev/football-prediction-bot"
readonly DST_REPO="modouxin-dev/football-prediction-bot-staging"
readonly SRC_BRANCH="main"

readonly TASK="${1:-}"
readonly RUN_ID="${RUN_ID:-$(date -u +%Y%m%d%H%M%S)-$$}"
readonly WORKSPACE="${WORKSPACE:-/workspace}"

if [ -z "$TASK" ]; then
    echo "status=failed"
    echo "task=none"
    exit 1
fi

echo "task=${TASK}"
echo "run_id=${RUN_ID}"

# ---- 凭据检查（只检查存在性，不打印值） ----
if [ -z "${GH_TOKEN:-}" ]; then
    echo "status=failed"
    echo "message=GH_TOKEN 未注入"
    exit 1
fi

rm -rf "${WORKSPACE}"
mkdir -p "${WORKSPACE}/source" "${WORKSPACE}/output"

# ---- 只读克隆源仓库（用 Authorization 头，不把 Token 拼进 URL） ----
echo "[run] 克隆源仓库 ${SRC_REPO}"
git -c http.extraHeader="Authorization: Bearer ${GH_TOKEN}" \
    clone --depth=1 --branch "${SRC_BRANCH}" \
    "https://github.com/${SRC_REPO}.git" "${WORKSPACE}/source" \
    >/dev/null 2>&1

cd "${WORKSPACE}/source"
COMMIT="$(git rev-parse --short HEAD)"
echo "commit=${COMMIT}"

# ---- 固定 CI 流程 ----
echo "[run] 执行测试"
TEST_OUT="$(bash "${WORKSPACE}/source/agent-terminal/worker/ci/test.sh" 2>&1)" \
    || { echo "$TEST_OUT" | tail -3; echo "status=failed"; exit 1; }
echo "$TEST_OUT" | tail -2
TESTS="$(echo "$TEST_OUT" | grep -Eo '[0-9]+ passed' | tail -1 || echo '0 passed')"
echo "tests=${TESTS}"

echo "[run] 秘密扫描"
SCAN_OUT="$(bash "${WORKSPACE}/source/agent-terminal/worker/ci/secret-scan.sh" 2>&1)" \
    || { echo "$SCAN_OUT" | tail -3; echo "status=failed"; exit 1; }
echo "$SCAN_OUT" | tail -1
SCAN="$(echo "$SCAN_OUT" | grep -q 'clean' && echo 'clean' || echo 'dirty')"
echo "secret_scan=${SCAN}"

# ---- 打包产物（不含 .git，避免把源仓库历史带过去） ----
echo "[run] 打包产物"
git archive --format=tar HEAD | tar -x -C "${WORKSPACE}/output"

# ---- 推送到 staging 临时分支 ----
echo "[run] 推送到 ${DST_REPO} 临时分支"
cd "${WORKSPACE}/output"
git init -q
git config user.name "sandbox-bot"
git config user.email "sandbox-bot@example.invalid"
git add -A
git commit -q -m "sandbox: promote tested artifact (${COMMIT})"
git remote add origin "https://github.com/${DST_REPO}.git"
git -c http.extraHeader="Authorization: Bearer ${GH_TOKEN}" \
    push -q origin "HEAD:refs/heads/sandbox/${RUN_ID}" >/dev/null 2>&1

# ---- 创建 PR（人工审核后再合并） ----
echo "[run] 创建 PR"
PR_URL="$(curl -sS -X POST \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${DST_REPO}/pulls" \
    -d "{\"title\":\"sandbox: ${COMMIT}\",\"head\":\"sandbox/${RUN_ID}\",\"base\":\"main\",\"body\":\"自动化沙盒产物，待人工审核。\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("html_url",""))' 2>/dev/null || echo '')"

echo "pr_url=${PR_URL}"
echo "status=passed"
