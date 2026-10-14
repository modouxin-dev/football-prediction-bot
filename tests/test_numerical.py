"""数值精度验证 —— 手写算法必须有可复现的对照，不能假设它是对的。

项目刻意不依赖 numpy/scipy/pandas（requirements.txt 只有 5 项），
泊松 PMF、Elo 更新、Dixon-Coles 修正全是纯 Python 手写。
手写不等于错，但**必须有对照实验兜底** —— 否则一个参数顺序写反、
一个阶乘溢出，模型会静默输出错误的概率，而所有人看到的表象只是
"最近预测不太准"。

签名说明（按真实代码，勿凭印象改）：
    analyzer.poisson_pmf(lmbda, k)          —— λ 在前，k 在后
    elo.calculate_elo(a, b, result, k_factor, *, home_advantage, multiplier, scale)
"""

import math

import pytest

from analyzer import poisson_pmf
from elo import calculate_elo


# ---- 泊松 PMF ---------------------------------------------------------------

def test_poisson_pmf_normalized():
    """0~20 球范围内概率和 ≈ 1。

    阈值 1e-6：λ≤4 时 21 项截断的尾概率约 1e-8，留两个数量级余量。
    """
    for lam in [0.5, 1.0, 2.0, 2.5, 3.0, 4.0]:
        total = sum(poisson_pmf(lam, k) for k in range(21))
        assert abs(total - 1.0) < 1e-6, f"λ={lam}: sum={total}"


def test_poisson_pmf_matches_manual_formula():
    """与独立写一遍的公式对照 —— 防止实现本身抄错。"""
    for lam in [0.8, 1.7, 3.3]:
        for k in range(8):
            expected = math.exp(-lam) * lam ** k / math.factorial(k)
            assert poisson_pmf(lam, k) == pytest.approx(expected, rel=1e-12)


def test_poisson_pmf_known_values():
    """教科书固定值：λ=2.5 时 P(X=3) ≈ 0.2138。"""
    assert poisson_pmf(2.5, 3) == pytest.approx(0.213763, abs=1e-6)
    assert poisson_pmf(1.0, 0) == pytest.approx(math.exp(-1.0), rel=1e-12)


def test_poisson_pmf_zero_bounds():
    """k=0 必为 e^-λ；λ=0 时只有 k=0 非零（无进球的极端场次）。"""
    assert poisson_pmf(3.0, 0) == pytest.approx(math.exp(-3.0), rel=1e-12)
    assert poisson_pmf(0.0, 0) == pytest.approx(1.0)
    assert poisson_pmf(0.0, 1) == pytest.approx(0.0)


def test_poisson_pmf_vs_scipy_reference():
    """与 scipy 对照（项目不依赖它，仅在装了的环境下作为交叉验证）。"""
    scipy_stats = pytest.importorskip("scipy.stats")
    for lam in [1.0, 2.0, 3.0]:
        for k in range(10):
            assert poisson_pmf(lam, k) == pytest.approx(
                scipy_stats.poisson.pmf(k, lam), abs=1e-12)


# ---- Elo --------------------------------------------------------------------

def test_elo_rating_conservation():
    """零和：一方加多少，另一方减多少。

    这是Elo的意义所在 —— 联赛总评分恒定，才能跨赛季比较。
    """
    for result in (1.0, 0.5, 0.0):
        a, b = calculate_elo(1500.0, 1500.0, result, 32)
        assert (a + b) == pytest.approx(3000.0, abs=1e-10), f"result={result}"


def test_elo_conservation_with_home_advantage():
    """主场优势只改期望胜率、不改零和性质。

    若哪天有人把 home_advantage 加进评分本身，这条会立刻红。
    """
    a, b = calculate_elo(1500.0, 1500.0, 1.0, 32, home_advantage=65.0)
    assert (a + b) == pytest.approx(3000.0, abs=1e-10)


def test_elo_conservation_with_multiplier():
    """净胜球乘数放大变化量，但仍必须零和。"""
    a, b = calculate_elo(1500.0, 1500.0, 1.0, 32, multiplier=1.75)
    assert (a + b) == pytest.approx(3000.0, abs=1e-10)


def test_elo_winner_gains_loser_loses():
    """胜方涨、负方跌；平局时强者略亏、弱者略赚。"""
    win_a, win_b = calculate_elo(1500.0, 1500.0, 1.0, 32)
    assert win_a > 1500.0 > win_b

    strong, weak = calculate_elo(1600.0, 1400.0, 0.5, 32)
    assert strong < 1600.0 and weak > 1400.0


def test_elo_symmetric_no_home_edge():
    """同分、无主场优势时，双方期望胜率各 0.5 → 胜方恰好涨 K/2。"""
    a, _ = calculate_elo(1500.0, 1500.0, 1.0, 32)
    assert a == pytest.approx(1516.0, abs=1e-9)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    ok = True
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"[PASS] {name}")
        except Exception as exc:  # noqa: BLE001 - 脚本入口需汇总全部结果
            ok = False
            print(f"[FAIL] {name}: {exc}")
    print("\n[ALL PASSED] 数值验证通过" if ok else "\n[FAILED]")
    sys.exit(0 if ok else 1)
