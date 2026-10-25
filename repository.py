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
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = None  # 由 paths.DB_PATH 统一决定（默认 /data/football.db）


def _dump_fixture_json(fixture: Any) -> str | None:
    """把单场原始赛程序列化成 JSON；不可序列化或为空时返回 None。

    序列化失败绝不能让「保存预测」整条链路失败——赛程快照只是可追溯性数据，
    丢了大不了回到「重新拉取」，但不该把预测结果一起弄丢。
    """
    if not isinstance(fixture, dict) or not fixture:
        return None
    try:
        return json.dumps(fixture, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        log.warning("赛程快照序列化失败，本次不落盘：%s", exc)
        return None

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

-- 回测结果留档：每次回测都落盘，重启可查、跨部署保留。
-- 没有它，回测结论只活在进程内存里，容器一重启就丢失，
-- 也就无从判断"模型是在变好还是在变差"。
CREATE TABLE IF NOT EXISTS backtest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    competition   TEXT NOT NULL,
    season        TEXT,             -- NULL = 全部赛季
    variant       TEXT NOT NULL,
    min_history   INTEGER NOT NULL,
    n             INTEGER NOT NULL, -- 评估场次
    hits          INTEGER NOT NULL,
    accuracy      REAL,
    log_loss      REAL,
    rps           REAL,
    brier         REAL,
    ece           REAL,
    rho           REAL,
    corpus_db     INTEGER,          -- 语料中来自数据库的场数
    corpus_csv    INTEGER,          -- 语料中来自内置 CSV 的场数
    verdict       TEXT,
    by_level      TEXT,             -- JSON：各信心等级的命中率
    calibration   TEXT,             -- JSON：分档校准
    -- 精确比分口径：与 accuracy（方向命中）是两个量级的尺子，
    -- 早期只存 accuracy，看板写「命中率」时读者一律理解成方向命中，
    -- 于是 5% 被当成模型崩了。两个口径必须分列存储，不可再合并。
    exact_n       INTEGER,          -- 参与精确比分评估的场次
    exact_hits    INTEGER,
    exact_rate    REAL,             -- 精确比分命中率（猜中 2-1），业界 5~8%
    -- 语料窗口：回测每天跑，但语料固定时结果逐字相同（20 条记录一模一样），
    -- 看不出模型在变好还是变坏。记录窗口边界 + 相对上次的增量，
    -- 才能让「每天跑一次」产生可观测的变化。
    window_start  TEXT,             -- 语料最早比赛日期
    window_end    TEXT,             -- 语料最晚比赛日期
    n_new         INTEGER,          -- 相比上次同口径回测，语料新增的场次数
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bt_comp_created
    ON backtest_runs(competition, created_at);

-- 赛前信号对照：一行 = 一场比赛 × 一种信号类型（见 signal_ab 模块顶部说明）。
-- 同一场可能同时触发伤停与阵容，混在一行就没法拆开评估每种信号的价值。
CREATE TABLE IF NOT EXISTS signal_ab (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id            TEXT NOT NULL,
    signal_type           TEXT NOT NULL,   -- 字符串而非枚举：新增类型不改表
    signal_strength       REAL,            -- 原始值（归一化逻辑会变，原始值不会）
    signal_strength_norm  REAL,            -- 归一化值，仅展示参考
    change_type           TEXT NOT NULL DEFAULT 'applied',  -- applied / revoked
    pure_baseline         TEXT,            -- 无任何赛前信号的预测（JSON）
    partial_baseline      TEXT,            -- 有他类信号、独缺本类的预测（JSON）
    signal_prediction     TEXT,            -- 施加本信号后的预测（JSON）
    human_note            TEXT,            -- human readable，复盘时一眼看懂
    status                TEXT NOT NULL DEFAULT 'ok',      -- ok / partial_failure
    error                 TEXT,
    actual_home           INTEGER,
    actual_away           INTEGER,
    settled_at            TEXT,
    is_processed          INTEGER NOT NULL DEFAULT 0,
    is_archived           INTEGER NOT NULL DEFAULT 0,  -- 预留：将来归档不用改表
    is_test               INTEGER NOT NULL DEFAULT 0,  -- 测试污染必须能过滤掉
    config_snapshot       TEXT,            -- 当时的关键配置，半年后回溯靠它
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
-- 复盘查询按 (fixture_id, signal_type) 聚合；重新预测时 UPDATE 同一行，
-- 而不是 INSERT 新行 —— 否则同一场会有多条记录，复盘不知道该用哪条。
CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_ab
    ON signal_ab(fixture_id, signal_type);
CREATE INDEX IF NOT EXISTS idx_signal_ab_processed
    ON signal_ab(is_processed);
CREATE INDEX IF NOT EXISTS idx_signal_ab_type
    ON signal_ab(signal_type, is_processed);

-- 信号类型注册表：新增类型先在这里登记，避免在代码里硬编码散落各处。
CREATE TABLE IF NOT EXISTS signal_registry (
    signal_type  TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    description  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

-- 权重变更历史：只追加、永不覆盖。旧权重必须永久保留，否则没法回答
-- "某段时间表现差是不是因为权重不好"。
CREATE TABLE IF NOT EXISTS signal_weight_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_type   TEXT NOT NULL,
    old_weight    REAL,
    new_weight    REAL,
    applied       INTEGER NOT NULL DEFAULT 0,   -- 0=仅建议/模拟，1=已真正应用
    sample_from   TEXT,
    sample_to     TEXT,
    n_samples     INTEGER,
    metric        TEXT,
    reason        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_swl_type ON signal_weight_log(signal_type, created_at);

-- 发布级配置快照：每次发布记录当时的关键配置。半年后回答"那时对照结果
-- 为什么长那样"必须靠它 —— 代码里只有今天的默认值，而当时的强度/阈值
-- /融合权重可能完全不同，没有快照历史数据就废了一半。
CREATE TABLE IF NOT EXISTS config_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT,             -- 代码版本（commit SHA 或 release tag）
    payload     TEXT NOT NULL,    -- JSON：各信号权重、阈值、融合权重等
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cfg_snap_created
    ON config_snapshot(created_at);

-- 对照数据的只读视图：把 JSON 概率列展开成数值列，导出/外部分析直接查它，
-- 不用在每个脚本里重复解析 JSON（重复解析迟早会有一处漏掉口径）。
CREATE VIEW IF NOT EXISTS v_signal_ab AS
SELECT id, fixture_id, signal_type, signal_strength, signal_strength_norm,
       change_type, status, error, actual_home, actual_away,
       CAST(json_extract(pure_baseline,    '$.home') AS REAL) AS pure_home,
       CAST(json_extract(pure_baseline,    '$.draw') AS REAL) AS pure_draw,
       CAST(json_extract(pure_baseline,    '$.away') AS REAL) AS pure_away,
       CAST(json_extract(partial_baseline, '$.home') AS REAL) AS partial_home,
       CAST(json_extract(partial_baseline, '$.draw') AS REAL) AS partial_draw,
       CAST(json_extract(partial_baseline, '$.away') AS REAL) AS partial_away,
       CAST(json_extract(signal_prediction,'$.home') AS REAL) AS signal_home,
       CAST(json_extract(signal_prediction,'$.draw') AS REAL) AS signal_draw,
       CAST(json_extract(signal_prediction,'$.away') AS REAL) AS signal_away,
       human_note, config_snapshot, is_test, is_archived,
       created_at, updated_at
FROM signal_ab;

-- 赛前二次推送去重：一行 = 一场已经推送过赛前更新的比赛。
--   没有它，赛前刷新每 15 分钟一轮会把同一场比赛反复推一遍，
--   用户端就是刷屏。同一场比赛终身只推一次，见 scheduler.prematch_push。
CREATE TABLE IF NOT EXISTS prematch_push (
    fixture_id  INTEGER PRIMARY KEY,
    pushed_at   TEXT NOT NULL
);

-- 定时任务执行流水：回答「这个 job 到底跑没跑、成功没成功」。
-- 为什么必须有：此前 7 个 job 的失败全部只写 log.warning，既不落库也不告警。
-- 后果是机器人照常推送、看板显示旧数据，用户以为一切正常，
-- 只有去翻 Railway 日志才知道某个 job 已经连续失败好几天。
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,      -- 任务名（sync / backtest / settle ...）
    status      TEXT NOT NULL,      -- ok / fail / skip
    message     TEXT,               -- 失败原因或跳过原因，成功时为 NULL
    duration_ms INTEGER,            -- 耗时，用于发现「跑得越来越慢」
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job_time
    ON job_runs(job, created_at DESC);

-- 迁移完成标记：补列脚本必须幂等，且跑过一次后不必每次启动都扫全表。
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
"""

def _ddl_of(table: str) -> str:
    """从 SCHEMA 里切出某张表的建表语句。

    配额模块（record_quota / read_quota）会用**独立的 sqlite 连接**写
    api_quota，且可能在主库初始化之前就被调用。它需要一份能保证表存在的
    DDL。早先这里是把整段 DDL 又抄了一遍，和 SCHEMA 里的那份形成双份定义
    ——两份当时一致，但将来给表加字段时只改一处就会埋雷。
    现在改为直接从 SCHEMA 切，物理上只有一份文本，不可能再走偏。
    """
    marker = f"CREATE TABLE IF NOT EXISTS {table} "
    start = SCHEMA.index(marker)
    return SCHEMA[start:SCHEMA.index(");", start) + 2]


# 配额模块专用：内容与 SCHEMA 中的 api_quota 定义逐字同源。
QUOTA_SCHEMA = _ddl_of("api_quota")

_local = threading.local()


def _quota_day() -> str:
    """按机器人时区（默认 Asia/Shanghai）取当日日期。

    免费层额度按自然日重置，所以计数也按「机器人所在时区的自然日」分桶，
    避免 UTC 与本地时区错位导致跨天统计串味。
    """
    try:
        import os
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Shanghai"))
    except Exception:  # 容器缺 tzdata 时退化为固定 +8，不影响计数可用性
        from datetime import timedelta
        tz: ZoneInfo | timezone = timezone(timedelta(hours=8))
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
        self._ensure_backtest_run_columns()
        self._ensure_signal_ab_columns()
        self._seed_signal_registry()
        self._ensure_prediction_fixture_column()

    PREDICTION_FIXTURE_COLUMN = "fixture_json"

    def _ensure_prediction_fixture_column(self) -> None:
        """为旧库补上 predictions.fixture_json：单场比赛的原始赛程快照。

        没有它，赛程缓存（TTL 10 分钟）过期或机器人重启后，用户点开历史推送
        卡片就会误报「该场比赛已不在今日赛程中」——预测明明算过、记录还在，
        却因为重新拉取的今日赛程里没有它而打不开。快照落库后，
        只要这场比赛算过一次，任何时候都能点开。补列幂等，失败不拖垮启动。
        """
        try:
            conn = self._connect()
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(predictions)")}
            if self.PREDICTION_FIXTURE_COLUMN not in existing:
                conn.execute("ALTER TABLE predictions ADD COLUMN fixture_json TEXT")
            conn.commit()
        except Exception as exc:
            log.warning("补建 predictions.fixture_json 列失败：%s", exc)

    def get_fixture_json(self, fixture_id) -> dict[str, Any] | None:
        """取回该场比赛的原始赛程快照；没有快照返回 None（不抛异常）。

        快照缺失有两种正常情况：旧库补列前保存的预测、以及从未算过的比赛。
        两种情况都由上层按「确实找不到」处理，不伪装成数据源故障。
        """
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT fixture_json FROM predictions WHERE fixture_id = ?",
                (str(fixture_id),),
            ).fetchone()
        except Exception as exc:
            log.warning("读取赛程快照失败：%s", exc)
            return None
        if row is None:
            return None
        try:
            raw = row["fixture_json"]
        except (IndexError, KeyError, TypeError):
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def get_stored_prediction(self, fixture_id) -> dict[str, Any] | None:
        """取回已落盘的预测结果（不看赛程快照），没有则返回 None。

        与 get_fixture_json 的区别：那个取的是**原始赛程**（用于重新计算），
        这个取的是**预测结论本身**（用于直接展示）。

        为什么需要它：赛程快照是后来才加的列，此前算过的预测只有结论、
        没有快照；比赛一旦开赛或跨天，今日赛程里也查不到了。用户点旧卡片时
        会撞上「该场比赛已不在今日赛程中」——可结论明明就躺在库里。
        这类请求的目标是**看已算过的结论**，不是重新算一遍，
        所以只要结论在，就该展示出来，而不是报错。
        """
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT fixture_id, league, home, away, kickoff, home_prob, "
                "draw_prob, away_prob, best_score, result, actual_home, actual_away "
                "FROM predictions WHERE fixture_id = ?",
                (str(fixture_id),),
            ).fetchone()
        except Exception as exc:
            log.warning("读取已存预测失败：%s", exc)
            return None
        if row is None:
            return None
        d = dict(row)
        # 三合一概率缺任一，这份结论就不完整，宁可不展示也不造数
        if d.get("home_prob") is None or d.get("draw_prob") is None or d.get("away_prob") is None:
            return None
        return d

    # 旧库补列：精确的比分口径与语料窗口。与 signal_ab 同一套幂等机制。
    BACKTEST_RUN_COLUMNS = {
        "exact_n": "INTEGER",
        "exact_hits": "INTEGER",
        "exact_rate": "REAL",
        "window_start": "TEXT",
        "window_end": "TEXT",
        "n_new": "INTEGER",
    }

    def _ensure_backtest_run_columns(self) -> None:
        """为旧库补上「精确比分口径」与「语料窗口」列。

        缺列时 INSERT 会因列数不匹配直接失败（回测白跑），
        但这里只补列、不改 INSERT 语义，因此新老代码共用同一张表是安全的。
        """
        name = "backtest_runs_exact_and_window_v1"
        if self._migration_done(name):
            return
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='backtest_runs'").fetchall()
            if not rows:
                self._mark_migration(name)
                return
            existing = {r["name"] for r in
                        conn.execute("PRAGMA table_info(backtest_runs)")}
            for col, ddl in self.BACKTEST_RUN_COLUMNS.items():
                if col not in existing:
                    conn.execute(
                        f"ALTER TABLE backtest_runs ADD COLUMN {col} {ddl}")
            conn.commit()
            self._mark_migration(name)
        except Exception as exc:  # 补列失败不能拖垮启动
            log.warning("补建回测口径列失败：%s", exc)

    # 旧库补列必须幂等：跑两次不能报错、不能重复补。
    # 完成后写一条迁移标记，下次启动先查标记，避免每次扫全表。
    SIGNAL_AB_COLUMNS = {
        "signal_strength_norm": "REAL",
        "partial_baseline": "TEXT",
        "human_note": "TEXT",
        "is_archived": "INTEGER NOT NULL DEFAULT 0",
        "is_test": "INTEGER NOT NULL DEFAULT 0",
        "config_snapshot": "TEXT",
    }

    def _migration_done(self, name: str) -> bool:
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?", (name,)).fetchone()
        except Exception:
            return False
        return row is not None

    def _mark_migration(self, name: str) -> None:
        try:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(name, applied_at) VALUES(?,?)",
                (name, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
        except Exception as exc:
            log.warning("写入迁移标记失败（%s）：%s", name, exc)

    def _ensure_signal_ab_columns(self) -> None:
        """为早期版本的 signal_ab 表补列；已跑过则直接跳过（幂等）。"""
        name = "signal_ab_extra_columns_v1"
        if self._migration_done(name):
            return
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='signal_ab'").fetchall()
            if not rows:
                # 表不存在说明本次启动的 executescript 会建好，无需补列
                self._mark_migration(name)
                return
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(signal_ab)")}
            for col, ddl in self.SIGNAL_AB_COLUMNS.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE signal_ab ADD COLUMN {col} {ddl}")
            conn.commit()
        except Exception as exc:  # 补列失败不能拖垮启动
            log.warning("补建 signal_ab 列失败：%s", exc)
            return
        self._mark_migration(name)

    def prematch_pushed(self, fixture_id: Any) -> bool:
        """该场赛前更新是否已推送过（去重，避免每轮重复推送刷屏）。

        查不到一律返回 False —— 宁可多推一次，也不能因为标记读不出来
        而永远不再推送。理由同 _lineup_already_applied。
        """
        if fixture_id is None:
            return False
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT 1 FROM prematch_push WHERE fixture_id=?",
                (int(fixture_id),)).fetchone()
        except Exception as exc:
            log.warning("读取赛前推送标记失败（fixture=%s）：%s", fixture_id, exc)
            return False
        return row is not None

    def mark_prematch_pushed(self, fixture_ids: Any) -> int:
        """标记这些比赛已推送过赛前更新；返回实际写入的行数。"""
        ids: list[int] = []
        for fid in fixture_ids or []:
            try:
                ids.append(int(fid))
            except (TypeError, ValueError):
                continue
        if not ids:
            return 0
        try:
            conn = self._connect()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.executemany(
                "INSERT OR REPLACE INTO prematch_push(fixture_id, pushed_at) "
                "VALUES(?,?)", [(fid, now) for fid in ids])
            conn.commit()
        except Exception as exc:
            log.warning("写入赛前推送标记失败：%s", exc)
            return 0
        return len(ids)

    def _seed_signal_registry(self) -> None:
        """把已注册的信号类型写进注册表；已存在的不覆盖（保留自定义描述）。"""
        from signal_ab import SIGNAL_REGISTRY, utc_now_iso
        name = "signal_registry_seed_v1"
        if self._migration_done(name):
            return
        try:
            conn = self._connect()
            now = utc_now_iso()
            for sig_type, label, desc in SIGNAL_REGISTRY:
                conn.execute(
                    "INSERT OR IGNORE INTO signal_registry"
                    "(signal_type, label, description, enabled, created_at) "
                    "VALUES(?,?,?,1,?)", (sig_type, label, desc, now))
            conn.commit()
        except Exception as exc:
            log.warning("初始化信号注册表失败：%s", exc)
            return
        self._mark_migration(name)

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
                    best_score, created_at, inputs, fixture_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fixture_id) DO UPDATE SET
                     season=excluded.season, kickoff=excluded.kickoff,
                     model_version=excluded.model_version, source=excluded.source,
                     level_key=excluded.level_key, result=excluded.result,
                     home_prob=excluded.home_prob, draw_prob=excluded.draw_prob,
                     away_prob=excluded.away_prob, best_score=excluded.best_score,
                     created_at=excluded.created_at, inputs=excluded.inputs,
                     -- 新快照为空时保留旧快照：重算预测不该把已保存的赛程数据抹掉
                     fixture_json=COALESCE(excluded.fixture_json, predictions.fixture_json)""",
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
                    _dump_fixture_json(getattr(prediction, "fixture", None)),
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
    def settled_samples(self, limit: int = 2000) -> list[tuple[Any, ...]]:
        """已结算且三项概率齐全的样本，供温度校准拟合。

        **取的是模型原始概率，不是展示概率**。predictions 表里的
        home_prob/draw_prob/away_prob 是市场融合后的展示口径
        （市场占 w，默认 0.75），拿它拟合温度等于在校准市场——
        而实测市场本身已校准良好（校准后 Δlogloss=0），泊松才是过度自信方。
        因此优先读 inputs.model_probs（融合前留档），读不到才回退展示概率。

        返回 [(probs, actual_index), ...]，actual_index 为 0/1/2（主胜/平局/客胜）。
        任何异常都退化为空列表——拟合不出来就保持不校准，绝不猜测。
        """
        try:
            conn = self._connect()
            rows = list(conn.execute(
                "SELECT home_prob, draw_prob, away_prob, actual_home, actual_away, inputs "
                "FROM predictions WHERE actual_home IS NOT NULL AND actual_away IS NOT NULL "
                "AND home_prob IS NOT NULL AND draw_prob IS NOT NULL AND away_prob IS NOT NULL "
                "ORDER BY kickoff DESC LIMIT ?",
                (int(limit),),
            ))
        except Exception as exc:
            log.warning("读取已结算样本失败：%s", exc)
            return []
        out: list[tuple[Any, ...]] = []
        for r in rows:
            try:
                hs, as_ = int(r["actual_home"]), int(r["actual_away"])
            except (TypeError, ValueError, KeyError):
                continue
            probs = self._model_probs_from_inputs(r["inputs"])
            if probs is None:
                # 老数据没有留档：回退展示概率（那时尚未启用市场融合）
                try:
                    probs = [float(r["home_prob"]), float(r["draw_prob"]),
                             float(r["away_prob"])]
                except (TypeError, ValueError, KeyError):
                    continue
            if sum(probs) <= 0:
                continue
            out.append((probs, 0 if hs > as_ else (1 if hs == as_ else 2)))
        return out

    @staticmethod
    def _model_probs_from_inputs(raw: Any) -> list[float] | None:
        """从 inputs JSON 取模型原始概率；缺失/损坏返回 None。"""
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        vals = data.get("model_probs")
        if not isinstance(vals, (list, tuple)) or len(vals) < 3:
            return None
        try:
            out = [float(v) for v in vals[:3]]
        except (TypeError, ValueError):
            return None
        if any(not math.isfinite(v) or v < 0 for v in out) or sum(out) <= 0:
            return None
        return out

    # ---- 回测留档 ----------------------------------------------------------
    def save_backtest_run(self, *, competition: str, variant: str,
                          min_history: int, result: dict[str, Any],
                          season: int | None = None) -> int | None:
        """把一次回测的结论落盘。返回新记录 id，失败返回 None。

        回测很贵（1110 场要跑几十秒），结果只放内存等于白跑——
        容器重启、重新部署都会让它消失。落盘后才能回答"模型是否在变好"。
        """
        import json
        from datetime import datetime, timezone

        corpus = result.get("corpus") or {}
        try:
            conn = self._connect()

            # 语料增量：与上一次同口径回测比，语料多了多少场。
            # 没有这个数字，每天一条一模一样的记录会让人误以为"跑了很多次验证"，
            # 实际是同一次验证重复了 N 遍。n_new=0 就是在明确说：样本没长。
            n_new = None
            try:
                prev = conn.execute(
                    "SELECT corpus_db, corpus_csv FROM backtest_runs "
                    "WHERE competition=? AND variant=? ORDER BY id DESC LIMIT 1",
                    (str(competition), str(variant))).fetchone()
                if prev is not None:
                    prev_total = (prev["corpus_db"] or 0) + (prev["corpus_csv"] or 0)
                    cur_total = (corpus.get("db") or 0) + (corpus.get("history") or 0)
                    n_new = max(0, cur_total - prev_total)
            except Exception as exc:
                log.warning("计算语料增量失败（不影响落盘）：%s", exc)

            cur = conn.execute(
                "INSERT INTO backtest_runs (competition, season, variant, min_history, "
                "n, hits, accuracy, log_loss, rps, brier, ece, rho, "
                "corpus_db, corpus_csv, verdict, by_level, calibration, "
                "exact_n, exact_hits, exact_rate, window_start, window_end, n_new, "
                "created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(competition), season, str(variant), int(min_history),
                    int(result.get("total_predictions") or 0),
                    int(result.get("correct_predictions") or 0),
                    result.get("accuracy"), result.get("log_loss"),
                    result.get("rps"), result.get("brier"), result.get("ece"),
                    result.get("rho"),
                    corpus.get("db"), corpus.get("history"),
                    result.get("verdict"),
                    json.dumps(result.get("by_level") or {}, ensure_ascii=False),
                    json.dumps(result.get("calibration") or {}, ensure_ascii=False),
                    int(result.get("exact_n") or 0),
                    int(result.get("exact_hits") or 0),
                    result.get("exact_rate"),
                    corpus.get("window_start"), corpus.get("window_end"),
                    n_new,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return cur.lastrowid
        except Exception as exc:
            log.warning("回测结果落盘失败：%s", exc)
            return None

    def latest_backtest(self, competition: str,
                        variant: str | None = None) -> dict[str, Any] | None:
        """取最近一次回测结论。没有就返回 None（调用方自行判断）。"""
        import json

        try:
            conn = self._connect()
            if variant is None:
                row = conn.execute(
                    "SELECT * FROM backtest_runs WHERE competition=? "
                    "ORDER BY id DESC LIMIT 1", (str(competition),)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM backtest_runs WHERE competition=? AND variant=? "
                    "ORDER BY id DESC LIMIT 1", (str(competition), str(variant),
                                                 )).fetchone()
        except Exception as exc:
            log.warning("读取回测结论失败：%s", exc)
            return None
        if row is None:
            return None
        d = dict(row)
        for k in ("by_level", "calibration"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else {}
            except (TypeError, ValueError):
                d[k] = {}
        return d

    def backtest_history(self, competition: str, limit: int = 20) -> list[dict[str, Any]]:
        """历史回测记录（新→旧），用于观察指标随时间的变化。"""
        try:
            conn = self._connect()
            rows = list(conn.execute(
                "SELECT id, competition, season, variant, n, accuracy, log_loss, "
                "rps, corpus_db, corpus_csv, verdict, created_at, "
                "exact_n, exact_hits, exact_rate, window_start, window_end, n_new "
                "FROM backtest_runs WHERE competition=? ORDER BY id DESC LIMIT ?",
                (str(competition), int(limit))))
        except Exception as exc:
            log.warning("读取回测历史失败：%s", exc)
            return []
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        """命中率统计：总命中、连胜/连败、各信心等级 / 预测方向 / 联赛命中率。

        by_result 与 by_league 是后加的两个细分维度：只有总命中率看不出模型
        偏在哪（主胜准而平局不准时，总分会被拉到中间值）。两者恒定存在——
        空库时是空字典而不是缺失键，调用方不必做 hasattr 防御。
        """
        try:
            conn = self._connect()
            rows = list(conn.execute(
                "SELECT level_key, result, league, actual_home, actual_away, kickoff, "
                "home_prob, draw_prob, away_prob "
                "FROM predictions WHERE actual_home IS NOT NULL AND actual_away IS NOT NULL "
                "ORDER BY kickoff"
            ))
        except Exception as exc:
            log.warning("统计命中率失败：%s", exc)
            return {"total": 0, "hit": 0, "rate": None, "streak": 0, "by_level": {},
                    "by_result": {}, "by_league": {},
                    "pending": 0, "persistent": self.persistent,
                    "brier": None, "logloss": None, "scored": 0}

        total = hit = 0
        streak = 0
        scored = 0
        brier_sum = 0.0
        logloss_sum = 0.0
        by_level: dict[str, dict[str, Any]] = {}
        by_result: dict[str, dict[str, Any]] = {}
        by_league: dict[str, dict[str, Any]] = {}
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
            # 预测方向维度：result 为空（老记录/降级路径）时不计入，
            # 但这场仍计入总场次——宁可方向维度少一场，也不能总数对不上。
            if r["result"]:
                slot_r = by_result.setdefault(r["result"], {"total": 0, "hit": 0})
                slot_r["total"] += 1
                slot_r["hit"] += 1 if ok else 0
            # 联赛维度：缺失归「其它」而不是丢弃，理由同上（各项之和须等于总数）
            lg = (r["league"] or "").strip() or "其它"
            slot_l = by_league.setdefault(lg, {"total": 0, "hit": 0})
            slot_l["total"] += 1
            slot_l["hit"] += 1 if ok else 0
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

        # 两个细分维度补 rate：与顶层 rate 同一口径（0~1 小数），
        # 让视图层不必再自己算一遍除法。
        for slot in list(by_result.values()) + list(by_league.values()):
            slot["rate"] = (slot["hit"] / slot["total"]) if slot["total"] else None

        return {
            "total": total,
            "hit": hit,
            "rate": (hit / total) if total else None,
            "streak": streak,
            "by_level": by_level,
            "by_result": by_result,
            "by_league": by_league,
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

    def save_matches(self, competition: str, matches: list[dict[str, Any]],
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

    def latest_finished(self, competition: str = "", limit: int = 1) -> list[dict[str, Any]]:
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

    def finished_without_stats(self, competition: str = "", limit: int = 50) -> list[dict[str, Any]]:
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
            params: list[Any] = []
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
                     *, limit: int = 500) -> list[dict[str, Any]]:
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

    def upcoming_matches(self, day_from: str, day_to: str,
                         *, limit: int = 500) -> list[dict[str, Any]]:
        """按**日期**（YYYY-MM-DD）粗筛本地赛程，返回保存时的原始 fixture 结构。

        为什么只按日期过滤、不按精确时刻过滤：
            `utc_date` 是文本，两种数据源的后缀不一致（`+00:00` / `Z`），
            字符串区间比较会错序——看起来是同一天的比赛可能被漏掉。
            精确的开赛时刻窗口交给上层用 parse_kickoff 解析后判断，
            这里只负责把候选范围缩小到「今天/明天」这几百行。

        不在这里过滤 status：
            已完场的比赛由上层按解析出的开赛时间排除（`now < kickoff`），
            SQL 层判断状态反而会把「推迟但尚未开赛」的比赛误杀。
        """
        try:
            conn = self._connect()
            rows = list(conn.execute(
                """SELECT raw_json FROM matches
                   WHERE substr(utc_date,1,10) BETWEEN ? AND ?
                   ORDER BY utc_date LIMIT ?""",
                (str(day_from), str(day_to), int(limit)),
            ))
        except Exception as exc:
            log.warning("读取本地赛程失败：%s", exc)
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r["raw_json"]))
            except (ValueError, TypeError):
                continue
        return out

    def matches_with_predictions(self, day: str, *, limit: int = 300,
                                 competitions: tuple[str, ...] | None = None
                                 ) -> list[dict[str, Any]]:
        """指定日期（YYYY-MM-DD）的比赛，左连已生成的预测。

        为什么连表而不是让上层自己拼：predictions 的键是 API fixture_id 的
        **文本**形式，matches 的主键是**整数** id，两者写法还不一致
        （"fd-123" vs 123）。上层各自拼接极易漏配，这里统一 CAST 后比较。

        未生成预测的比赛 `prediction` 为 None —— 调用方据此显示「未预测」，
        而不是假装成 0%，避免把「没算过」误读成「算出来是 0」。

        competitions 非空时只返回这些分区代码的比赛。默认 None = 不过滤，
        行为与加参数前完全一致（既有调用方不受影响）。

        为什么要这层过滤：本地库里存着历史同步进来的所有比赛，而自动预测
        只跑配置的联赛。不过滤的话，看板会列出从未生成过预测的比赛，
        每条都恒显示「尚未生成预测」——看起来像预测坏了，实际是这批比赛
        根本不在预测范围内。
        """
        where = "substr(m.utc_date,1,10)=?"
        params: list[Any] = [str(day)]
        if competitions:
            codes = [str(c) for c in competitions if str(c)]
            if codes:
                where += " AND m.competition_code IN ({})".format(
                    ",".join("?" * len(codes)))
                params.extend(codes)
        params.append(int(limit))
        try:
            conn = self._connect()
            rows = list(conn.execute(
                """SELECT m.id AS match_id, m.competition_code AS comp,
                          m.utc_date AS kickoff, m.status AS status,
                          m.matchday AS matchday,
                          m.home_team_id AS home_id, m.away_team_id AS away_id,
                          m.home_team_name AS home, m.away_team_name AS away,
                          m.home_score AS hs, m.away_score AS aws,
                          p.result AS pred_result, p.home_prob, p.draw_prob,
                          p.away_prob, p.level_key,
                          p.actual_home, p.actual_away
                   FROM matches m
                   LEFT JOIN predictions p ON p.fixture_id = CAST(m.id AS TEXT)
                   WHERE """ + where + """
                   ORDER BY m.competition_code, m.utc_date
                   LIMIT ?""",
                tuple(params),
            ))
        except Exception as exc:
            log.warning("读取当日赛程失败：%s", exc)
            return []
        out: list[dict[str, Any]] = []
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
                "matchday": r["matchday"],
                "home_id": r["home_id"],
                "away_id": r["away_id"],
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

    def recent_form(self, competition: str, before: str, *, per_team: int = 5) -> dict[str, dict[str, Any]]:
        """各队在 before（YYYY-MM-DD）**之前**的最后 per_team 场战绩。

        只统计已有比分的比赛，且严格早于 before —— 因此用它渲染未来赛程时，
        区间内尚未开踢的比赛不会被算进任何一队的状态里（无前视偏差）。
        比分缺失的场次整场跳过，不把 None 当成 0 参与统计。

        返回 {team_id: {"form": [{"r": "W"/"D"/"L", "gf": int, "ga": int,
                                  "home": bool, "date": str, "opp": str}],
                        "w": int, "d": int, "l": int, "gf": int, "ga": int}}
        form 按时间**由旧到新**排列；数据不足时列表更短，绝不补占位。
        """
        per_team = max(1, min(int(per_team), 20))
        try:
            conn = self._connect()
            rows = list(conn.execute(
                """SELECT home_team_id, home_team_name, away_team_id, away_team_name,
                          home_score, away_score, utc_date
                   FROM matches
                   WHERE competition_code=?
                     AND home_score IS NOT NULL AND away_score IS NOT NULL
                     AND substr(utc_date,1,10) < ?
                   ORDER BY utc_date, id""",
                (str(competition), str(before)),
            ))
        except Exception as exc:
            log.warning("读取近期战绩失败（competition=%s）：%s", competition, exc)
            return {}
        buf: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            hs, aws = r["home_score"], r["away_score"]
            sides = (
                (r["home_team_id"], hs, aws, True, r["away_team_name"]),
                (r["away_team_id"], aws, hs, False, r["home_team_name"]),
            )
            for tid, gf, ga, is_home, opp in sides:
                if not tid:
                    continue
                slot = buf.setdefault(str(tid), [])
                slot.append({
                    "r": "W" if gf > ga else ("L" if gf < ga else "D"),
                    "gf": int(gf), "ga": int(ga), "home": bool(is_home),
                    "date": (r["utc_date"] or "")[:10], "opp": opp or "?",
                })
                if len(slot) > per_team:
                    del slot[0]
        out: dict[str, dict[str, Any]] = {}
        for tid, items in buf.items():
            out[tid] = {
                "form": items,
                "w": sum(1 for x in items if x["r"] == "W"),
                "d": sum(1 for x in items if x["r"] == "D"),
                "l": sum(1 for x in items if x["r"] == "L"),
                "gf": sum(x["gf"] for x in items),
                "ga": sum(x["ga"] for x in items),
            }
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

    # ---- 赛前信号对照（signal_ab）------------------------------------------
    # 设计要点见 signal_ab 模块顶部：一行 = 一场比赛 × 一种信号类型，
    # 重复触发 UPDATE 同一行，信号消失标 revoked，算不出 signal 版标
    # partial_failure。写入失败一律只告警，绝不阻断预测主流程。
    def save_signal_ab(
        self,
        *,
        fixture_id: Any,
        signal_type: str,
        pure_baseline: Any = None,
        signal_prediction: Any = None,
        partial_baseline: Any = None,
        signal_strength: Any = None,
        signal_strength_norm: Any = None,
        change_type: str = "applied",
        human_note: str = "",
        error: str | None = None,
        config_snapshot: Any = None,
        is_test: bool = False,
    ) -> bool:
        """写入/更新一条对照记录（按 fixture_id + signal_type 唯一）。

        返回是否成功。任何异常都被吞掉并告警 —— 对照是为了以后能变好，
        不能因为它挂了导致今天的预测发不出去。
        """
        from signal_ab import (STATUS_OK, STATUS_PARTIAL_FAILURE, VALID_CHANGE_TYPES,
                               VALID_STATUSES, _dump_probs, utc_now_iso)
        try:
            if change_type not in VALID_CHANGE_TYPES:
                change_type = "applied"
            status = STATUS_OK if error is None else STATUS_PARTIAL_FAILURE
            if status not in VALID_STATUSES:
                status = STATUS_OK
            now = utc_now_iso()
            snap = config_snapshot
            if snap is not None and not isinstance(snap, str):
                try:
                    snap = json.dumps(snap, ensure_ascii=False)
                except (TypeError, ValueError):
                    snap = None
            conn = self._connect()
            conn.execute(
                """INSERT INTO signal_ab
                   (fixture_id, signal_type, signal_strength, signal_strength_norm,
                    change_type, pure_baseline, partial_baseline, signal_prediction,
                    human_note, status, error, is_processed, is_archived, is_test,
                    config_snapshot, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?)
                   ON CONFLICT(fixture_id, signal_type) DO UPDATE SET
                     signal_strength=excluded.signal_strength,
                     signal_strength_norm=excluded.signal_strength_norm,
                     change_type=excluded.change_type,
                     pure_baseline=excluded.pure_baseline,
                     partial_baseline=excluded.partial_baseline,
                     signal_prediction=excluded.signal_prediction,
                     human_note=excluded.human_note,
                     status=excluded.status,
                     error=excluded.error,
                     is_test=excluded.is_test,
                     config_snapshot=excluded.config_snapshot,
                     updated_at=excluded.updated_at""",
                (str(fixture_id), str(signal_type), signal_strength,
                 signal_strength_norm, change_type,
                 _dump_probs(pure_baseline), _dump_probs(partial_baseline),
                 _dump_probs(signal_prediction), human_note or "",
                 status, error, 1 if is_test else 0, snap, now, now),
            )
            conn.commit()
            return True
        except Exception as exc:
            log.warning("写入信号对照失败（fixture=%s type=%s）：%s",
                        fixture_id, signal_type, exc)
            return False

    def signal_ab_rows(self, *, settled_only: bool = False,
                       signal_type: str | None = None,
                       include_test: bool = False,
                       include_archived: bool = False,
                       limit: int = 5000) -> list[sqlite3.Row]:
        """读取对照记录。默认过滤测试数据与已归档数据。"""
        try:
            conn = self._connect()
            sql = "SELECT * FROM signal_ab WHERE 1=1"
            args: list[Any] = []
            if not include_test:
                sql += " AND is_test=0"
            if not include_archived:
                sql += " AND is_archived=0"
            if settled_only:
                sql += " AND actual_home IS NOT NULL AND actual_away IS NOT NULL"
            if signal_type:
                sql += " AND signal_type=?"
                args.append(signal_type)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            args.append(int(limit))
            return list(conn.execute(sql, args))
        except Exception as exc:
            log.warning("读取信号对照失败：%s", exc)
            return []

    def signal_ab_for_fixture(self, fixture_id: Any,
                              include_test: bool = False) -> list[sqlite3.Row]:
        """某场比赛的全部对照记录（调试用：一场比赛会触发多种信号）。"""
        try:
            conn = self._connect()
            sql = "SELECT * FROM signal_ab WHERE fixture_id=?"
            args: list[Any] = [str(fixture_id)]
            if not include_test:
                sql += " AND is_test=0"
            sql += " ORDER BY signal_type"
            return list(conn.execute(sql, args))
        except Exception as exc:
            log.warning("读取单场信号对照失败（fixture=%s）：%s", fixture_id, exc)
            return []

    def settle_signal_ab(self, rows: list[tuple[Any, int, int]]) -> int:
        """回填真实比分。rows = [(fixture_id, home, away), ...]。

        只更新尚未结算的记录，返回实际更新行数。回填失败不抛异常。
        """
        if not rows:
            return 0
        from signal_ab import utc_now_iso
        now = utc_now_iso()
        updated = 0
        try:
            conn = self._connect()
            for fixture_id, home, away in rows:
                try:
                    h, a = int(home), int(away)
                except (TypeError, ValueError):
                    continue
                cur = conn.execute(
                    "UPDATE signal_ab SET actual_home=?, actual_away=?, "
                    "settled_at=?, is_processed=1 "
                    "WHERE fixture_id=? AND actual_home IS NULL",
                    (h, a, now, str(fixture_id)))
                updated += cur.rowcount or 0
            conn.commit()
        except Exception as exc:
            log.warning("回填信号对照比分失败：%s", exc)
        return updated

    def unsettled_signal_ab(self, limit: int = 500) -> list[sqlite3.Row]:
        """待回填的对照记录（用于重试队列）。"""
        try:
            conn = self._connect()
            return list(conn.execute(
                "SELECT * FROM signal_ab WHERE is_test=0 AND is_archived=0 "
                "AND (actual_home IS NULL OR actual_away IS NULL) "
                "ORDER BY updated_at DESC LIMIT ?", (int(limit),)))
        except Exception as exc:
            log.warning("读取待回填对照失败：%s", exc)
            return []

    def log_signal_weight(self, *, signal_type: str, old_weight: Any,
                          new_weight: Any, applied: bool,
                          sample_from: str | None = None,
                          sample_to: str | None = None,
                          n_samples: int | None = None,
                          metric: str | None = None,
                          reason: str = "") -> bool:
        """追加一条权重变更审计日志。只追加、永不覆盖已有记录。"""
        from signal_ab import utc_now_iso
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO signal_weight_log"
                "(signal_type, old_weight, new_weight, applied, sample_from, "
                " sample_to, n_samples, metric, reason, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(signal_type), old_weight, new_weight, 1 if applied else 0,
                 sample_from, sample_to, n_samples, metric, reason, utc_now_iso()))
            conn.commit()
            return True
        except Exception as exc:
            log.warning("写入权重审计日志失败：%s", exc)
            return False

    def signal_weight_history(self, signal_type: str | None = None,
                              limit: int = 100) -> list[sqlite3.Row]:
        try:
            conn = self._connect()
            if signal_type:
                return list(conn.execute(
                    "SELECT * FROM signal_weight_log WHERE signal_type=? "
                    "ORDER BY id DESC LIMIT ?", (signal_type, int(limit))))
            return list(conn.execute(
                "SELECT * FROM signal_weight_log ORDER BY id DESC LIMIT ?",
                (int(limit),)))
        except Exception as exc:
            log.warning("读取权重历史失败：%s", exc)
            return []


    def save_config_snapshot(self, *, version: str, payload: Any,
                             created_at: str) -> bool:
        """记录发布级配置快照。只追加，永不覆盖。"""
        try:
            text = payload if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False)
            conn = self._connect()
            conn.execute(
                "INSERT INTO config_snapshot(version, payload, created_at) "
                "VALUES(?,?,?)", (version, text, created_at))
            conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("写入配置快照失败：%s", exc)
            return False

    def config_snapshots(self, limit: int = 50) -> list[sqlite3.Row]:
        try:
            conn = self._connect()
            return list(conn.execute(
                "SELECT * FROM config_snapshot ORDER BY id DESC LIMIT ?",
                (int(limit),)))
        except Exception as exc:  # noqa: BLE001
            log.warning("读取配置快照失败：%s", exc)
            return []

    def signal_ab_view(self, *, signal_type: str | None = None,
                       include_test: bool = False,
                       limit: int = 5000) -> list[sqlite3.Row]:
        """读 v_signal_ab 视图（概率已展开成数值列，供导出与外部分析）。

        视图不存在时（极老的库）回退到直接查表，绝不因为视图缺失就失败。
        """
        try:
            conn = self._connect()
            sql = "SELECT * FROM v_signal_ab WHERE 1=1"
            args: list[Any] = []
            if not include_test:
                sql += " AND is_test=0"
            if signal_type:
                sql += " AND signal_type=?"
                args.append(signal_type)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            args.append(int(limit))
            return list(conn.execute(sql, args))
        except Exception as exc:  # noqa: BLE001
            log.warning("读取对照视图失败，回退查表：%s", exc)
            return self.signal_ab_rows(signal_type=signal_type,
                                       include_test=include_test, limit=limit)


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


def _extract_match(m: dict[str, Any]) -> dict:
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
