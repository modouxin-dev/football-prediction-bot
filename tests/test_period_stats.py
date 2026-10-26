"""分时间窗口的胜率 / 平均赔率 / ROI 统计。

存在理由
--------
只有总命中率看不出趋势：模型近期是否走坏、命中率是不是靠高赔冷门堆出来的，
都必须分窗口看。而赔率与 ROI 是「这套推荐到底赚不赚」的唯一直接答案。

为什么必须落库赔率
------------------
赔率是**当时的市场报价**，事后拉不到（历史盘口不保留，整季重拉既烧额度
又拿不到旧价）。所以 inputs.odds 是赔率类统计的唯一数据源。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from repository import PredictionRepository


def _mk(tmp_path):
    return PredictionRepository(str(tmp_path / "t.db"))


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _add(repo, fid, days_ago, result, home, away, odds=None):
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions (fixture_id, home, away, league, kickoff, "
        "result, home_prob, draw_prob, away_prob, best_score, created_at, "
        "actual_home, actual_away, inputs) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, "A", "B", "L", _days_ago(days_ago), result, 0.5, 0.3, 0.2, "1-0",
         _days_ago(days_ago), home, away,
         json.dumps({"odds": odds}) if odds else None))
    conn.commit()


def test_period_stats_empty(tmp_path):
    """空库不报错，rate 为 None 而不是 0——0 会被读成「一场没中」。"""
    r = _mk(tmp_path).period_stats(7)
    assert r["total"] == 0
    assert r["rate"] is None
    assert r["roi"] is None


def test_period_stats_only_counts_window(tmp_path):
    """窗口外的不计入——这是「周/月」统计的定义。"""
    repo = _mk(tmp_path)
    _add(repo, "f1", 2, "主胜", 1, 0, {"home": 2.0, "draw": 3.0, "away": 4.0})
    _add(repo, "f2", 60, "主胜", 1, 0, {"home": 2.0, "draw": 3.0, "away": 4.0})
    assert repo.period_stats(7)["total"] == 1
    assert repo.period_stats(7)["hit"] == 1


def test_period_stats_rate_and_avg_odds(tmp_path):
    """命中率与平均赔率分开算——高命中+低赔未必赚。"""
    repo = _mk(tmp_path)
    _add(repo, "f1", 1, "主胜", 1, 0, {"home": 2.0, "draw": 3.0, "away": 4.0})
    _add(repo, "f2", 2, "主胜", 0, 1, {"home": 4.0, "draw": 3.0, "away": 2.0})
    r = repo.period_stats(7)
    assert r["total"] == 2
    assert r["hit"] == 1
    assert r["rate"] == 0.5
    assert r["avg_odds"] == 3.0      # (2.0 + 4.0) / 2


def test_period_stats_roi(tmp_path):
    """ROI = (返还 − 本金) / 本金。押 1 单位：中 2.0 赚 1，中 4.0 赚 3。"""
    repo = _mk(tmp_path)
    _add(repo, "f1", 1, "主胜", 1, 0, {"home": 2.0, "draw": 3.0, "away": 4.0})
    _add(repo, "f2", 2, "主胜", 0, 1, {"home": 4.0, "draw": 3.0, "away": 2.0})
    r = repo.period_stats(7)
    # 本金 2（两场都有赔率），返还 2.0（只第一场中）
    assert r["stake"] == 2
    assert r["roi"] == pytest.approx((2.0 - 2.0) / 2.0)


def test_period_stats_roi_positive(tmp_path):
    """押中高赔 = 正 ROI：这是「命中率不高但赚钱」的情形。"""
    repo = _mk(tmp_path)
    _add(repo, "f1", 1, "主胜", 1, 0, {"home": 5.0, "draw": 3.0, "away": 2.0})
    _add(repo, "f2", 2, "主胜", 0, 1, {"home": 5.0, "draw": 3.0, "away": 2.0})
    r = repo.period_stats(7)
    assert r["rate"] == 0.5          # 命中率只有 50%
    assert r["roi"] == pytest.approx((5.0 - 2.0) / 2.0)   # 但 ROI +150%


def test_period_stats_excludes_no_odds_from_roi(tmp_path):
    """无赔率的场次剔除出 ROI，单独计数——不做平均赔率填充。"""
    repo = _mk(tmp_path)
    _add(repo, "f1", 1, "主胜", 1, 0, {"home": 2.0, "draw": 3.0, "away": 4.0})
    _add(repo, "f2", 2, "主胜", 1, 0, None)      # 老记录，没赔率
    r = repo.period_stats(7)
    assert r["total"] == 2
    assert r["no_odds"] == 1
    assert r["stake"] == 1                        # ROI 只按 1 场算
    assert r["roi"] == pytest.approx((2.0 - 1.0) / 1.0)


def test_period_stats_all_no_odds_roi_is_none(tmp_path):
    """全无赔率时 ROI 为 None 而不是 0——0 会被读成「打平」。"""
    repo = _mk(tmp_path)
    _add(repo, "f1", 1, "主胜", 1, 0, None)
    r = repo.period_stats(7)
    assert r["roi"] is None
    assert r["rate"] == 1.0          # 命中率照样能算


def test_odds_of_rejects_dirty_values():
    """赔率 ≤1 是脏数据，必须拒。"""
    from repository import _odds_of

    assert _odds_of(json.dumps({"odds": {"home": 2.5}}), "主胜") == 2.5
    assert _odds_of(json.dumps({"odds": {"home": 1.0}}), "主胜") is None
    assert _odds_of(json.dumps({"odds": {"home": 0.5}}), "主胜") is None
    assert _odds_of(json.dumps({}), "主胜") is None
    assert _odds_of(None, "主胜") is None
    assert _odds_of(json.dumps({"odds": {"home": 2.0}}), None) is None
    assert _odds_of("not-json", "主胜") is None
    # 英文写法也要兼容（历史数据可能两种都有）
    assert _odds_of(json.dumps({"odds": {"home": 2.5}}), "home") == 2.5


def test_odds_of_matches_result_direction():
    """取的是推荐**方向对应的**赔率，不是主胜赔率。"""
    from repository import _odds_of

    raw = json.dumps({"odds": {"home": 2.0, "draw": 3.0, "away": 4.0}})
    assert _odds_of(raw, "主胜") == 2.0
    assert _odds_of(raw, "平局") == 3.0
    assert _odds_of(raw, "客胜") == 4.0
