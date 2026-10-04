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
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import random
import time

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = None  # 由 paths.DB_PATH 统一决定（默认 /data/football.db）

# 写锁重试：云环境（Railway Volume）下 SQLite 并发写冲突远高于本地
DEFAULT_WRITE_RETRIES = 3        # 取锁失败后的重试次数
BACKOFF_BASE_SECONDS = 0.02      # 退避基数：20ms → 40ms → 80ms
BACKOFF_MAX_SECONDS = 0.5        # 单次退避上限，避免长时间阻塞机器人
CONNECTION_BUSY_TIMEOUT_MS = 15000   # 普通查询的等待上限（与既有行为一致）
TRANSACTION_BUSY_TIMEOUT_MS = 100    # 原子事务内的等待上限：快速失败 + 退避接管

# result 字段的合法取值：存的是「预测方向」，命中率按 result == 实际赛果判定。
# 历史版本曾写入 'pending'/NULL，这类值永远不等于任何赛果，会把命中率压成 0。
VALID_RESULTS = ("主胜", "平局", "客胜")


def _restore_busy_timeout(conn: sqlite3.Connection) -> None:
    """事务结束后把 busy_timeout 还原为连接建立时的值。"""
    try:
        conn.execute(f"PRAGMA busy_timeout={CONNECTION_BUSY_TIMEOUT_MS}")
    except Exception:
        pass


def _sleep_backoff(attempt: int, reason: str = "") -> None:
    """指数退避 + 随机抖动。抖动用于打散多个任务的同时重试（惊群）。"""
    delay = min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_MAX_SECONDS)
    delay *= 0.5 + random.random() * 0.5  # 50%~100% 抖动
    log.warning("SQLite 写锁冲突（%s），第 %d 次退避 %.0fms 后重试",
                reason or "locked", attempt + 1, delay * 1000)
    time.sleep(delay)

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id               INTEGER PRIMARY KEY,
    competition_code TEXT NOT NULL,
    season           INTEGER,
    utc_date         TEXT NOT NULL,
    status           TEXT,
    matchday         INTEGER,
    home_team_id     TEXT,
    home_team_name   TEXT,
    away_team_id     TEXT,
    away_team_name   TEXT,
    home_score       INTEGER,
    away_score       INTEGER,
    home_sot         INTEGER,
    away_sot         INTEGER,
    stats_fetched    INTEGER DEFAULT 0,
    source           TEXT NOT NULL,
    raw_json         TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE(competition_code, utc_date, home_team_id, away_team_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(utc_date);
CREATE INDEX IF NOT EXISTS idx_matches_comp_date ON matches(competition_code, utc_date);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    competition TEXT,
    date_from   TEXT,
    date_to     TEXT,
    http_status TEXT,
    received    INTEGER,
    saved       INTEGER,
    message     TEXT,
    created_at  TEXT NOT NULL
);

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

-- Elo 评分：按联赛隔离，team_id 为主键的一部分（同一队在不同联赛各有一套分）
CREATE TABLE IF NOT EXISTS elo_ratings (
    team_id      TEXT NOT NULL,
    competition  TEXT NOT NULL,
    rating       REAL NOT NULL,
    matches      INTEGER NOT NULL DEFAULT 0,
    last_match   TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (competition, team_id)
);

-- Elo 幂等标记：已计入评分的比赛不再重复计算
CREATE TABLE IF NOT EXISTS elo_processed (
    fixture_id   TEXT PRIMARY KEY,
    competition  TEXT,
    season       INTEGER,
    home_team_id TEXT,
    away_team_id TEXT,
    home_score   INTEGER,
    away_score   INTEGER,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_elo_processed_comp ON elo_processed(competition, season);

-- Elo 变更留痕：便于排查「评分是否被重复计算」
CREATE TABLE IF NOT EXISTS elo_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id   TEXT NOT NULL,
    team_id      TEXT NOT NULL,
    competition  TEXT NOT NULL,
    before       REAL NOT NULL,
    after        REAL NOT NULL,
    delta        REAL NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_elo_log_fixture ON elo_log(fixture_id);
CREATE TABLE IF NOT EXISTS api_quota (
    day      TEXT NOT NULL,
    source   TEXT NOT NULL,
    n        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
);
"""

QUOTA_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_quota (
    day      TEXT NOT NULL,
    source   TEXT NOT NULL,
    n        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
);
"""

_local = threading.local()


def _quota_day() -> str:
    """按机器人时区（默认 Asia/Shanghai）取当日日期。

    免费层额度按自然日重置，所以计数也按「机器人所在时区的自然日」分桶，
    避免 UTC 与本地时区错位导致跨天统计串味。
    """
    try:
        from zoneinfo import ZoneInfo
        import os
        tz = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Shanghai"))
    except Exception:  # 容器缺 tzdata 时退化为固定 +8，不影响计数可用性
        from datetime import timedelta
        tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d")


def bump_quota(source: str, n: int = 1) -> None:
    """记录一次「实际发出」的请求。

    只在真实网络请求发生时调用（主源失败、切备用源都各算一次），
    因此数字就是免费层额度的真实消耗量。计数失败绝不影响主流程。
    """
    try:
        import paths
        db_path = str(paths.DB_PATH)
    except Exception:
        return
    try:
        day = _quota_day()
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            conn.executescript(QUOTA_SCHEMA)
            conn.execute(
                "INSERT INTO api_quota(day, source, n) VALUES(?,?,?) "
                "ON CONFLICT(day, source) DO UPDATE SET n = n + excluded.n",
                (day, source, int(n)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 计数是旁路功能，失败只记日志
        log.debug("配额计数失败（不影响主流程）：%s", exc)


def quota_today() -> dict[str, int]:
    """返回今日各数据源的实际请求次数；读不到返回空字典。"""
    try:
        import paths
        db_path = str(paths.DB_PATH)
        day = _quota_day()
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            conn.executescript(QUOTA_SCHEMA)
            rows = conn.execute(
                "SELECT source, n FROM api_quota WHERE day = ?", (day,)
            ).fetchall()
        finally:
            conn.close()
        return {src: int(n) for src, n in rows}
    except Exception as exc:  # noqa: BLE001
        log.debug("读取配额计数失败：%s", exc)
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PredictionRepository:
    """SQLite 预测仓库：落盘预测、回写赛果、统计命中率。

    线程安全：每个线程持有独立连接（sqlite3 连接不能跨线程共用）。
    写入失败只记日志，绝不中断机器人主流程。
    """

    def __init__(self, db_path: str | None = None) -> None:
        import paths

        self.db_path = db_path or DEFAULT_DB_PATH or str(paths.DB_PATH)
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
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            # WAL：读写不互相阻塞，降低多进程/定时任务同时写入时的锁冲突
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=15000")
            except Exception:
                pass
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
        self._ensure_match_stat_columns()

    # 已存在的旧库不会被 CREATE TABLE IF NOT EXISTS 补列，
    # 挂载卷上的库是跨部署保留的，必须显式 ALTER 才能加上新字段。
    MATCH_STAT_COLUMNS = {
        "home_sot": "INTEGER",
        "away_sot": "INTEGER",
        "stats_fetched": "INTEGER DEFAULT 0",
    }

    def _ensure_match_stat_columns(self) -> None:
        """为旧库补上场面数据列；缺哪列补哪列，已有则不动。"""
        try:
            conn = self._connect()
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
            for col, ddl in self.MATCH_STAT_COLUMNS.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {ddl}")
            conn.commit()
        except Exception as exc:  # 补列失败不能拖垮启动
            log.warning("补建比赛统计列失败：%s", exc)

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
        """回写真实比分。返回是否有记录被更新。

        顺带保证 result 合法：命中判定是 `result == 实际赛果`，result 若为
        'pending'/NULL 则这场永远算未命中，命中率会被悄悄压低。
        """
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE predictions SET actual_home=?, actual_away=?, settled_at=? "
                "WHERE fixture_id=?",
                (home_score, away_score, _now_iso(), str(fixture_id)),
            )
            conn.commit()
            updated = cur.rowcount > 0
        except Exception as exc:
            log.warning("回写赛果失败（fixture=%s）：%s", fixture_id, exc)
            return False
        if updated:
            self._backfill_result(fixture_id)
        return updated

    def _backfill_result(self, fixture_id) -> bool:
        """单条修复：result 非法时按已保存的胜平负概率取最大方向回填。

        概率本身没丢，方向可以确定性恢复；概率全为 0 的异常行不动，
        避免凭空造出一个方向。
        """
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE predictions SET result = CASE "
                "  WHEN home_prob >= draw_prob AND home_prob >= away_prob THEN '主胜' "
                "  WHEN away_prob >= draw_prob AND away_prob >= home_prob THEN '客胜' "
                "  ELSE '平局' END "
                "WHERE fixture_id=? "
                "  AND (result IS NULL OR result NOT IN ('主胜','平局','客胜')) "
                "  AND COALESCE(home_prob,0) + COALESCE(draw_prob,0) + COALESCE(away_prob,0) > 0",
                (str(fixture_id),),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as exc:
            log.warning("回填预测方向失败（fixture=%s）：%s", fixture_id, exc)
            return False

    def backfill_results(self) -> int:
        """批量修复历史遗留的非法 result，返回修复条数。

        幂等：合法行不满足 NOT IN 条件，重复调用不会改动任何数据。
        只动 result，不碰 actual_* / settled_at / 概率，命中判定口径不变。
        """
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE predictions SET result = CASE "
                "  WHEN home_prob >= draw_prob AND home_prob >= away_prob THEN '主胜' "
                "  WHEN away_prob >= draw_prob AND away_prob >= home_prob THEN '客胜' "
                "  ELSE '平局' END "
                "WHERE (result IS NULL OR result NOT IN ('主胜','平局','客胜')) "
                "  AND COALESCE(home_prob,0) + COALESCE(draw_prob,0) + COALESCE(away_prob,0) > 0",
            )
            conn.commit()
            fixed = cur.rowcount or 0
            if fixed:
                log.info("回填非法预测方向 %s 条（历史遗留 pending/NULL，命中率会随之修正）", fixed)
            return fixed
        except Exception as exc:
            log.warning("批量回填预测方向失败：%s", exc)
            return 0

    # ---- 原子事务 -----------------------------------------------------------
    @contextmanager
    def atomic(self, *, retries: int = DEFAULT_WRITE_RETRIES):
        """原子写事务（BEGIN IMMEDIATE），供批量且必须一致的写入使用。

        为什么需要：Python sqlite3 默认 isolation_level='' 会隐式开启事务，
        直接 execute("BEGIN IMMEDIATE") 会报
        "cannot start a transaction within a transaction"。
        故此处临时切到 isolation_level=None（autocommit）手动控制事务边界，
        用完再还原，不影响本文件其它方法的既有行为。

        BEGIN IMMEDIATE 立刻取写锁，避免多个定时任务并发时
        先读后写造成的 `database is locked` 死锁。

        **悲观并发 + 指数退避**：云环境（Railway 挂载卷）下 SQLite 写锁冲突
        比本地频繁得多。取锁失败时按 20ms → 40ms → 80ms… 退避重试，
        并叠加少量随机抖动，避免多个任务同时重试造成「惊群」。
        全部重试失败后才抛出，调用方捕获后降级（不影响机器人主流程）。
        """
        conn = self._connect()
        previous = conn.isolation_level
        conn.isolation_level = None
        # 连接级 busy_timeout 默认 15s：单次等待太久，且会与退避叠加成
        # 「(retries+1) × 15s」的长阻塞（实测 45s）。事务期间降到 100ms，
        # 让锁冲突快速暴露，交由下面的指数退避接管，总耗时可控在 1s 内。
        try:
            conn.execute(f"PRAGMA busy_timeout={TRANSACTION_BUSY_TIMEOUT_MS}")
        except Exception:
            pass
        last_exc: Exception | None = None
        for attempt in range(max(0, int(retries)) + 1):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if attempt >= retries:
                    conn.isolation_level = previous
                    _restore_busy_timeout(conn)
                    raise
                _sleep_backoff(attempt, str(exc))
        else:  # pragma: no cover - retries<0 的极端情况
            conn.isolation_level = previous
            _restore_busy_timeout(conn)
            raise last_exc or sqlite3.OperationalError("无法开启事务")

        try:
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            # 提交阶段仍可能被锁：再退避重试一轮整体事务
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            if "locked" not in str(exc).lower():
                raise
            if retries > 0:
                _sleep_backoff(0, str(exc))
                with self.atomic(retries=retries - 1) as c2:
                    yield c2
                return
            raise
        finally:
            conn.isolation_level = previous
            _restore_busy_timeout(conn)

    # ---- Elo 评分持久化 -----------------------------------------------------
    def elo_ratings(self, competition: str) -> dict[str, float]:
        """读取某联赛全部球队的当前 Elo 分，返回 {team_id: rating}。"""
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT team_id, rating FROM elo_ratings WHERE competition=?", (competition,)
            ).fetchall()
            return {r["team_id"]: float(r["rating"]) for r in rows}
        except Exception as exc:
            log.warning("读取 Elo 评分失败（competition=%s）：%s", competition, exc)
            return {}

    def save_elo_ratings(self, competition: str, ratings: dict[str, float],
                         *, matches_played: dict[str, int] | None = None,
                         last_match: dict[str, str] | None = None) -> None:
        """整批写回 Elo 分。同队重复写入为更新，不产生重复行。"""
        if not ratings:
            return
        try:
            conn = self._connect()
            now = _now_iso()
            for team_id, rating in ratings.items():
                conn.execute(
                    """INSERT INTO elo_ratings
                       (team_id, competition, rating, matches, last_match, updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(competition, team_id) DO UPDATE SET
                         rating=excluded.rating,
                         matches=excluded.matches,
                         last_match=excluded.last_match,
                         updated_at=excluded.updated_at""",
                    (
                        str(team_id), competition, float(rating),
                        int((matches_played or {}).get(str(team_id), 0)),
                        (last_match or {}).get(str(team_id)),
                        now,
                    ),
                )
            conn.commit()
        except Exception as exc:
            log.warning("写入 Elo 评分失败（competition=%s）：%s", competition, exc)

    def elo_is_processed(self, fixture_id) -> bool:
        """该场比赛是否已计入 Elo（幂等检查，防止重复计算）。"""
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT 1 FROM elo_processed WHERE fixture_id=?", (str(fixture_id),)
            ).fetchone()
            return row is not None
        except Exception as exc:
            log.warning("查询 Elo 处理标记失败（fixture=%s）：%s", fixture_id, exc)
            return False

    def mark_elo_processed(self, fixture_id, *, competition: str = "", season: int | None = None,
                           home_team_id: str = "", away_team_id: str = "",
                           home_score: int | None = None, away_score: int | None = None) -> None:
        """标记比赛已计入 Elo，并记录比分用于排查。"""
        try:
            conn = self._connect()
            conn.execute(
                """INSERT INTO elo_processed
                   (fixture_id, competition, season, home_team_id, away_team_id,
                    home_score, away_score, processed_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(fixture_id) DO UPDATE SET
                     home_score=excluded.home_score,
                     away_score=excluded.away_score,
                     processed_at=excluded.processed_at""",
                (str(fixture_id), competition, season, str(home_team_id), str(away_team_id),
                 home_score, away_score, _now_iso()),
            )
            conn.commit()
        except Exception as exc:
            log.warning("写入 Elo 处理标记失败（fixture=%s）：%s", fixture_id, exc)

    def log_elo_change(self, fixture_id, competition: str, team_id,
                       before: float, after: float) -> None:
        """记录单场比赛引起的 Elo 变化，便于核验是否被重复计算。"""
        try:
            conn = self._connect()
            conn.execute(
                """INSERT INTO elo_log
                   (fixture_id, team_id, competition, before, after, delta, processed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (str(fixture_id), str(team_id), competition,
                 float(before), float(after), float(after) - float(before), _now_iso()),
            )
            conn.commit()
        except Exception as exc:
            log.warning("写入 Elo 变更日志失败（fixture=%s）：%s", fixture_id, exc)

    def elo_history(self, fixture_id) -> list[sqlite3.Row]:
        """某场比赛产生的 Elo 变更记录；正常情况下每个队最多一条。"""
        try:
            conn = self._connect()
            return list(conn.execute(
                "SELECT * FROM elo_log WHERE fixture_id=? ORDER BY id", (str(fixture_id),)
            ))
        except Exception as exc:
            log.warning("读取 Elo 变更记录失败（fixture=%s）：%s", fixture_id, exc)
            return []

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
                "SELECT level_key, result, actual_home, actual_away, kickoff, "
                "home_prob, draw_prob, away_prob "
                "FROM predictions WHERE actual_home IS NOT NULL AND actual_away IS NOT NULL "
                "ORDER BY kickoff"
            ))
        except Exception as exc:
            log.warning("统计命中率失败：%s", exc)
            return {"total": 0, "hit": 0, "rate": None, "streak": 0, "by_level": {},
                    "pending": 0, "persistent": self.persistent,
                    "brier": None, "logloss": None, "scored": 0}

        total = hit = 0
        streak = 0
        scored = 0
        brier_sum = 0.0
        logloss_sum = 0.0
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
            slot = by_level.setdefault(
                lv, {"total": 0, "hit": 0, "scored": 0, "brier_sum": 0.0, "logloss_sum": 0.0})
            slot["total"] += 1
            slot["hit"] += 1 if ok else 0
            # 概率缺失（早期记录或降级路径）时跳过：只统计命中率，不计入校准指标
            cal = brier_logloss(
                {"主胜": r["home_prob"], "平局": r["draw_prob"], "客胜": r["away_prob"]},
                actual,
            )
            if cal is not None:
                b, ll = cal
                scored += 1
                brier_sum += b
                logloss_sum += ll
                slot["scored"] += 1
                slot["brier_sum"] += b
                slot["logloss_sum"] += ll

        # 累加器换成均值，并剔除中间量——调用方不该看到 brier_sum 这种半成品
        for slot in by_level.values():
            s = slot.pop("scored")
            bs = slot.pop("brier_sum")
            ls = slot.pop("logloss_sum")
            slot["scored"] = s
            slot["brier"] = (bs / s) if s else None
            slot["logloss"] = (ls / s) if s else None

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
            "scored": scored,
            "brier": (brier_sum / scored) if scored else None,
            "logloss": (logloss_sum / scored) if scored else None,
        }

    def tables(self) -> list[str]:
        """当前数据库里的表名，供 /storage 自检确认落盘生效。"""
        try:
            conn = self._connect()
            return [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
        except Exception as exc:
            log.warning("查询数据表失败：%s", exc)
            return []

    def recent(self, limit: int = 10) -> list[sqlite3.Row]:
        try:
            conn = self._connect()
            return list(conn.execute(
                "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?", (limit,)
            ))
        except Exception as exc:
            log.warning("查询最近预测失败：%s", exc)
            return []


    # ---- 比赛本地缓存（外部 API 拉取，SQLite 保存） ---------------------------
    def save_matches(self, competition: str, matches: list[dict],
                     source: str = "football-data.org",
                     http_status: str = "200", message: str = "") -> int:
        """把外部 API 拉到的比赛写入本地库，返回实际写入条数。

        同一场比赛重复拉取时更新状态与比分，不产生重复行。
        """
        saved = 0
        try:
            conn = self._connect()
            for m in matches:
                fx = _extract_match(m)
                mid = fx["id"]
                utc_date = fx["utc_date"]
                home_id = fx["home_id"]
                away_id = fx["away_id"]
                # 先按业务唯一键定位已有行：命中则更新，避免 UNIQUE 冲突中断整批
                row = conn.execute(
                    "SELECT id FROM matches WHERE competition_code=? AND utc_date=? "
                    "AND home_team_id=? AND away_team_id=?",
                    (competition, utc_date, home_id, away_id),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        """UPDATE matches SET status=?, home_score=?, away_score=?,
                           raw_json=?, updated_at=? WHERE id=?""",
                        (fx["status"], fx["home_score"], fx["away_score"],
                         json.dumps(m, ensure_ascii=False), _now_iso(), row["id"]),
                    )
                    saved += 1
                    continue
                conn.execute(
                    """INSERT INTO matches
                       (id, competition_code, season, utc_date, status, matchday,
                        home_team_id, home_team_name, away_team_id, away_team_name,
                        home_score, away_score, source, raw_json, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         status=excluded.status,
                         home_score=excluded.home_score,
                         away_score=excluded.away_score,
                         raw_json=excluded.raw_json,
                         updated_at=excluded.updated_at""",
                    (
                        mid, competition,
                        fx["season"],
                        utc_date,
                        fx["status"],
                        fx["matchday"],
                        home_id, fx["home_name"],
                        away_id, fx["away_name"],
                        fx["home_score"], fx["away_score"],
                        source,
                        json.dumps(m, ensure_ascii=False),
                        _now_iso(),
                    ),
                )
                saved += 1
            conn.commit()
        except Exception as exc:
            log.warning("保存比赛到本地库失败：%s", exc)
            return 0

        self.log_sync(source, competition, "", "", http_status, len(matches), saved, message)
        return saved

    def latest_finished(self, competition: str = "", limit: int = 1) -> list[dict]:
        """最近已完场的比赛（有比分），按开赛时间倒序。"""
        try:
            conn = self._connect()
            if competition:
                rows = conn.execute(
                    "SELECT id, home_team_id, away_team_id, home_team_name, away_team_name, "
                    "utc_date, home_score, away_score, home_sot, away_sot FROM matches "
                    "WHERE competition_code=? AND home_score IS NOT NULL "
                    "ORDER BY utc_date DESC, id DESC LIMIT ?",
                    (competition, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, home_team_id, away_team_id, home_team_name, away_team_name, "
                    "utc_date, home_score, away_score, home_sot, away_sot FROM matches "
                    "WHERE home_score IS NOT NULL "
                    "ORDER BY utc_date DESC, id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        except Exception as exc:
            log.warning("查询已完场比赛失败：%s", exc)
            return []
        return [dict(r) for r in rows]

    def finished_without_stats(self, competition: str = "", limit: int = 50) -> list[dict]:
        """已完场但还没抓过技术统计的比赛。

        stats_fetched 标记「已经探测过」：探测成功与否都置 1，
        避免对「该场本来就没有统计」的比赛反复浪费额度。
        """
        try:
            conn = self._connect()
            sql = (
                "SELECT id, home_team_id, away_team_id, utc_date FROM matches "
                "WHERE home_score IS NOT NULL "
                "AND (stats_fetched IS NULL OR stats_fetched=0) "
            )
            params: list = []
            if competition:
                sql += "AND competition_code=? "
                params.append(competition)
            sql += "ORDER BY utc_date DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            log.warning("查询待抓统计的比赛失败：%s", exc)
            return []
        return [dict(r) for r in rows]

    def save_match_stats(self, fixture_id, home_sot: int | None,
                         away_sot: int | None) -> bool:
        """写入一场比赛的射正数；探测过就置 stats_fetched=1。

        射正数缺失（None）也算「探测过」，只写标记不写数值，
        否则同一批没有统计的比赛会被每天重复拉取，白耗额度。
        """
        try:
            conn = self._connect()
            conn.execute(
                "UPDATE matches SET home_sot=?, away_sot=?, stats_fetched=1 WHERE id=?",
                (home_sot, away_sot, str(fixture_id)),
            )
            conn.commit()
        except Exception as exc:
            log.warning("保存比赛统计失败（fixture=%s）：%s", fixture_id, exc)
            return False
        return True

    def count_finished_matches(self, competition: str | None = None) -> int:
        """已完赛（有比分）的场次。

        这是强度榜的有效样本量：未开赛的比赛没有比分，不能参与攻防强度估计。
        传入 competition 则只统计该联赛，不传则统计全部。
        """
        try:
            conn = self._connect()
            if competition:
                row = conn.execute(
                    "SELECT COUNT(*) FROM matches "
                    "WHERE competition_code=? AND home_score IS NOT NULL",
                    (str(competition),)).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM matches "
                    "WHERE home_score IS NOT NULL").fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            log.warning("统计已完赛场次失败：%s", exc)
            return 0

    def load_matches(self, competition: str, date_from: str, date_to: str,
                     *, limit: int = 500) -> list[dict]:
        """从本地库读取指定日期范围内的比赛（不再回源）。

        返回的是保存时的原始 fixture 结构，可直接喂给预测/展示模块。
        """
        try:
            conn = self._connect()
            rows = list(conn.execute(
                """SELECT raw_json FROM matches
                   WHERE competition_code=? AND substr(utc_date,1,10) BETWEEN ? AND ?
                   ORDER BY utc_date LIMIT ?""",
                (str(competition), date_from, date_to, limit),
            ))
        except Exception as exc:
            log.warning("读取本地比赛失败：%s", exc)
            return []
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["raw_json"]))
            except (ValueError, TypeError):
                continue
        return out

    def matches_with_predictions(self, day: str, *, limit: int = 300) -> list[dict]:
        """指定日期（YYYY-MM-DD）的全部比赛，左连已生成的预测。

        为什么连表而不是让上层自己拼：predictions 的键是 API fixture_id 的
        **文本**形式，matches 的主键是**整数** id，两者写法还不一致
        （"fd-123" vs 123）。上层各自拼接极易漏配，这里统一 CAST 后比较。

        未生成预测的比赛 `prediction` 为 None —— 调用方据此显示「未预测」，
        而不是假装成 0%，避免把「没算过」误读成「算出来是 0」。
        """
        try:
            conn = self._connect()
            rows = list(conn.execute(
                """SELECT m.id AS match_id, m.competition_code AS comp,
                          m.utc_date AS kickoff, m.status AS status,
                          m.home_team_name AS home, m.away_team_name AS away,
                          m.home_score AS hs, m.away_score AS aws,
                          p.result AS pred_result, p.home_prob, p.draw_prob,
                          p.away_prob, p.level_key,
                          p.actual_home, p.actual_away
                   FROM matches m
                   LEFT JOIN predictions p ON p.fixture_id = CAST(m.id AS TEXT)
                   WHERE substr(m.utc_date,1,10)=?
                   ORDER BY m.competition_code, m.utc_date
                   LIMIT ?""",
                (str(day), int(limit)),
            ))
        except Exception as exc:
            log.warning("读取当日赛程失败：%s", exc)
            return []
        out: list[dict] = []
        for r in rows:
            if r["pred_result"] is None:
                pred = None
            else:
                pred = {
                    "result": r["pred_result"],
                    "home_prob": r["home_prob"],
                    "draw_prob": r["draw_prob"],
                    "away_prob": r["away_prob"],
                    "level": r["level_key"],
                }
            out.append({
                "fixture_id": str(r["match_id"]),
                "competition": r["comp"],
                "kickoff": r["kickoff"],
                "status": r["status"],
                "home": r["home"],
                "away": r["away"],
                "home_score": r["hs"],
                "away_score": r["aws"],
                "actual_home": r["actual_home"],
                "actual_away": r["actual_away"],
                "prediction": pred,
            })
        return out

    def matches_count(self) -> int:
        try:
            conn = self._connect()
            row = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    def log_sync(self, source: str, competition: str, date_from: str, date_to: str,
                 http_status: str, received: int, saved: int, message: str = "") -> None:
        """每次同步记一条结构化日志：源、联赛、日期范围、状态码、收发数量。"""
        log.info(
            "同步｜source=%s competition=%s date_from=%s date_to=%s "
            "http_status=%s received_matches=%d saved_matches=%d message=%s",
            source, competition, date_from, date_to,
            http_status, received, saved, message or "-",
        )
        try:
            conn = self._connect()
            conn.execute(
                """INSERT INTO sync_log
                   (source, competition, date_from, date_to, http_status,
                    received, saved, message, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (source, competition, date_from, date_to, str(http_status),
                 received, saved, message, _now_iso()),
            )
            conn.commit()
        except Exception as exc:
            log.warning("记录同步日志失败：%s", exc)

    def recent_sync(self, limit: int = 5) -> list[sqlite3.Row]:
        try:
            conn = self._connect()
            return list(conn.execute(
                "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
            ))
        except Exception:
            return []


def _stable_id(raw: str) -> int:
    """把 'fd-123' / '123' 这类 ID 归一化成整数主键（非数字时取稳定哈希）。"""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        try:
            return int(digits[-9:])  # 取末 9 位，避免溢出
        except ValueError:
            pass
    return abs(hash(raw)) % (10**9)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_match(m: dict) -> dict:
    """把两种数据源的赛程结构归一化成同一组字段。

    API-Football（主源）是嵌套结构：fixture / teams / goals / league。
    football-data.org（备用源）是扁平结构：utcDate / homeTeam / score.fullTime。

    之前只认嵌套结构，备用源拉到的数据会被存成空值，
    导致本地库里 utc_date、队名、比分全为空，历史查询查不到任何东西。
    """
    fixture = m.get("fixture") or {}
    teams = m.get("teams") or {}
    goals = m.get("goals") or {}
    league = m.get("league") or {}

    # 备用源：顶层就是比赛本体
    flat = bool(m.get("utcDate")) or bool(m.get("homeTeam"))

    if flat:
        home = m.get("homeTeam") or {}
        away = m.get("awayTeam") or {}
        full_time = (m.get("score") or {}).get("fullTime") or {}
        raw_id = m.get("id")
        utc_date = m.get("utcDate") or ""
        status = m.get("status") or ""
        season = (m.get("season") or {}).get("startDate") or ""
        season = _as_int(season[:4]) if season else None
        matchday = m.get("matchday")
        home_score = _as_int(full_time.get("home"))
        away_score = _as_int(full_time.get("away"))
    else:
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        raw_id = fixture.get("id")
        utc_date = fixture.get("date") or ""
        status = (fixture.get("status") or {}).get("short") or ""
        season = _as_int(league.get("season"))
        matchday = None
        # 主源的 round 形如 "Regular Season - 5"
        raw_round = league.get("round") or ""
        digits = "".join(ch for ch in raw_round.split("-")[-1] if ch.isdigit())
        matchday = _as_int(digits) if digits else None
        # 加时/点球可能为空，退回到 fulltime
        home_score = _as_int(goals.get("home"))
        away_score = _as_int(goals.get("away"))

    return {
        "id": _stable_id(str(raw_id or "")),
        "utc_date": utc_date,
        "status": status,
        "season": season,
        "matchday": matchday,
        "home_id": str(home.get("id") or ""),
        "home_name": home.get("name") or "",
        "away_id": str(away.get("id") or ""),
        "away_name": away.get("name") or "",
        "home_score": home_score,
        "away_score": away_score,
    }


# LogLoss 的概率下限。取 1e-6 而非 1e-15：后者在模型给出极端概率时会算出
# 14+ 的巨大值，一条就足以把均值拉飞，而这是展示指标不是数值优化，宁可截断。
LOGLOSS_FLOOR = 1e-6

# 三分类随机猜测（1/3, 1/3, 1/3）的 Brier 理论值，用作对照基准。
BRIER_RANDOM = 2.0 / 3.0


def brier_logloss(probs: dict[str, float], actual: str) -> tuple[float, float] | None:
    """三分类校准指标：(Brier, LogLoss)。

    纯函数，便于单测直接断言数值——此前测试只验证「指标能被计算和分组」，
    没验证「算出来是对的」，破坏公式时测试仍全绿，这是个真实盲区。

    probs 用 result 字段同一套键：主胜 / 平局 / 客胜。
    概率缺失、非有限值或三者之和不为正时返回 None，由调用方跳过该样本。
    """
    keys = ("主胜", "平局", "客胜")
    if actual not in keys:
        return None
    try:
        vec = [float(probs.get(k, 0.0)) for k in keys]
    except (TypeError, ValueError):
        return None
    if any(v != v or v in (float("inf"), float("-inf")) for v in vec):  # NaN/inf
        return None
    if any(v < 0.0 for v in vec):
        return None
    total = sum(vec)
    if total <= 0.0:
        return None
    # 概率可能未归一化（历史数据/降级路径），归一后再算，否则 Brier 会被量纲污染
    vec = [v / total for v in vec]
    outcome_index = keys.index(actual)
    brier = sum((v - (1.0 if i == outcome_index else 0.0)) ** 2 for i, v in enumerate(vec))
    logloss = -math.log(max(vec[outcome_index], LOGLOSS_FLOOR))
    return brier, logloss


def _outcome(home: int | None, away: int | None) -> str | None:
    """真实比分 → 胜平负标签（与模型 result 字段同一套口径）。"""
    if home is None or away is None:
        return None
    if home > away:
        return "主胜"
    if home < away:
        return "客胜"
    return "平局"
