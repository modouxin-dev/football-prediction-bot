"""预测结果持久化 / Prediction repository.

机器人在云端运行，容器重启后内存数据会丢失，因此把预测落盘到 SQLite 单文件，
即「挂到机器人自己的存储」：

- 零新增依赖（sqlite3 属于 Python 标准库）
- 路径由 DB_PATH 指定（默认 /data/predictions.db，Railway 持久化卷通常挂这里）
- 目录不可写时自动回退到内存数据库，机器人功能不受影响

落盘后可支撑：
- 比赛结束后回写真实比分
- 统计命中率、连胜、各信心等级命中率
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/data/predictions.db"  # Railway 持久化卷通常挂在这里

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    fixture_id   TEXT PRIMARY KEY,
    season       INTEGER,
    league       TEXT,
    home         TEXT,
    away         TEXT,
    kickoff      TEXT,
    model_version TEXT,
    source       TEXT,
    level_key    TEXT,
    result       TEXT,
    home_prob    REAL,
    draw_prob    REAL,
    away_prob    REAL,
    best_score   TEXT,
    created_at   TEXT,
    inputs       TEXT,
    actual_home  INTEGER,
    actual_away  INTEGER,
    settled_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_predictions_kickoff ON predictions(kickoff);
"""

_local = threading.local()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PredictionRepository:
    """SQLite 预测仓库：落盘预测、回写赛果、统计命中率。

    线程安全：每个线程持有独立连接（sqlite3 连接不能跨线程共用）。
    写入失败只记日志，绝不中断机器人主流程。
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self._memory = False
        self._init_schema()

    # ---- 连接 ---------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # 连接按路径缓存：同一线程内多个仓库实例不能共用一条连接
        conns = getattr(_local, "conns", None)
        if conns is None:
            conns = {}
            _local.conns = conns
        cached = conns.get(self.db_path)
        if cached is not None:
            return cached
        try:
            if self.db_path != ":memory:":
                path = Path(self.db_path)
                if path.parent and not path.parent.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
        except Exception as exc:  # 目录不可写 → 回退内存库
            log.warning("无法打开数据库 %s（%s），回退内存存储", self.db_path, exc)
            self._memory = True
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
        conns[self.db_path] = conn
        return conn

    def _init_schema(self) -> None:
        try:
            conn = self._connect()
            conn.executescript(SCHEMA)
            conn.commit()
        except Exception as exc:  # 建表失败也不能拖垮启动
            log.warning("初始化数据库失败：%s", exc)

    @property
    def persistent(self) -> bool:
        """是否真正落盘（False 表示当前是内存回退，重启会丢）。"""
        return not self._memory

    # ---- 写入 ---------------------------------------------------------------
    def save(self, prediction) -> None:
        """保存一条预测。重复保存同场比赛则更新（以最新模型结果为准）。"""
        try:
            a = prediction.analysis
            best_score = a.get("best_score")
            level = getattr(prediction, "level", None) or {}
            conn = self._connect()
            conn.execute(
                """INSERT INTO predictions
                   (fixture_id, season, league, home, away, kickoff, model_version,
                    source, level_key, result, home_prob, draw_prob, away_prob,
                    best_score, created_at, inputs)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fixture_id) DO UPDATE SET
                     season=excluded.season, kickoff=excluded.kickoff,
                     model_version=excluded.model_version, source=excluded.source,
                     level_key=excluded.level_key, result=excluded.result,
                     home_prob=excluded.home_prob, draw_prob=excluded.draw_prob,
                     away_prob=excluded.away_prob, best_score=excluded.best_score,
                     created_at=excluded.created_at, inputs=excluded.inputs""",
                (
                    str(prediction.fixture_id),
                    int(prediction.season or 0),
                    prediction.league,
                    prediction.home,
                    prediction.away,
                    prediction.kickoff.isoformat() if prediction.kickoff else None,
                    prediction.model_version,
                    getattr(prediction, "source", "") or "",
                    level.get("key") if isinstance(level, dict) else None,
                    level.get("result") if isinstance(level, dict) else None,
                    float(a.get("win_prob", 0)),
                    float(a.get("draw_prob", 0)),
                    float(a.get("loss_prob", 0)),
                    best_score,
                    prediction.created_at.isoformat(),
                    json.dumps(getattr(prediction, "inputs", {}), ensure_ascii=False),
                ),
            )
            conn.commit()
        except Exception as exc:  # 落盘失败不影响本次预测返回
            log.warning("保存预测失败（fixture=%s）：%s", prediction.fixture_id, exc)

    def settle(self, fixture_id, home_score: int | None, away_score: int | None) -> bool:
        """回写真实比分。返回是否有记录被更新。"""
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE predictions SET actual_home=?, actual_away=?, settled_at=? "
                "WHERE fixture_id=?",
                (home_score, away_score, _now_iso(), str(fixture_id)),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as exc:
            log.warning("回写赛果失败（fixture=%s）：%s", fixture_id, exc)
            return False

    def pending(self, limit: int = 200) -> list[sqlite3.Row]:
        """尚未回写赛果的已开赛比赛，供定时任务同步结果。"""
        try:
            conn = self._connect()
            return list(conn.execute(
                "SELECT * FROM predictions WHERE actual_home IS NULL "
                "AND kickoff IS NOT NULL AND kickoff <= ? ORDER BY kickoff LIMIT ?",
                (_now_iso(), limit),
            ))
        except Exception as exc:
            log.warning("查询待结算预测失败：%s", exc)
            return []

    # ---- 统计 ---------------------------------------------------------------
    def stats(self) -> dict:
        """命中率统计：总命中、连胜/连败、各信心等级命中率。"""
        try:
            conn = self._connect()
            rows = list(conn.execute(
                "SELECT level_key, result, actual_home, actual_away, kickoff "
                "FROM predictions WHERE actual_home IS NOT NULL AND actual_away IS NOT NULL "
                "ORDER BY kickoff"
            ))
        except Exception as exc:
            log.warning("统计命中率失败：%s", exc)
            return {"total": 0, "hit": 0, "rate": None, "streak": 0, "by_level": {},
                    "pending": 0, "persistent": self.persistent}

        total = hit = 0
        streak = 0
        by_level: dict[str, dict] = {}
        for r in rows:
            actual = _outcome(r["actual_home"], r["actual_away"])
            if actual is None:
                continue
            total += 1
            ok = (r["result"] == actual)
            if ok:
                hit += 1
                streak = streak + 1 if streak >= 0 else 1
            else:
                streak = -1 if streak >= 0 else streak - 1
            lv = r["level_key"] or "unknown"
            slot = by_level.setdefault(lv, {"total": 0, "hit": 0})
            slot["total"] += 1
            slot["hit"] += 1 if ok else 0

        try:
            pending = conn.execute(
                "SELECT COUNT(*) c FROM predictions WHERE actual_home IS NULL"
            ).fetchone()["c"]
        except Exception:
            pending = 0

        return {
            "total": total,
            "hit": hit,
            "rate": (hit / total) if total else None,
            "streak": streak,
            "by_level": by_level,
            "pending": pending,
            "persistent": self.persistent,
        }

    def recent(self, limit: int = 10) -> list[sqlite3.Row]:
        try:
            conn = self._connect()
            return list(conn.execute(
                "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?", (limit,)
            ))
        except Exception as exc:
            log.warning("查询最近预测失败：%s", exc)
            return []


def _outcome(home: int | None, away: int | None) -> str | None:
    """真实比分 → 胜平负标签（与模型 result 字段同一套口径）。"""
    if home is None or away is None:
        return None
    if home > away:
        return "主胜"
    if home < away:
        return "客胜"
    return "平局"
