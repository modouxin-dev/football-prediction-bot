"""队名埋点：收集「未收录、显示英文原名」的真实队名 / Unmatched team-name telemetry.

为什么需要它 / Why this exists
--------------------------------
静态收录表永远滞后于现实：升班马、新联赛、数据源改写法（缩写/裸名），
这些只有在**线上真正渲染到**的那一刻才会暴露。靠人工截图去发现，
永远是事后补救。这里让程序自己把没翻出来的原名攒下来，管理员
一条 /teammiss 就能拉到最新漏网名单，按真实语料补收录。

    A static name table always lags behind reality: promoted clubs, new
    leagues, and source-side spelling changes (abbreviations / bare names)
    only surface when they are actually rendered in production. Finding them
    by screenshot is always reactive. This module makes the program collect
    untranslated originals on its own, so an admin can pull the current
    miss list with a single /teammiss command and extend the table with
    real-world spellings.

为什么落盘而不是只放内存 / Why persist instead of memory-only
------------------------------------------------------------
start.sh 在**同一个容器里跑两个进程**：uvicorn（看板）后台 + main.py（机器人）
前台。内存里的 set 既跨不了进程，重启也会丢，等于白记。
因此这里写 SQLite（与主库同目录、随挂载卷保留），并用**内存 set 做一级缓存**：
同一个名字只在首次出现时写库，之后零开销——列表页一页几十个队名，
不能每次渲染都写一次磁盘。

    start.sh runs **two processes in one container**: uvicorn (dashboard) in
    background + main.py (bot) in foreground. An in-memory set neither crosses
    processes nor survives restarts, so it would collect nothing useful.
    Hence SQLite (same dir as main DB, kept by the mounted volume), with an
    in-memory set as a first-level cache: a name is written only on its first
    occurrence, afterwards zero cost — a list page renders dozens of team
    names, we cannot hit disk for every single render.

设计约束 / Design constraints
-----------------------------
1. 失败必须静默：埋点是诊断设施，绝不能因为写库失败而打断渲染主流程。
   Failures must be silent: this is diagnostics, it must never break rendering.
2. 只记录，不猜测：这里收集的是「没翻出来的原名」，补什么中文名由人决定。
   Record only, never guess: we collect untranslated originals; the Chinese
   name to add stays a human decision.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# 埋点库与主库同目录，随挂载卷一起保留 / Same dir as main DB, kept by the volume
_MISS_DB_NAME = "team_misses.db"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS team_name_misses (
    raw        TEXT PRIMARY KEY,   -- 数据源原名 / original name from source
    hits       INTEGER NOT NULL DEFAULT 0,  -- 出现次数 / occurrence count
    first_seen TEXT NOT NULL,      -- 首次出现 (ISO8601 UTC)
    last_seen  TEXT NOT NULL       -- 末次出现 (ISO8601 UTC)
)
"""

# 一级缓存：已见过的名字不再写库 / First-level cache: known names skip the write
_seen: set[str] = set()
_seen_lock = threading.Lock()

_db_path: Path | None = None
_db_path_lock = threading.Lock()
_ready = False


def _now_iso() -> str:
    """当前 UTC 时间（ISO8601 秒精度，去微秒以免字符串过长）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _resolve_db_path() -> Path:
    """解析埋点库路径：跟随 paths.DATA_DIR，与主库同级。

    为什么不用主库 football.db：埋点是诊断数据，塞进主库会改变主库结构，
    也让备份/回滚牵连到业务表。独立一个库，删掉文件就等于重置埋点。
    """
    global _db_path
    if _db_path is not None:
        return _db_path
    with _db_path_lock:
        if _db_path is not None:
            return _db_path
        try:
            import paths
            base = Path(paths.DATA_DIR)
        except Exception:
            base = Path("/tmp")
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = Path("/tmp")
        _db_path = base / _MISS_DB_NAME
        return _db_path


def set_db_path(path: str | Path) -> None:
    """测试或临时目录用：强制指定埋点库位置，并重置一级缓存。

    只应在测试里调用。生产路径由 _resolve_db_path 自动决定。
    """
    global _db_path, _ready
    with _db_path_lock:
        _db_path = Path(path)
        _ready = False
    with _seen_lock:
        _seen.clear()


def _connect() -> sqlite3.Connection:
    """建立连接并建表；WAL 让两个进程可同时写。"""
    conn = sqlite3.connect(str(_resolve_db_path()), timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.execute(_TABLE_SQL)
    return conn


def _ensure_ready() -> None:
    """首次写入前建好表。建表失败不致命，后续写入会自然静默失败。"""
    global _ready
    if _ready:
        return
    try:
        conn = _connect()
        conn.commit()
        conn.close()
        _ready = True
    except Exception as exc:
        log.debug("队名埋点库初始化失败（不影响主流程）：%s", exc)


def record(raw: str | None) -> None:
    """记录一个「未收录、只能显示英文原名」的队名。

    只做记录，不猜测中文名。空值/空白直接忽略。
    同一名字仅首次写库，之后靠内存 set 短路；写库失败一律静默。
    """
    name = (raw or "").strip()
    if not name:
        return
    with _seen_lock:
        if name in _seen:
            return
        _seen.add(name)
    _ensure_ready()
    try:
        now = _now_iso()
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO team_name_misses (raw, hits, first_seen, last_seen)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(raw) DO UPDATE SET
                    hits = hits + 1,
                    last_seen = excluded.last_seen
                """,
                (name, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # 埋点失败绝不影响渲染 / telemetry failure must never break rendering
        log.debug("队名埋点写入失败（已忽略）：%s", exc)


def recent(limit: int = 50) -> list[tuple[str, int, str]]:
    """按末次出现倒序返回漏网队名：(原名, 次数, 末次时间)。"""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT raw, hits, last_seen FROM team_name_misses
                ORDER BY last_seen DESC, rowid DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
        return [(r[0], int(r[1]), str(r[2])) for r in rows]
    except Exception as exc:
        log.debug("队名埋点读取失败：%s", exc)
        return []


def count() -> int:
    """去重后的漏网队名总数。"""
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM team_name_misses").fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def clear() -> None:
    """清空埋点库与内存缓存（仅供测试/管理员重置）。"""
    global _ready
    with _seen_lock:
        _seen.clear()
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM team_name_misses")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    _ready = True
