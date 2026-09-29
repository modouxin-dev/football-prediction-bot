#!/bin/sh
# SQLite 热备份：把当前数据库安全复制一份到 backups/，保留最近 N 份。
#
# 为什么不用 cp：数据库开启了 WAL 模式，直接 cp 可能复制到不一致的中间状态。
# 用 sqlite3 的 .backup 命令是官方推荐的热备方式，可在服务运行时安全执行。
#
# 用法：
#   ./backup_db.sh                 备份到 /data/backups（容器内默认）
#   ./backup_db.sh /path/to/dir    指定备份目录
#   KEEP=30 ./backup_db.sh         保留最近 30 份（默认 14）
#
# 建议用 cron 每天跑一次。在宿主机上：
#   0 4 * * * cd /path/to/repo && ./backup_db.sh ./data/backups >> ./data/backup.log 2>&1

set -eu

KEEP="${KEEP:-14}"
DB="${DB_PATH:-/data/football.db}"
BACKUP_DIR="${1:-${BACKUP_DIR:-/data/backups}}"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [ ! -f "$DB" ]; then
    echo "[$(date '+%F %T')] 跳过：数据库不存在 ($DB)" >&2
    exit 0
fi

mkdir -p "$BACKUP_DIR"

# .backup 会正确处理 WAL，得到一致性快照
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$BACKUP_DIR/football-$STAMP.db'"
else
    # 容器里通常没装 sqlite3 CLI，退化为 VACUUM INTO（同样是一致性快照，且能整理碎片）
    python - <<PY
import sqlite3, sys
src = sqlite3.connect("$DB", timeout=30)
dst = "$BACKUP_DIR/football-$STAMP.db"
try:
    src.execute("VACUUM INTO ?", (dst,))
except Exception as exc:
    print("VACUUM INTO 失败：", exc, file=sys.stderr)
    sys.exit(1)
finally:
    src.close()
PY
fi

# 压缩，节省空间
if command -v gzip >/dev/null 2>&1; then
    gzip -f "$BACKUP_DIR/football-$STAMP.db"
    echo "[$(date '+%F %T')] 已备份: football-$STAMP.db.gz"
else
    echo "[$(date '+%F %T')] 已备份: football-$STAMP.db"
fi

# 只保留最近 KEEP 份，避免占满磁盘
ls -1t "$BACKUP_DIR" 2>/dev/null | grep '^football-.*\.db' | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$BACKUP_DIR/$old"
    echo "[$(date '+%F %T')] 已清理旧备份: $old"
done
