"""第 4 阶段：Dixon-Coles 低比分修正 —— 数学验证。

审计要求：不能只测「程序不崩溃」，必须用手算的已知值验证计算精度。
本文件即该模块的 mathematical_verification。

参考：Dixon & Coles (1997), "Modelling Association Football Scores and
Inefficiencies in the Football Betting Market", Applied Statistics.
"""
import math

import pytest

from analyzer import (
    MAX_GOALS, RHO_MIN_SAMPLES, dc_rho_bounds, dc_score_matrix, dc_tau,
    clamp_rho, fit_rho, poisson_pmf,
)

# 手算基准：λ=1.5, μ=1.2, ρ=-0.1
LH, LA, RHO = 1.5, 1.2, -0.1


# ============================================================================
# 一、τ 修正项（对照手算值，精度 1e-12）
# ============================================================================

def test_tau_00_hand_computed():
    """τ(0,0) = 1 − λμρ = 1 − 1.5×1.2×(−0.1) = 1.18"""
    assert abs(dc_tau(0, 0, LH, LA, RHO) - 1.18) < 1e-12


def test_tau_01_hand_computed():
    """τ(0,1) = 1 + λρ = 1 + 1.5×(−0.1) = 0.85"""
    assert abs(dc_tau(0, 1, LH, LA, RHO) - 0.85) < 1e-12


def test_tau_10_hand_computed():
    """τ(1,0) = 1 + μρ = 1 + 1.2×(−0.1) = 0.88"""
    assert abs(dc_tau(1, 0, LH, LA, RHO) - 0.88) < 1e-12


def test_tau_11_hand_computed():
    """τ(1,1) = 1 − ρ = 1.10"""
    assert abs(dc_tau(1, 1, LH, LA, RHO) - 1.10) < 1e-12


def test_tau_other_cells_are_one():
    """除四个低比分格子外的所有格子 τ 恒为 1。"""
    for x in range(MAX_GOALS + 1):
        for y in range(MAX_GOALS + 1):
            if (x, y) in {(0, 0), (0, 1), (1, 0), (1, 1)}:
                continue
            assert dc_tau(x, y, LH, LA, RHO) == 1.0


def test_tau_rho_zero_is_one():
    """ρ=0 时全部 τ=1，这是与纯泊松等价的基础。"""
    for x in range(4):
        for y in range(4):
            assert dc_tau(x, y, LH, LA, 0.0) == 1.0


def test_tau_sign_direction():
    """ρ<0 的语义：抬高 0-0/1-1，压低 1-0/0-1。"""
    assert dc_tau(0, 0, LH, LA, RHO) > 1.0
    assert dc_tau(1, 1, LH, LA, RHO) > 1.0
    assert dc_tau(1, 0, LH, LA, RHO) < 1.0
    assert dc_tau(0, 1, LH, LA, RHO) < 1.0


# ============================================================================
# 二、矩阵性质
# ============================================================================

def test_rho_zero_identical_to_pure_poisson():
    """ρ=0 必须与纯泊松逐元素一致（向后兼容铁律）。"""
    n = MAX_GOALS + 1
    ph = [poisson_pmf(LH, k) for k in range(n)]
    pa = [poisson_pmf(LA, k) for k in range(n)]
    expected = [[a * b for b in pa] for a in ph]
    total = sum(sum(r) for r in expected)
    expected = [[v / total for v in r] for r in expected]
    got = dc_score_matrix(LH, LA, rho=0.0)
    for i in range(n):
        for j in range(n):
            assert abs(got[i][j] - expected[i][j]) < 1e-12


def test_matrix_sums_to_one():
    for rho in (-0.2, -0.1, 0.0, 0.1, 0.2):
        m = dc_score_matrix(LH, LA, rho=rho)
        assert abs(sum(sum(r) for r in m) - 1.0) < 1e-12


def test_all_probabilities_non_negative():
    for rho in (-0.3, -0.1, 0.0, 0.3):
        m = dc_score_matrix(LH, LA, rho=rho)
        assert all(v >= 0.0 for row in m for v in row)


def test_negative_rho_raises_low_scores():
    """核心语义验证：ρ<0 必须抬高 0-0 / 1-1、压低 1-0 / 0-1。"""
    base = dc_score_matrix(LH, LA, rho=0.0)
    dc = dc_score_matrix(LH, LA, rho=RHO)
    assert dc[0][0] > base[0][0], "0-0 未抬高"
    assert dc[1][1] > base[1][1], "1-1 未抬高"
    assert dc[1][0] < base[1][0], "1-0 未压低"
    assert dc[0][1] < base[0][1], "0-1 未压低"


def test_other_cells_unchanged_after_normalization():
    """只有四个低比分格子的**原始**概率被改动。

    注意：归一化后所有格子都会等比缩放，故比较的是「相对比例」不变。
    这里直接验证 τ=1 的格子在归一化前后的比值保持一致。
    """
    m0 = dc_score_matrix(LH, LA, rho=0.0)
    m1 = dc_score_matrix(LH, LA, rho=RHO)
    for (x, y) in [(2, 1), (3, 0), (2, 2), (5, 4)]:
        assert dc_tau(x, y, LH, LA, RHO) == 1.0
    # τ=1 的格子，比例应等于两个矩阵归一化因子之比（对所有此类格子相同）
    ratios = {m1[x][y] / m0[x][y] for (x, y) in [(2, 1), (3, 0), (2, 2), (5, 4)]}
    assert len(ratios) == 1, "τ=1 的格子缩放比例不一致"


def test_draw_probability_increases_with_negative_rho():
    """0-0 与 1-1 都是平局 → 平局概率必然上升。"""
    def draw_p(rho):
        m = dc_score_matrix(LH, LA, rho=rho)
        return sum(m[i][i] for i in range(MAX_GOALS + 1))
    assert draw_p(-0.1) > draw_p(0.0)


# ============================================================================
# 三、ρ 可行区间与夹紧（鲁棒性：防 NaN / 负概率）
# ============================================================================

def test_rho_bounds_hand_computed():
    """λ=1.5, μ=1.2：
       下界 = max(−1/1.5, −1/1.2) = max(−0.6667, −0.8333) = −0.6667 = −1/λ
       上界 = min(1/(1.5×1.2), 1) = min(0.5556, 1) = 0.5556
       （max 取的是**更接近 0** 的那个，即 −1/λ，因为 λ > μ）"""
    lo, hi = dc_rho_bounds(LH, LA)
    assert abs(lo - (-1.0 / 1.5)) < 1e-12
    assert abs(hi - (1.0 / 1.8)) < 1e-12


def test_clamp_rho_keeps_tau_non_negative():
    """夹紧后的 ρ 必须保证所有 τ ≥ 0 —— 否则会出现负概率。"""
    for lh in (0.3, 0.8, 1.5, 3.0, 6.0):
        for la in (0.3, 0.8, 1.2, 2.5, 5.0):
            for rho in (-10.0, -1.0, -0.5, 0.0, 0.5, 1.0, 10.0):
                r = clamp_rho(rho, lh, la)
                for (x, y) in [(0, 0), (0, 1), (1, 0), (1, 1)]:
                    assert dc_tau(x, y, lh, la, r) >= -1e-12


def test_clamp_rho_handles_nan_and_inf():
    """NaN / inf 必须被安全处理为 0（退回纯泊松），不能污染结果。"""
    assert clamp_rho(float("nan"), LH, LA) == 0.0
    assert clamp_rho(float("inf"), LH, LA) == 0.0
    assert clamp_rho(float("-inf"), LH, LA) == 0.0
    assert clamp_rho(None, LH, LA) == 0.0


def test_clamp_rho_handles_non_numeric():
    assert clamp_rho("bad", LH, LA) == 0.0


def test_extreme_low_scoring_no_nan():
    """极低进球率：λ 接近 0，不能产生 NaN 或溢出。"""
    m = dc_score_matrix(1e-6, 1e-6, rho=-0.1)
    assert all(math.isfinite(v) and v >= 0.0 for row in m for v in row)
    assert abs(sum(sum(r) for r in m) - 1.0) < 1e-9


def test_extreme_high_scoring_no_nan():
    """极高进球率：λ=20 时 exp(−λ) 极小，仍不能崩。"""
    m = dc_score_matrix(20.0, 20.0, rho=-0.1)
    assert all(math.isfinite(v) and v >= 0.0 for row in m for v in row)
    assert abs(sum(sum(r) for r in m) - 1.0) < 1e-9


def test_zero_lambda_degrades_gracefully():
    m = dc_score_matrix(0.0, 0.0, rho=-0.1)
    assert all(math.isfinite(v) and v >= 0.0 for row in m for v in row)


def test_negative_lambda_degrades_gracefully():
    m = dc_score_matrix(-1.0, 1.0, rho=-0.1)
    assert all(math.isfinite(v) and v >= 0.0 for row in m for v in row)


# ============================================================================
# 四、ρ 拟合（参数优化）
# ============================================================================

def _observations(n, rho_true, seed=7, lh=1.5, la=1.2):
    """用固定 λ/μ 生成观测样本。"""
    import random
    random.seed(seed)
    out = []
    for _ in range(n):
        m = dc_score_matrix(lh, la, rho=rho_true)
        r = random.random()
        acc = 0.0
        for x, row in enumerate(m):
            for y, p in enumerate(row):
                acc += p
                if r <= acc:
                    out.append((lh, la, x, y))
                    break
            else:
                continue
            break
    return out


def test_fit_rho_requires_minimum_samples():
    """样本不足时必须退回 0.0（纯泊松），绝不猜测。"""
    assert fit_rho([]) == 0.0
    assert fit_rho(_observations(RHO_MIN_SAMPLES - 1, -0.1)) == 0.0


def test_fit_rho_recovers_known_value_large_sample():
    """大样本下应能恢复真实 ρ（无偏性验证）。"""
    est = fit_rho(_observations(4000, -0.10))
    assert abs(est - (-0.10)) < 0.05, f"拟合 {est}，偏离真实值 -0.10 过远"


def test_fit_rho_returns_zero_rho_for_independent_data():
    """数据本身无相关性（ρ_true=0）时，拟合结果应接近 0。"""
    est = fit_rho(_observations(4000, 0.0))
    assert abs(est) < 0.05, f"独立数据却拟合出 ρ={est}"


def test_fit_rho_ignores_out_of_range_scores():
    """超过 max_goals 的比分不能参与拟合（否则索引越界）。"""
    obs = [(1.5, 1.2, 99, 99)] * 100
    assert fit_rho(obs) == 0.0


def test_fit_rho_handles_none_scores():
    obs = [(1.5, 1.2, None, None)] * 100
    assert fit_rho(obs) == 0.0


def test_fit_rho_returns_finite():
    est = fit_rho(_observations(500, -0.15))
    assert math.isfinite(est)


# ============================================================================
# 五、接入后的端到端行为
# ============================================================================

def test_calculate_prediction_accepts_rho():
    from analyzer import MatchAnalyzer
    a = MatchAnalyzer()
    base = a.calculate_prediction(
        {"attack": 1.2, "defense": 0.9}, {"attack": 1.1, "defense": 0.95},
        league_avg_home=1.5, league_avg_away=1.2)
    dc = a.calculate_prediction(
        {"attack": 1.2, "defense": 0.9}, {"attack": 1.1, "defense": 0.95},
        league_avg_home=1.5, league_avg_away=1.2, rho=-0.1)
    assert abs(base["win_prob"] - dc["win_prob"]) > 1e-6
    assert dc["dixon_coles"] is True
    assert base["dixon_coles"] is False


def test_rho_zero_backward_compatible():
    from analyzer import MatchAnalyzer
    a = MatchAnalyzer()
    kwargs = {"home_stats": {"attack": 1.2, "defense": 0.9},
              "away_stats": {"attack": 1.1, "defense": 0.95},
              "league_avg_home": 1.5, "league_avg_away": 1.2}
    assert a.calculate_prediction(**kwargs)["win_prob"] == \
        a.calculate_prediction(**kwargs, rho=0.0)["win_prob"]
