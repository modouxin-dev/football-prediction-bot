"""概率温度校准：修正泊松模型系统性过度自信。

背景（实测英超 1139 场，walk-forward、无前视偏差）
------------------------------------------------
模型输出的概率分布"过度尖锐"：

    预测 ~55% 的比赛实际只发生 43%（−11.2pt）
    预测 ~74% 的比赛实际只发生 66%（ −8.6pt）

而低概率档反而低估（平局整体低估约 3pt）。这个偏差会直接污染业务判定：

    价值偏差 = 模型概率 − 1/赔率

高档虚高 11pt 远超 5% 的 Value Bet 阈值，会把并非价值注的比赛误标成价值注。
温度缩放 p' = softmax(log p / T) 是校准过度自信的标准做法：T>1 拉平分布，
**不改变三项概率的排序**，因此准确率不变、只是让概率说真话。
"""

import math

import pytest

from analyzer import (
    TEMP_MIN_SAMPLES,
    apply_temperature,
    fit_temperature,
)


def _obs(n: int, overconfident: bool = True) -> list[tuple]:
    """造一批 (probs, actual) 样本。

    **真实**赛果分布恒定为 主胜 50% / 平局 30% / 客胜 20%。
    模型声明的概率分两种：
      - overconfident=True：说主胜 65%（高于真实的 50%）—— 模拟现实的过度自信
      - overconfident=False：说的与真实完全一致（已完美校准）

    actual 按固定周期分配，保证实际频率精确等于真实分布，不引入随机性。
    """
    stated_home = 0.65 if overconfident else 0.50
    # 剩余概率按真实的 30:20 拆给平局/客胜
    rest = 1.0 - stated_home
    probs = [stated_home, rest * 0.6, rest * 0.4]
    out: list[tuple] = []
    for i in range(n):
        bucket = i % 10          # 0-4 主胜(50%) / 5-7 平局(30%) / 8-9 客胜(20%)
        actual = 0 if bucket < 5 else (1 if bucket < 8 else 2)
        out.append((list(probs), actual))
    return out


# ---- apply_temperature ----------------------------------------------------

def test_temperature_one_is_identity():
    """T=1.0 必须是恒等变换：不校准时不能偷偷改变任何数字。"""
    probs = [0.5, 0.3, 0.2]
    assert apply_temperature(probs, 1.0) == pytest.approx(probs)


def test_temperature_preserves_order():
    """温度缩放不得改变三项概率的大小排序（否则会改变预测方向）。"""
    probs = [0.55, 0.25, 0.20]
    scaled = apply_temperature(probs, 1.3)
    order_before = sorted(range(3), key=lambda k: probs[k], reverse=True)
    order_after = sorted(range(3), key=lambda k: scaled[k], reverse=True)
    assert order_before == order_after


def test_temperature_flattens_distribution():
    """T>1 必须拉平分布：最大项变小、最小项变大。"""
    probs = [0.70, 0.20, 0.10]
    scaled = apply_temperature(probs, 1.5)
    assert scaled[0] < probs[0], "最大项应被压低"
    assert scaled[2] > probs[2], "最小项应被抬高"


@pytest.mark.parametrize("t", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_temperature_is_noop(t):
    """非法温度一律原样返回，绝不产出 NaN 污染下游判定。"""
    probs = [0.5, 0.3, 0.2]
    assert apply_temperature(probs, t) == pytest.approx(probs)


def test_zero_sum_probabilities_are_returned_untouched():
    """全零概率不该触发除零。"""
    assert apply_temperature([0.0, 0.0, 0.0], 1.3) == [0.0, 0.0, 0.0]


# ---- fit_temperature ------------------------------------------------------

def test_insufficient_samples_returns_one():
    """样本不足时必须返回 1.0（不校准），绝不猜测。"""
    assert fit_temperature(_obs(TEMP_MIN_SAMPLES - 1)) == 1.0


def test_overconfident_model_gets_temperature_above_one():
    """过度自信的模型应拟合出 T>1，把分布拉平。"""
    t = fit_temperature(_obs(400, overconfident=True))
    assert t > 1.0, f"过度自信应得到 T>1，实际 {t}"


def test_well_calibrated_model_stays_near_one():
    """已完美校准的模型不应被强行拉平（T 应接近 1）。"""
    t = fit_temperature(_obs(400, overconfident=False))
    assert abs(t - 1.0) < 0.15, f"已校准模型不该大幅拉平，实际 T={t}"


def test_fit_uses_only_past_samples():
    """拟合不得偷看未来：用前半段拟合，只能在后半段验证，不能反过来。

    这条锁住"用全量数据拟合再评估自身"的前视偏差——那样得到的漂亮数字
    上线即失效。
    """
    obs = _obs(600, overconfident=True)
    half = len(obs) // 2
    t = fit_temperature(obs[:half])          # 只用前半段
    assert t > 1.0


    def nll(rows, temp):
        return -sum(math.log(max(apply_temperature(p, temp)[a], 1e-9))
                    for p, a in rows) / len(rows)

    # 在**未参与拟合**的后半段上，校准后的对数损失必须更低
    assert nll(obs[half:], t) < nll(obs[half:], 1.0)


def test_malformed_observations_are_skipped():
    """脏数据（None、越界索引、零和概率）应被跳过而不是崩掉。"""
    good = _obs(TEMP_MIN_SAMPLES + 20)
    dirty = good + [(None, 0), ([0.5, 0.3, 0.2], 9), ([0.0, 0.0, 0.0], 0), ([0.5], 0)]
    assert fit_temperature(dirty) == fit_temperature(good)


# ---- 生产接入 --------------------------------------------------------------

def test_predict_match_applies_temperature_without_flipping_direction():
    """生产入口 predict_match 应用温度后，预测方向不得改变。"""
    from analyzer import MatchAnalyzer, build_league_model

    standings = [
        {"team": {"id": 1}, "home": {"played": 10, "goals": {"for": 20, "against": 8}},
         "away": {"played": 10, "goals": {"for": 15, "against": 10}}},
        {"team": {"id": 2}, "home": {"played": 10, "goals": {"for": 10, "against": 15}},
         "away": {"played": 10, "goals": {"for": 8, "against": 20}}},
    ]
    model = build_league_model(standings)
    a = MatchAnalyzer()
    raw = a.predict_match(model, 1, 2)
    cal = a.predict_match(model, 1, 2, temperature=1.4)

    # 方向不变
    assert max(("win_prob", "draw_prob", "loss_prob"), key=lambda k: raw[k]) == \
           max(("win_prob", "draw_prob", "loss_prob"), key=lambda k: cal[k])
    # 但最大项被压低（不再那么自信）
    top = max(("win_prob", "draw_prob", "loss_prob"), key=lambda k: raw[k])
    assert cal[top] < raw[top]
    # 概率仍是合法分布
    assert abs(sum(cal[k] for k in ("win_prob", "draw_prob", "loss_prob")) - 1.0) < 1e-6
    assert cal.get("calibrated") is True


def test_service_refresh_calibration_is_safe_on_empty_db(tmp_path):
    """空库/无已结算样本时必须保持 T=1.0，不校准、不猜测、不崩。"""
    import os

    from repository import PredictionRepository
    from service import PredictionService

    repo = PredictionRepository(str(tmp_path / "s.db"))
    assert repo.settled_samples() == []

    svc = PredictionService.__new__(PredictionService)
    svc.repo = repo
    assert svc.refresh_calibration() == 1.0


def test_backtest_calibration_does_not_use_future():
    """回测里的温度校准不得引入前视偏差。

    判据：开启 use_calib 后准确率必须**完全不变**（温度不改变排序），
    而 log_loss 应当改善。若准确率也变了，说明改的不是校准而是别的什么。
    """
    import os
    import tempfile

    from backtest import WalkForwardBacktester
    from backtest_corpus import load_corpus
    import repository

    with tempfile.TemporaryDirectory() as d:
        ms, _ = load_corpus(
            repository.PredictionRepository(os.path.join(d, "e.db")), "PL")
        base = WalkForwardBacktester(min_history=30).run(ms)
        cal = WalkForwardBacktester(min_history=30, use_calib=True).run(ms)

    assert cal.n == base.n
    assert abs(cal.accuracy_rate - base.accuracy_rate) < 1e-9, "温度校准不该改变准确率"
    assert cal.log_loss <= base.log_loss + 1e-9, (
        "校准应改善对数损失：%.4f vs %.4f" % (cal.log_loss, base.log_loss))
