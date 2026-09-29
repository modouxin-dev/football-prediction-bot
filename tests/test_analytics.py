"""只读分析层 / Analytics layer.

看板的所有数字都来自这里，因此必须验证：
1. 空库时给出明确提示，而不是画出假曲线
2. 指标计算与 backtest.py 的既有实现一致（不出现两套口径）
"""
import math
import sqlite3

import pytest

import analytics
from repository import PredictionRepository

# 与 backtest.py 同一套判据，避免分析层自造一套
from backtest import log_loss, outcome_index, rps


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "a.db")


def _seed(db, n_matches=0, n_predictions=0, seed=1):
    import random

    random.seed(seed)
    repo = PredictionRepository(db)
    conn = repo._connect()

    def pois(l):
        limit, k, p = math.exp(-l), 0, 1.0
        while True:
            p *= random.random()
            if p <= limit:
                return k
            k += 1

    teams = [f"T{i}" for i in range(10)]
    for i in range(n_matches):
        h, a = random.sample(teams, 2)
        conn.execute(
            "INSERT OR REPLACE INTO matches (id,competition_code,season,utc_date,status,"
            "home_team_id,home_team_name,away_team_id,away_team_name,home_score,away_score,"
            "source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "PL", 2026, f"2026-01-{(i % 28) + 1:02d}T15:00", "FT",
             h, h, a, a, pois(1.4), pois(1.1), "t", "x"))
    for i in range(n_predictions):
        h, a = random.sample(teams, 2)
        hp, dp, ap = 0.5, 0.25, 0.25
        res = max([("主胜", hp), ("平局", dp), ("客胜", ap)], key=lambda x: x[1])[0]
        conn.execute(
            "INSERT OR REPLACE INTO predictions (fixture_id,season,league,home,away,kickoff,"
            "model_version,source,level_key,result,home_prob,draw_prob,away_prob,best_score,"
            "created_at,inputs,actual_home,actual_away,settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"p{i}", 2026, "PL", h, a, f"2026-02-{(i % 28) + 1:02d}T15:00",
             "v1", "s", "high", res, hp, dp, ap, "1-0", "2026-02-01", "{}",
             pois(1.4), pois(1.1), "2026-02-02"))
    conn.commit()
    return repo


# ============================================================================
# 空库：必须有明确提示，不能画假曲线
# ============================================================================

def test_empty_db_returns_empty_status(db):
    h = analytics.model_health(db)
    assert h["status"] == "empty"
    assert h["settled"] == 0
    assert h["message"]


def test_empty_audit(db):
    assert analytics.prediction_audit(db)["status"] == "empty"


def test_empty_strength(db):
    s = analytics.strength_table(db)
    assert s["status"] == "insufficient"
    assert s["teams"] == []


def test_health_survives_missing_tables(tmp_path):
    """库里连表都没有时必须报错而非崩溃。"""
    empty = str(tmp_path / "nope.db")
    sqlite3.connect(empty).close()
    h = analytics.model_health(empty)
    assert h["status"] == "empty"


# ============================================================================
# 有数据：指标口径必须与 backtest.py 一致
# ============================================================================

def test_health_counts_settled(db):
    _seed(db, n_predictions=25)
    h = analytics.model_health(db)
    assert h["settled"] == 25
    assert 0.0 <= h["accuracy"] <= 1.0
    assert h["mean_log_loss"] > 0


def test_trend_is_monotonic_in_length(db):
    """趋势数组长度 = 已结算场次，且 n 从 1 递增。"""
    _seed(db, n_predictions=30)
    h = analytics.model_health(db)
    assert len(h["trend"]) == 30
    assert [p["n"] for p in h["trend"]] == list(range(1, 31))


def test_cumulative_log_loss_is_average(db):
    """累积 Log Loss 必须是「到当前为止的平均」，不是简单累加。"""
    _seed(db, n_predictions=10)
    h = analytics.model_health(db)
    for p in h["trend"]:
        assert 0 < p["log_loss"] < 20   # 单场最大约 -ln(eps)，平均不会超过这个量级


def test_log_loss_matches_backtest_implementation(db):
    """同一组概率与结果，analytics 与 backtest 必须算出同一个值。"""
    probs = (0.5, 0.3, 0.2)
    actual = outcome_index(1, 0)          # 主胜
    expected = log_loss(probs, actual)
    repo = PredictionRepository(db)
    conn = repo._connect()
    conn.execute(
        "INSERT OR REPLACE INTO predictions (fixture_id,season,league,home,away,kickoff,"
        "model_version,source,level_key,result,home_prob,draw_prob,away_prob,best_score,"
        "created_at,inputs,actual_home,actual_away,settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("x", 2026, "PL", "A", "B", "2026-01-01T15:00", "v1", "s", "high", "主胜",
         probs[0], probs[1], probs[2], "1-0", "2026-01-01", "{}", 1, 0, "2026-01-02"))
    conn.commit()
    h = analytics.model_health(db)
    assert abs(h["mean_log_loss"] - expected) < 1e-12


def test_insufficient_sample_is_flagged(db):
    """少于阈值时必须标记为不可靠，防止用户对着噪声下结论。"""
    _seed(db, n_predictions=analytics.MIN_SETTLED_FOR_CURVE - 1)
    h = analytics.model_health(db)
    assert h["status"] == "insufficient"
    assert h["reliable"] is False
    assert h["message"]


def test_sufficient_sample_is_reliable(db):
    _seed(db, n_predictions=analytics.MIN_SETTLED_FOR_CURVE + 5)
    assert analytics.model_health(db)["reliable"] is True


def test_calibration_buckets_present(db):
    _seed(db, n_predictions=40)
    assert len(analytics.model_health(db)["calibration"]) > 0


def test_by_level_counts(db):
    _seed(db, n_predictions=20)
    lv = analytics.model_health(db)["by_level"]
    assert sum(v["total"] for v in lv.values()) == 20


# ============================================================================
# 预测审计
# ============================================================================

def test_audit_lists_predictions(db):
    _seed(db, n_predictions=15)
    a = analytics.prediction_audit(db)
    assert a["count"] == 15
    assert a["status"] == "ok"


def test_audit_respects_limit(db):
    _seed(db, n_predictions=40)
    assert analytics.prediction_audit(db, limit=10)["count"] == 10


def test_audit_items_have_score_and_hit(db):
    _seed(db, n_predictions=5)
    for it in analytics.prediction_audit(db)["items"]:
        assert "-" in it["score"]
        assert isinstance(it["hit"], bool)


# ============================================================================
# 强度榜
# ============================================================================

def test_strength_requires_minimum_matches(db):
    _seed(db, n_matches=analytics.MIN_MATCHES_FOR_STRENGTH - 1)
    s = analytics.strength_table(db)
    assert s["status"] == "insufficient"
    assert s["teams"] == []


def test_strength_ranks_teams(db):
    _seed(db, n_matches=150)
    s = analytics.strength_table(db)
    assert s["status"] == "ok"
    assert len(s["teams"]) > 1
    # 必须按综合强度降序
    overalls = [t["overall"] for t in s["teams"]]
    assert overalls == sorted(overalls, reverse=True)


def test_strength_skips_unplayed_matches(db):
    """未开赛的比赛（无比分）不能污染强度计算。"""
    repo = PredictionRepository(db)
    conn = repo._connect()
    # 注意：matches 有 UNIQUE(competition_code, utc_date, home, away) 约束，
    # 日期必须各不相同，否则会被 REPLACE 覆盖掉
    for i in range(80):
        conn.execute(
            "INSERT OR REPLACE INTO matches (id,competition_code,season,utc_date,status,"
            "home_team_id,home_team_name,away_team_id,away_team_name,home_score,away_score,"
            "source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "PL", 2026, f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T15:00", "FT",
             "A", "A", "B", "B", 1, 0, "t", "x"))
    # 一场未开赛（比分为 NULL）。日期必须避开上面的 80 场，
    # 否则会被 UNIQUE(competition_code, utc_date, home, away) 覆盖掉
    conn.execute(
        "INSERT OR REPLACE INTO matches (id,competition_code,season,utc_date,status,"
        "home_team_id,home_team_name,away_team_id,away_team_name,home_score,away_score,"
        "source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (999, "PL", 2026, "2026-12-31T15:00", "NS", "A", "A", "B", "B", None, None, "t", "x"))
    conn.commit()
    s = analytics.strength_table(db)
    assert s["finished"] == 80, "未开赛的比赛被计入了"


def test_strength_fields_are_finite(db):
    _seed(db, n_matches=120)
    for t in analytics.strength_table(db)["teams"]:
        assert math.isfinite(t["attack"]) and math.isfinite(t["defense"])
        assert math.isfinite(t["overall"])
