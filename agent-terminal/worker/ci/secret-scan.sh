#!/usr/bin/env bash
# 秘密扫描：检查工作区是否混入凭据。
# 命中即失败（exit 1），阻止产物被推送到 staging 仓库。
set -Eeuo pipefail

WORKDIR="${WORKDIR:-/workspace/source}"
cd "$WORKDIR"

# 命中模式（正则）
PATTERNS=(
    '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'          # Telegram Bot Token
    'gh[pousr]_[A-Za-z0-9]{20,}'                 # GitHub 细粒度/PAT
    '-----BEGIN [A-Z ]*PRIVATE KEY-----'         # 私钥
    'xox[baprs]-[A-Za-z0-9-]{10,}'               # Slack Token
    'AKIA[0-9A-Z]{16}'                           # AWS Access Key
)

# 扫描范围：工作区内所有文本文件，排除 .git 与本扫描脚本自身
FILES="$(find . -path ./.git -prune -o -type f -print 2>/dev/null || true)"

HITS=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    # 跳过二进制
    file "$f" 2>/dev/null | grep -q 'text' || continue
    for pat in "${PATTERNS[@]}"; do
        if grep -Eq "$pat" "$f" 2>/dev/null; then
            # 只报告文件名与命中模式编号，绝不回显命中内容本身
            echo "[secret-scan] 命中：$f"
            HITS=$((HITS + 1))
            break
        fi
    done
done <<< "$FILES"

if [ "$HITS" -gt 0 ]; then
    echo "[secret-scan] 结果：发现 ${HITS} 处疑似凭据，禁止推送"
    exit 1
fi

echo "[secret-scan] 结果：clean"
