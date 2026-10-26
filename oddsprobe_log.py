"""盘口探测落盘：把 /odds 的真实盘口结构存下来 / Odds probe persistence.

为什么需要它 / Why this exists
--------------------------------
亚洲盘在数据源码表里的 bet id 与选项写法**必须实测确认**，凭记忆猜 id
会导致解析永远取不到值——而取不到值是静默的：不报错、不告警，只是推荐里
没有亚盘，看不出是「没做」还是「没取到」。

但「把探测结果发回 Telegram」这条路不可靠：

- 一场比赛的盘口全列出来有几十个 bet，拼成单条远超 Telegram 4096 上限
  （实测抛 ``BadRequest: Message is too long``，异常发生在最后一步，
  前面取数全白做，用户侧只看到「机器人没回复」）。
- 分片能解决长度，却解决不了「用户不确定到底跑没跑」。

因此这里把探测结果**先落盘**，让它不依赖任何一条消息是否发送成功：
管理员打开 ``/oddsprobe`` 端点即可拿到完整结构，命令本身只回一条精简摘要。

    The odds probe result is persisted first, so it never depends on whether
    any single Telegram message got delivered.

为什么落盘而不是只放内存 / Why persist
--------------------------------------
start.sh 在**同一个容器里跑两个进程**：uvicorn（看板）后台 + main.py（机器人）
前台。内存既跨不了进程，重启也会丢——而探测结果恰恰要跨进程读：
机器人在 Telegram 里跑命令，管理员在网页上看结果。
这与队名埋点（teammiss）是同一个约束，故沿用同样的写法。

设计约束 / Design constraints
-----------------------------
1. 失败必须静默：这是诊断设施，绝不能因为写库失败而打断探测主流程。
2. 只记录，不猜测：原样保存数据源说了什么，bet id 的含义由人判读。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DB_NAME = "odds_probe.db"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS odds_probe (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id  TEXT NOT NULL,      -- 探测的比赛 ID
    bet_id      TEXT NOT NULL,      -- 盘口 id，如 "4"
    bet_name    TEXT NOT NULL,      -- 盘口名，如 "Asian Handicap"
    n_values    INTEGER NOT NULL DEFAULT 0,
    values_json TEXT,               -- 选项原始 JSON（前若干个）
    seen_at     TEXT NOT NULL       -- 探测时间 (ISO8601 UTC)
)
"""

_RUN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS odds_probe_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id  TEXT NOT NULL,
    ok          INTEGER NOT NULL,        -- 1=取到盘口 0=没取到
    n_bets      INTEGER NOT NULL DEFAULT 0,
    message     TEXT,                    -- 失败原因简述
    seen_at     TEXT NOT NULL
)
"""

_db_path: Path | None = None
_lock = threading.Lock()
_ready = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _resolve_db_path() -> Path:
    global _db_path
    if _db_path is not None:
        return _db_path
    with _lock:
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
        _db_path = base / _DB_NAME
        return _db_path


def set_db_path(path: str | Path) -> None:
    """测试用：强制指定库位置并重置建表标记。"""
    global _db_path, _ready
    with _lock:
        _db_path = Path(path)
        _ready = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_resolve_db_path()), timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.execute(_TABLE_SQL)
    conn.execute(_RUN_TABLE_SQL)
    return conn


def _ensure_ready() -> None:
    global _ready
    if _ready:
        return
    try:
        conn = _connect()
        conn.commit()
        conn.close()
        _ready = True
    except Exception as exc:
        log.debug("盘口探测库初始化失败（不影响主流程）：%s", exc)


def record_bets(fixture_id, bets) -> int:
    """落盘一场比赛的盘口结构。返回写入条数；失败静默返回 0。

    Args:
        fixture_id: 比赛 ID
        bets: [{"id":..., "name":..., "values":[...]}, ...]
    """
    _ensure_ready()
    rows = []
    for b in bets or []:
        vals = b.get("values") or []
        try:
            values_json = json.dumps(vals[:12], ensure_ascii=False)
        except Exception:
            values_json = None
        rows.append((str(fixture_id), str(b.get("id") or ""),
                     str(b.get("name") or "").strip(), len(vals),
                     values_json, _now_iso()))
    if not rows:
        return 0
    try:
        conn = _connect()
        try:
            conn.executemany(
                """INSERT INTO odds_probe
                   (fixture_id, bet_id, bet_name, n_values, values_json, seen_at)
                   VALUES (?,?,?,?,?,?)""", rows)
            conn.commit()
        finally:
            conn.close()
        return len(rows)
    except Exception as exc:
        log.debug("盘口落盘失败（已忽略）：%s", exc)
        return 0


def record_run(fixture_id, ok: bool, n_bets: int = 0, message: str = "") -> None:
    """记录一次探测是否取到数据——用于区分「没跑」与「跑了但没盘口」。"""
    _ensure_ready()
    try:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO odds_probe_runs
                   (fixture_id, ok, n_bets, message, seen_at)
                   VALUES (?,?,?,?,?)""",
                (str(fixture_id), 1 if ok else 0, int(n_bets),
                 (message or "")[:300], _now_iso()))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("探测记录写入失败（已忽略）：%s", exc)


def latest_bets(limit: int = 200) -> list[dict]:
    """返回最近一次探测到的盘口结构（按 bet_id 排序）。"""
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT fixture_id FROM odds_probe ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return []
            fid = row[0]
            rows = conn.execute(
                """SELECT bet_id, bet_name, n_values, values_json, seen_at
                   FROM odds_probe WHERE fixture_id = ?
                   ORDER BY CAST(bet_id AS INTEGER), bet_id LIMIT ?""",
                (fid, int(limit))).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("读取盘口探测结果失败：%s", exc)
        return []
    out = []
    for bid, bname, n, vjson, seen in rows:
        try:
            vals = json.loads(vjson) if vjson else []
        except Exception:
            vals = []
        out.append({"id": bid, "name": bname, "n_values": int(n),
                    "values": vals, "seen_at": seen})
    return out


def latest_runs(limit: int = 10) -> list[dict]:
    """最近几次探测的成败记录。"""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT fixture_id, ok, n_bets, message, seen_at
                   FROM odds_probe_runs ORDER BY id DESC LIMIT ?""",
                (int(limit),)).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("读取探测记录失败：%s", exc)
        return []
    return [{"fixture_id": r[0], "ok": bool(r[1]), "n_bets": int(r[2]),
             "message": r[3], "seen_at": r[4]} for r in rows]


def latest_fixture_id() -> str | None:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT fixture_id FROM odds_probe ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


def clear() -> None:
    """清空探测库（仅供测试/重置）。"""
    global _ready
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM odds_probe")
            conn.execute("DELETE FROM odds_probe_runs")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    _ready = True
