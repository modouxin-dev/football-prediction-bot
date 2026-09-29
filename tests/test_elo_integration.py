"""Elo → 泊松 的融合验证 / Elo-Poisson integration.

重点：Elo 必须真正改变预测输出（否则只是装饰品），但影响必须有界。
"""
from analyzer import ELO_FACTOR_MAX, ELO_FACTOR_MIN, MatchAnalyzer
from elo import elo_multiplier

BASE = {
    "home_stats": {"attack": 1.2, "defense": 0.9},
    "away_stats": {"attack": 1.1, "defense": 0.95},
    "league_avg_home": 1.5,
    "league_avg_away": 1.2,
}


def pred(**kw):
    return MatchAnalyzer().calculate_prediction(**BASE, **kw)


# ---- 向后兼容 ---------------------------------------------------------------

def test_no_elo_factor_keeps_pure_poisson():
    """不传 elo_factor 时行为完全不变（已有测试与线上逻辑不受影响）。"""
    a = pred()
    assert "win_prob" in a and abs(a["lambda_home"] - 1.2 * 0.95 * 1.5) < 1e-9


# ---- 单场更新 ---------------------------------------------------------------

def test_elo_factor_changes_prediction():
    """核心：Elo 必须真正改变预测，不能是装饰品。"""
    plain = pred()
    with_elo = pred(elo_factor=1.2)
    assert with_elo["win_prob"] > plain["win_prob"]
    assert with_elo["loss_prob"] < plain["loss_prob"]


def test_elo_factor_weak_home_reduces_win_prob():
    plain = pred()
    weak = pred(elo_factor=0.85)
    assert weak["win_prob"] < plain["win_prob"]


def test_elo_factor_one_is_neutral():
    """系数为 1 时与不传完全一致。"""
    assert abs(pred(elo_factor=1.0)["win_prob"] - pred()["win_prob"]) < 1e-12


def test_elo_factor_tilts_share_not_multiply():
    """融合方式是「份额归一」而非直接相乘。

    主队进球占比被系数平移，因此比例朝预期方向变化，但不是简单的 ×f。
    """
    plain = pred()
    f = 1.1
    boosted = pred(elo_factor=f)
    plain_share = plain["lambda_home"] / (plain["lambda_home"] + plain["lambda_away"])
    boost_share = boosted["lambda_home"] / (boosted["lambda_home"] + boosted["lambda_away"])
    assert boost_share > plain_share
    # 且变化幅度小于直接相乘（更保守）
    assert boosted["lambda_home"] < plain["lambda_home"] * f


def test_elo_factor_preserves_total_goals():
    """λ主+λ客 不变 → 大小球判断不受 Elo 影响。"""
    plain = pred()
    for f in (0.85, 0.95, 1.0, 1.1, 1.25):
        m = pred(elo_factor=f)
        assert abs((m["lambda_home"] + m["lambda_away"])
                   - (plain["lambda_home"] + plain["lambda_away"])) < 1e-9


def test_elo_factor_is_clamped_in_analyzer():
    """即使调用方传离谱的值，也要被夹在安全区间——防止 Elo 带崩模型。"""
    sane = pred(elo_factor=ELO_FACTOR_MAX)
    crazy = pred(elo_factor=999.0)
    assert abs(crazy["lambda_home"] - sane["lambda_home"]) < 1e-9

    sane_low = pred(elo_factor=ELO_FACTOR_MIN)
    crazy_low = pred(elo_factor=0.0001)
    assert abs(crazy_low["lambda_home"] - sane_low["lambda_home"]) < 1e-9


def test_elo_factor_negative_or_zero_ignored():
    """非法值（0 / 负数）应被忽略，退化为纯泊松。"""
    for bad in (0, 0.0, -1.0):
        assert abs(pred(elo_factor=bad)["win_prob"] - pred()["win_prob"]) < 1e-12


def test_probabilities_still_sum_to_one():
    """融合后三项概率之和仍须为 1。"""
    for f in (0.8, 1.0, 1.25):
        r = pred(elo_factor=f)
        assert abs(r["win_prob"] + r["draw_prob"] + r["loss_prob"] - 1.0) < 1e-9


# ---- 端到端：真实 Elo 分 → 预测 ---------------------------------------------

def test_end_to_end_elo_ratings_to_prediction():
    """从 Elo 评分一路算到胜率变化，验证整条链路可用。"""
    plain = pred()
    factor = elo_multiplier(1700.0, 1500.0)          # 主队强 200 分
    assert factor > 1.0
    boosted = pred(elo_factor=factor)
    assert boosted["win_prob"] > plain["win_prob"]

    factor_away = elo_multiplier(1500.0, 1700.0)     # 客队强 200 分
    assert factor_away < 1.0
    lowered = pred(elo_factor=factor_away)
    assert lowered["win_prob"] < plain["win_prob"]


def test_elo_influence_is_bounded_in_practice():
    """实战检验：极端 Elo 分差下，胜率变化幅度仍应可控（不超过 ±10 个百分点）。"""
    plain = pred()
    for diff in (-600, -300, 300, 600):
        m = pred(elo_factor=elo_multiplier(1500.0 + diff, 1500.0))
        shift = abs(m["win_prob"] - plain["win_prob"])
        assert shift < 0.10, f"分差 {diff} 时胜率偏移 {shift:.3f}，Elo 影响过大"
