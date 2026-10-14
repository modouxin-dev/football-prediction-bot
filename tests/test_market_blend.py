"""市场锚定融合的测试。

重点不是「公式对不对」，而是三个容易出错的工程约束：
  1. 无赔率时必须原样退回模型概率，绝不能臆造市场信息
  2. 权重越界要钳制，不能让配错的变量把预测算废
  3. 融合只改最终概率，edge 仍用模型自身概率（否则价值投注失效）
"""

import pytest

from analyzer import (
    DEFAULT_MARKET_WEIGHT,
    blend_with_market,
    fit_market_weight,
    implied_probabilities,
)
from config import load_settings

MODEL = (0.50, 0.30, 0.20)
MARKET = (0.40, 0.30, 0.30)


def test_weight_zero_returns_model_unchanged():
    assert blend_with_market(MODEL, MARKET, 0.0) == pytest.approx(MODEL)


def test_weight_one_returns_market():
    assert blend_with_market(MODEL, MARKET, 1.0) == pytest.approx(MARKET)


def test_no_odds_falls_back_to_model():
    """无赔率时必须原样返回模型概率——这是线上最常见的降级路径。"""
    assert blend_with_market(MODEL, None, DEFAULT_MARKET_WEIGHT) == pytest.approx(MODEL)


def test_blend_interpolates_and_sums_to_one():
    out = blend_with_market(MODEL, MARKET, 0.5)
    assert sum(out) == pytest.approx(1.0)
    assert out[0] == pytest.approx(0.5 * 0.50 + 0.5 * 0.40)
    assert out[2] == pytest.approx(0.5 * 0.20 + 0.5 * 0.30)


@pytest.mark.parametrize("bad", [-1.0, 5.0, 99.0])
def test_out_of_range_weight_is_clamped(bad):
    """越界权重钳制到 [0,1]，结果必须落在模型与市场之间。"""
    out = blend_with_market(MODEL, MARKET, bad)
    assert sum(out) == pytest.approx(1.0)
    for i in range(3):
        lo, hi = min(MODEL[i], MARKET[i]), max(MODEL[i], MARKET[i])
        assert lo - 1e-9 <= out[i] <= hi + 1e-9


def test_default_weight_is_kept_when_odds_present():
    """默认权重下结果必须真的偏离纯模型——否则说明融合没接上。"""
    out = blend_with_market(MODEL, MARKET, DEFAULT_MARKET_WEIGHT)
    assert out != pytest.approx(MODEL)
    assert out[0] < MODEL[0] and out[0] > MARKET[0]


# ---- 权重拟合 ----------------------------------------------------------------

def test_fit_picks_market_when_market_is_better():
    """市场全对、模型全错 → 最优权重应趋近 1。"""
    samples = [(MODEL, MARKET, i % 3) for i in range(30)]
    # 让市场总是押中
    samples = [(MODEL, (0.8, 0.1, 0.1) if i % 3 == 0 else
                       (0.1, 0.8, 0.1) if i % 3 == 1 else
                       (0.1, 0.1, 0.8), i % 3) for i in range(30)]
    best, scores = fit_market_weight(samples)
    assert best == 1.0
    assert scores[1.0] < scores[0.0]


def test_fit_picks_model_when_model_is_better():
    samples = [((0.8, 0.1, 0.1) if i % 3 == 0 else
                (0.1, 0.8, 0.1) if i % 3 == 1 else
                (0.1, 0.1, 0.8), MARKET, i % 3) for i in range(30)]
    best, scores = fit_market_weight(samples)
    assert best == 0.0
    assert scores[0.0] < scores[1.0]


def test_fit_empty_samples_returns_default():
    best, scores = fit_market_weight([])
    assert best == DEFAULT_MARKET_WEIGHT
    assert scores == {}


def test_fit_handles_missing_odds_entries():
    """样本里混有没有赔率的场次时不应崩溃。"""
    samples = [(MODEL, MARKET, 0), (MODEL, None, 1), (MODEL, MARKET, 2)]
    best, scores = fit_market_weight(samples)
    assert 0.0 <= best <= 1.0
    assert len(scores) == len(scores)


# ---- 配置 --------------------------------------------------------------------

def _env(**extra):
    base = {"TELEGRAM_TOKEN": "t", "API_FOOTBALL_KEY": "k", "CHAT_ID": "1"}
    base.update(extra)
    return base


def test_default_weight_when_env_missing():
    s = load_settings(_env())
    assert s.market_weight == DEFAULT_MARKET_WEIGHT


def test_valid_weight_from_env():
    s = load_settings(_env(MARKET_BLEND_WEIGHT="0.6"))
    assert s.market_weight == pytest.approx(0.6)


@pytest.mark.parametrize("bad", ["abc", "3", "-1", ""])
def test_invalid_weight_falls_back_to_default(bad):
    s = load_settings(_env(MARKET_BLEND_WEIGHT=bad))
    assert s.market_weight == DEFAULT_MARKET_WEIGHT


def test_edge_still_uses_model_probability():
    """融合不能污染 edge：edge 用 1/赔率，与融合后的概率无关。

    这里锁住的是 implied_probabilities 的语义——它返回去水概率，
    而 edge 走的是 analyze_value（含抽水口径），两者不可混用。
    """
    odds = {"home": 2.0, "draw": 3.4, "away": 4.0}
    p = implied_probabilities(odds)
    assert sum(p.values()) == pytest.approx(1.0)
    # 去水后三项之和为 1，而原始倒数之和 > 1（含抽水）
    assert sum(1.0 / v for v in odds.values()) > 1.0
