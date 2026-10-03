#!/bin/sh
# 从 backups/ 里的一份备份恢复数据库。
#
# 为什么需要它：仓库原本只有 backup_db.sh —— 「有备份无恢复」等于没有备份。
# 这是本脚本存在的唯一理由。
#
# 用法：
#   ./restore.sh                        恢复 /data/backups 里最新的一份
#   ./restore.sh football-20261003.db.gz  指定文件名
#   ./restore.sh /abs/other.db.gz       指定完整路径
#   DB_PATH=/tmp/test.db ./restore.sh   指定恢复到哪里
#   YES=1 ./restore.sh                  跳过二次确认（自动化用）
#
# 安全设计（三条，缺一不可）：
#   1. 恢复前自动备份当前库 → 万一恢复错了还能回滚
#   2. 完整性校验（PRAGMA integrity_check）→ 坏备份不会被写进去
#   3. 二次确认 → 防止手滑覆盖线上库

set -eu

DB="${DB_PATH:-/data/football.db}"
BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
ARG="${1:-}"

if [ ! -d "$BACKUP_DIR" ]; then
    echo "[$(date '+%F %T')] 失败：备份目录不存在 ($BACKUP_DIR)" >&2
    exit 1
fi

# ---- 定位备份文件 ----
if [ -n "$ARG" ]; then
    if [ -f "$ARG" ]; then
        SRC="$ARG"
    elif [ -f "$BACKUP_DIR/$ARG" ]; then
        SRC="$BACKUP_DIR/$ARG"
    else
        echo "[$(date '+%F %T')] 失败：找不到备份 ($ARG)" >&2
        exit 1
    fi
else
    SRC="$(ls -1t "$BACKUP_DIR" 2>/dev/null | grep '^football-.*\.db\(\.gz\)\?$' | head -n 1 || true)"
    if [ -z "$SRC" ]; then
        echo "[$(date '+%F %T')] 失败：$BACKUP_DIR 里没有任何备份" >&2
        exit 1
    fi
    SRC="$BACKUP_DIR/$SRC"
fi

echo "[$(date '+%F %T')] 将用 $SRC 覆盖 $DB"

# ---- 二次确认 ----
if [ "${YES:-0}" != "1" ]; then
    printf '这会覆盖当前数据库，确定继续？输入 yes 回车：'
    read -r ans
    [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

# ---- 1. 先备份当前库（可回滚）----
if [ -f "$DB" ]; then
    # 加 PID：同一秒内连跑两次会撞文件名，VACUUM INTO 报
    # "output file already exists" 并中止恢复（实测复现）。
    PRE="$BACKUP_DIR/pre-restore-$(date +%Y%m%d-%H%M%S)-$$.db"
    python - <<PY
import sqlite3, sys
src = sqlite3.connect("$DB", timeout=30)
try:
    src.execute("VACUUM INTO ?", ("$PRE",))
except Exception as exc:
    print("恢复前备份失败（已中止恢复）：", exc, file=sys.stderr)
    sys.exit(1)
finally:
    src.close()
PY
    echo "[$(date '+%F %T')] 当前库已先备份到 $PRE"
fi

# ---- 解压到临时文件（支持 .gz）----
TMP="$(mktemp /tmp/restore-XXXXXX.db)"
case "$SRC" in
    *.gz)
        if command -v gzip >/dev/null 2>&1; then
            gzip -dc "$SRC" > "$TMP"
        else
            python -c "
import gzip, shutil, sys
with gzip.open('$SRC','rb') as f, open('$TMP','wb') as o:
    shutil.copyfileobj(f, o)
"
        fi
        ;;
    *) cp "$SRC" "$TMP" ;;
esac

# ---- 2. 完整性校验：坏备份绝不写入 ----
python - "$TMP" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1], timeout=30)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        ok = row and row[0] == "ok"
        n = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        con.close()
except sqlite3.DatabaseError as exc:
    # 备份文件根本不是 SQLite（截断/损坏/传错文件）也会走到这里，
    # 必须给出可读信息而不是抛 traceback。
    print(f"完整性校验失败：{exc}", file=sys.stderr)
    print("已中止恢复（原库未被改动）", file=sys.stderr)
    sys.exit(1)
if not ok:
    print("完整性校验失败，已中止恢复（原库未被改动）", file=sys.stderr)
    sys.exit(1)
print(f"完整性校验通过（{n} 张表）")
PY

# ---- 落库 ----
mkdir -p "$(dirname "$DB")"
mv "$TMP" "$DB"
# WAL/SHM 是旧库的，留着会与新库冲突
rm -f "$DB-wal" "$DB-shm"

echo "[$(date '+%F %T')] 恢复完成：$DB"
