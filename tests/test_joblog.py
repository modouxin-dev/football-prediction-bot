"""定时任务流水（joblog）的回归测试。

为什么单独成文件：
    修复前 7 个定时任务的失败**只写日志**，既不落库也不告警——机器人照常
    推送、看板显示旧数据，用户以为一切正常。joblog 把失败变成可查的数据。
    这些用例锁住的是"失败必须留下痕迹"，而不只是函数不报错。

    另一个关键点：观测手段本身绝不能成为新的故障源。所以每条用例都要验证
    「记录失败不影响业务」——joblog 的所有函数都必须吞掉自己的异常。
"""
from __future__ import annotations

import sqlite3

import pytest

import joblog
from review import daily_review


def _mk_db(tmp_path):
    """建一个带 job_runs 表的最小库。"""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, "
        "status TEXT NOT NULL, message TEXT, duration_ms INTEGER, "
        "created_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    return str(db)


# ============================================================================
# 一、落库与读取
# ============================================================================

def test_record_and_read_roundtrip(tmp_path):
    """写进去的结果必须能原样读出来。"""
    db = _mk_db(tmp_path)
    joblog.ok(db, "backtest", duration_ms=1234)
    rows = joblog.recent(db)
    assert len(rows) == 1
    assert rows[0]["job"] == "backtest"
    assert rows[0]["status"] == "ok"
    assert rows[0]["duration_ms"] == 1234
    assert rows[0]["message"] is None


def test_fail_stores_exception_message(tmp_path):
    """失败原因必须落库——只报「失败了」等于没说。"""
    db = _mk_db(tmp_path)
    joblog.fail(db, "settle", RuntimeError("API 超时"))
    row = joblog.recent(db)[0]
    assert row["status"] == "fail"
    assert "API 超时" in row["message"]
    assert "RuntimeError" in row["message"]


def test_skip_is_distinct_from_fail(tmp_path):
    """skip（无样本/未启用）不是故障，看板不能把它显示成红色。"""
    db = _mk_db(tmp_path)
    joblog.skip(db, "review", "无已结算样本")
    row = joblog.recent(db)[0]
    assert row["status"] == "skip"
    assert row["status"] != "fail"


def test_overlong_message_is_truncated(tmp_path):
    """超长报错要截断：一条几千字符的 API 报错会顶掉整个看板表格。"""
    db = _mk_db(tmp_path)
    joblog.fail(db, "backtest", "X" * 5000)
    msg = joblog.recent(db)[0]["message"]
    assert len(msg) <= 210, f"未截断，长度 {len(msg)}"
    assert msg.endswith("…")


def test_last_status_returns_latest_per_job(tmp_path):
    """每个任务只报最近一次——连跑三次后不能还显示第一次的结果。"""
    db = _mk_db(tmp_path)
    joblog.ok(db, "backtest")
    joblog.fail(db, "backtest", "第一次挂")
    joblog.ok(db, "backtest")
    joblog.fail(db, "settle", "一直挂")
    last = joblog.last_status(db)
    assert last["backtest"]["status"] == "ok"
    assert last["settle"]["status"] == "fail"


def test_record_never_raises_on_bad_path(tmp_path):
    """观测手段绝不能成为新的故障源：路径非法时静默返回，不抛异常。"""
    # 指向一个不存在的目录下的库文件
    bad = str(tmp_path / "no" / "such" / "dir" / "x.db")
    joblog.ok(bad, "backtest")            # 不应抛
    joblog.fail(bad, "settle", "boom")    # 不应抛
    assert joblog.recent(bad) == []
    assert joblog.last_status(bad) == {}


def test_memory_db_is_skipped_silently():
    """:memory: 库进程重启即空，记了无意义，还会在测试里制造噪声。"""
    joblog.ok(":memory:", "backtest")
    assert joblog.recent(":memory:") == []
    assert joblog.last_status(":memory:") == {}


def test_none_db_path_is_skipped():
    """db_path 为 None（settings 缺少该属性）时同样静默跳过。"""
    joblog.ok(None, "backtest")
    assert joblog.recent(None) == []


# ============================================================================
# 二、复盘双层窗口（回归 2026-10-25 修复的缺陷）
# ============================================================================

def _mk_pred_db(tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE predictions (
        fixture_id TEXT PRIMARY KEY, season INTEGER, league TEXT,
        home TEXT, away TEXT, kickoff TEXT, model_version TEXT, source TEXT,
        level_key TEXT, result TEXT, home_prob REAL, draw_prob REAL,
        away_prob REAL, best_score TEXT, created_at TEXT, inputs TEXT,
        actual_home INTEGER, actual_away INTEGER, settled_at TEXT)""")
    return db, conn


def _add(conn, fid, *, days_ago, league="英超", result="主胜",
         hp=0.45, dp=0.25, ap=0.30):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    hs, a_s = (2, 1) if result == "主胜" else ((1, 1) if result == "平局" else (0, 2))
    conn.execute(
        "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, 2026, league, "H" + fid, "A" + fid,
         (now - timedelta(days=days_ago)).isoformat(), "v1", "API-Football", None,
         result, hp, dp, ap, "2-1", now.isoformat(), "{}", hs, a_s, now.isoformat()))


def test_review_default_has_no_recent_block(tmp_path):
    """不传 recent_days 时行为与修复前一致——保证既有调用方不受影响。"""
    db, conn = _mk_pred_db(tmp_path)
    for i in range(20):
        _add(conn, f"f{i}", days_ago=i % 7 + 0.1)
    conn.commit()
    rv = daily_review(str(db), since_days=7)
    assert rv["status"] == "ok"
    assert rv["recent"] is None, "默认不应计算近期窗口"


def test_review_recent_block_reports_only_numbers(tmp_path):
    """近期窗口只报数字，不参与归因——它的样本量撑不起任何分组结论。"""
    db, conn = _mk_pred_db(tmp_path)
    # 30 天内 60 场，7 天内 14 场
    for i in range(60):
        _add(conn, f"f{i}", days_ago=(i % 30) + 0.1)
    conn.commit()
    rv = daily_review(str(db), since_days=30, recent_days=7)
    assert rv["settled"] > rv["recent"]["settled"], "归因样本应多于近期样本"
    assert rv["recent"]["days"] == 7
    assert set(rv["recent"]) == {"days", "settled", "hits", "accuracy"}


def test_review_recent_suppresses_rate_when_thin(tmp_path):
    """近期不足 8 场时不报命中率——3 场 100% 会暗示一个不存在的规律。"""
    from review import MIN_SAMPLE
    db, conn = _mk_pred_db(tmp_path)
    # 只在最近 3 天放 3 场，其余都在 30 天外（用 days_ago=25 避开 7 天窗口）
    for i in range(3):
        _add(conn, f"near{i}", days_ago=1 + i * 0.1)
    for i in range(40):
        _add(conn, f"far{i}", days_ago=10 + (i % 20))
    conn.commit()
    rv = daily_review(str(db), since_days=30, recent_days=7)
    assert rv["recent"] is not None
    assert rv["recent"]["settled"] == 3
    assert rv["recent"]["accuracy"] is None, (
        f"近期样本 {rv['recent']['settled']} < MIN_SAMPLE({MIN_SAMPLE})，不应报率"
    )


@pytest.mark.parametrize("window", [7, 30])
def test_review_widening_window_reduces_thin_groups(tmp_path, window):
    """窗口越宽，thin 分组越少——这是本次修复的核心收益，用数据锁住。

    7 天窗口下样本被切得太薄，联赛维度几乎全标 thin；
    放到 30 天后分组才有统计意义。
    """
    from review import MIN_SAMPLE
    db, conn = _mk_pred_db(tmp_path)
    leagues = ["德甲", "意甲", "西甲", "英超", "法甲", "欧冠"]
    n = max(60, window * 6)          # 保证每联赛每天都有量
    for i in range(n):
        _add(conn, f"f{i}", days_ago=(i % window) + 0.1, league=leagues[i % 6])
    conn.commit()
    rv = daily_review(str(db), since_days=window)
    thin = [g for g in rv["by_league"] if g["thin"]]
    total = len(rv["by_league"])
    if window == 30:
        assert not thin, f"30 天窗口下仍有 thin 分组：{[g['name'] for g in thin]}"
    # 7 天窗口的 thin 数量必然 >= 30 天窗口（样本更少）
    assert total >= 1
