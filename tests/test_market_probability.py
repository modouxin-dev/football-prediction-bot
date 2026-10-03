"""市场去水概率 / de-watered market probabilities

覆盖 analyzer.implied_probabilities / analyzer.market_gap 以及预测卡里的
「模型 vs 市场 · 去水后」表格。

为什么单独建文件：这是本项目第一次出现「两个赔率口径」——
    · edge（含抽水）= 模型概率 − 1/赔率，等价于 EV，用于 Value Bet 判据
    · 去水概率     = (1/赔率) / Σ(1/赔率)，用于与市场做公平对比
两者混用会让「模型比市场强多少」被系统性夸大，必须有测试把边界钉死。
"""

from __future__ import annotations

import pytest

from analyzer import implied_probabilities, market_gap, overround


# ---- implied_probabilities ------------------------------------------------

def test_standard_odds_sum_to_one():
    """标准英超盘：去水后三项概率之和恰为 1。"""
    probs = implied_probabilities({"home": 1.91, "draw": 3.60, "away": 4.20})
    assert probs
    assert sum(probs.values()) == pytest.approx(1.0)


def test_fair_odds_are_unchanged():
    """无抽水盘（Σ1/o = 1）：归一化不改变数值。"""
    probs = implied_probabilities({"home": 2.0, "draw": 3.0, "away": 6.0})
    assert probs["home"] == pytest.approx(0.5)
    assert probs["draw"] == pytest.approx(1 / 3)
    assert probs["away"] == pytest.approx(1 / 6)


def test_dewatered_is_lower_than_raw():
    """核心性质：含抽水时，去水概率恒 ≤ 原始 1/赔率。

    这正是旧实现的偏差来源——把市场概率算高了。
    """
    odds = {"home": 1.91, "draw": 3.60, "away": 4.20}
    probs = implied_probabilities(odds)
    for key in ("home", "draw", "away"):
        assert probs[key] <= 1.0 / odds[key]
    assert overround(odds) > 0  # 确认真的是含抽水盘


def test_missing_key_invalidates_whole_board():
    """缺任一项 → 整盘作废，不做部分计算。"""
    assert implied_probabilities({"home": 1.91, "draw": 3.60}) == {}
    assert implied_probabilities({"home": None, "draw": 3.60, "away": 4.20}) == {}


def test_price_le_one_invalidates_whole_board():
    """赔率 ≤ 1（脏数据/已结算盘口）→ 整盘作废。"""
    assert implied_probabilities({"home": 1.0, "draw": 3.60, "away": 4.20}) == {}
    assert implied_probabilities({"home": 0.5, "draw": 3.60, "away": 4.20}) == {}


def test_string_price_is_accepted():
    """字符串赔率（CSV/表单来源）能正常参与计算。"""
    probs = implied_probabilities({"home": "1.91", "draw": "3.60", "away": "4.20"})
    assert sum(probs.values()) == pytest.approx(1.0)


def test_nan_price_invalidates_whole_board():
    """NaN 不能蒙混过关：它既 > 1.0 也不是 None，会把整盘概率污染成 NaN。

    没有这条守卫时，1/nan = nan，sum 也是 nan，而 nan <= 0 为 False，
    于是函数会「成功」返回一组 NaN 概率，界面上显示成 nan%。
    """
    nan = float("nan")
    assert implied_probabilities({"home": nan, "draw": 3.60, "away": 4.20}) == {}


def test_empty_input_returns_empty():
    assert implied_probabilities(None) == {}
    assert implied_probabilities({}) == {}


# ---- market_gap -----------------------------------------------------------

def test_gap_is_model_minus_market():
    """差值方向：正数 = 模型比市场更看好。"""
    odds = {"home": 2.0, "draw": 3.0, "away": 6.0}
    model = {"home": 0.60, "draw": 0.25, "away": 0.15}
    gap = market_gap(model, odds)
    assert gap["home"] == pytest.approx(0.60 - 0.50)
    assert gap["away"] == pytest.approx(0.15 - 1 / 6)


def test_gap_empty_when_odds_invalid():
    assert market_gap({"home": 0.5, "draw": 0.3, "away": 0.2}, {}) == {}


def test_gap_magnitude_smaller_than_raw_edge():
    """去水口径的差值绝对值 ≤ 含抽水口径（edge = p − 1/o）。

    含抽水把市场概率抬高，于是 p − 市场 被压得更低（或更负）。
    对「模型更看好」的一侧，去水差值必然 ≥ 含抽水差值。
    """
    odds = {"home": 1.91, "draw": 3.60, "away": 4.20}
    model = {"home": 0.60, "draw": 0.25, "away": 0.15}
    gap = market_gap(model, odds)
    raw_edge = model["home"] - 1.0 / odds["home"]
    assert gap["home"] > raw_edge


# ---- 视图渲染 -------------------------------------------------------------

def _fake_prediction(odds=None, bookmakers=None):
    """构造一个最小可用的 Prediction 替身，只喂 format_odds_detail 需要的字段。"""
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    analysis = {
        "win_prob": 0.54,
        "draw_prob": 0.25,
        "loss_prob": 0.21,
    }
    return SimpleNamespace(
        home="Arsenal",
        away="Chelsea",
        kickoff=datetime(2026, 10, 4, 19, 30, tzinfo=ZoneInfo("UTC")),
        season="2026",
        odds=odds,
        bookmakers=bookmakers or [],
        outcomes={},
        analysis=analysis,
    )


def test_odds_detail_renders_dewatered_table():
    """赔率对比卡必须出现去水口径的三行表。"""
    from views.prediction import PredictionView

    from zoneinfo import ZoneInfo

    odds = {"home": 1.91, "draw": 3.60, "away": 4.20, "n": 3}
    p = _fake_prediction(odds=odds)
    text = PredictionView.format_odds_detail(p, ZoneInfo("Asia/Shanghai"))
    assert "模型 vs 市场 · 去水后" in text
    assert "市场(去水)" in text
    assert "差值" in text
    # 三项表头齐全
    for label in ("主胜", "平局", "客胜"):
        assert label in text
    # 明确告知两种口径不可互换，避免读者拿去水差值去判断下注
    assert "不可互换" in text


def test_odds_detail_without_odds_still_safe():
    from zoneinfo import ZoneInfo

    from views.prediction import PredictionView

    text = PredictionView.format_odds_detail(
        _fake_prediction(odds=None), ZoneInfo("Asia/Shanghai")
    )
    assert "暂无赔率" in text
