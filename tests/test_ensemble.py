"""多模型集成与兜底的单元测试。

重点不是「集成比市场准」（实测做不到，见 ensemble 模块 docstring），
而是**契约正确性**：缺失模型要重归一化、全缺失要兜底、
学习开关要真的生效、状态要能往返序列化。
"""
from __future__ import annotations

from datetime import timedelta

import math

import pytest

from ensemble import (
    DEFAULT_ETA,
    FALLBACK_PROBS,
    MODEL_NAMES,
    EnsemblePredictor,
    mix_probs,
    normalize_probs,
)


# ---------------------------------------------------------------------------
# 归一化与混合
# ---------------------------------------------------------------------------

def test_normalize_probs_basic():
    assert normalize_probs((1.0, 1.0, 1.0)) == pytest.approx((1 / 3,) * 3)


def test_normalize_probs_rejects_bad_input():
    """非法输入必须回退到经验值，而不是抛异常或返回 NaN。"""
    assert normalize_probs((0.0, 0.0, 0.0)) == FALLBACK_PROBS
    assert normalize_probs((-1.0, 2.0, 0.0))[0] == 0.0
    assert normalize_probs((float("nan"), 1.0, 1.0)) == FALLBACK_PROBS
    assert normalize_probs((1.0, 2.0)) == FALLBACK_PROBS
    assert normalize_probs(None) == FALLBACK_PROBS


def test_mix_probs_skips_zero_weight():
    out = mix_probs([((1.0, 0.0, 0.0), 0.0), ((0.0, 1.0, 0.0), 1.0)])
    assert out == pytest.approx((0.0, 1.0, 0.0))


def test_mix_probs_returns_none_when_all_zero():
    """权重全为 0 时必须返回 None，由调用方兜底 —— 不能臆造概率。"""
    assert mix_probs([((1.0, 0.0, 0.0), 0.0)]) is None
    assert mix_probs([]) is None


def test_mix_probs_weighted_average():
    out = mix_probs([((1.0, 0.0, 0.0), 0.75), ((0.0, 0.0, 1.0), 0.25)])
    assert out == pytest.approx((0.75, 0.0, 0.25))


# ---------------------------------------------------------------------------
# 权重与兜底
# ---------------------------------------------------------------------------

def test_empty_model_pool_rejected():
    with pytest.raises(ValueError):
        EnsemblePredictor([])


def test_weights_renormalize_when_market_missing():
    """市场缺席时，原本给市场的权重必须重新分给统计模型（和为 1）。"""
    ens = EnsemblePredictor()
    w = ens.weights(available=["poisson", "elo", "prior"])
    assert set(w) == {"poisson", "elo", "prior"}
    assert sum(w.values()) == pytest.approx(1.0)
    assert "market" not in w


def test_predict_all_missing_falls_back():
    """一路都没有 → 兜底经验值，且 fallback=True。"""
    ens = EnsemblePredictor()
    out = ens.predict({"poisson": None, "market": None})
    assert out["fallback"] is True
    assert out["probs"] == FALLBACK_PROBS
    assert out["used"] == []


def test_predict_single_model_returns_that_model():
    ens = EnsemblePredictor()
    out = ens.predict({"market": (0.5, 0.3, 0.2)})
    assert out["fallback"] is False
    assert out["probs"] == pytest.approx((0.5, 0.3, 0.2))
    assert out["used"] == ["market"]
    assert out["weights"]["market"] == pytest.approx(1.0)


def test_predict_drops_unknown_model_names():
    """不在模型池里的路必须被忽略，而不是污染权重。"""
    ens = EnsemblePredictor()
    out = ens.predict({"market": (0.5, 0.3, 0.2), "lstm": (0.9, 0.05, 0.05)})
    assert "lstm" in out["dropped"]
    assert out["probs"] == pytest.approx((0.5, 0.3, 0.2))


def test_predict_probs_sum_to_one():
    ens = EnsemblePredictor()
    out = ens.predict({
        "poisson": (0.44, 0.28, 0.28),
        "dc": (0.43, 0.29, 0.28),
        "elo": (0.50, 0.25, 0.25),
        "market": (0.42, 0.30, 0.28),
        "prior": (0.45, 0.25, 0.30),
    })
    assert sum(out["probs"]) == pytest.approx(1.0)
    assert all(0.0 <= p <= 1.0 for p in out["probs"])


def test_fixed_weights_give_market_majority():
    """learn=False 的固定经验权重下，市场应占多数（与实测 0.796 同方向）。"""
    ens = EnsemblePredictor()
    w = ens.raw_weights()
    assert w["market"] > 0.5
    assert sum(w.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 学习
# ---------------------------------------------------------------------------

def test_update_is_noop_when_learn_disabled():
    """learn=False 时 update 必须是安全的空操作，调用方才不必到处判断。"""
    ens = EnsemblePredictor(learn=False)
    before = ens.to_dict()
    ens.update({"market": (0.5, 0.3, 0.2)}, 0)
    assert ens.to_dict() == before


def test_learning_converges_toward_better_model():
    """反复喂「market 准、poisson 不准」的样本，市场权重必须上升。

    这是集成器「各方面结合」的核心证据：它应当自动学会谁更可信。
    """
    ens = EnsemblePredictor(models=("market", "poisson"), eta=DEFAULT_ETA,
                            learn=True)
    good = (0.70, 0.20, 0.10)   # market：主胜大概率 → 实际主胜
    bad = (0.10, 0.20, 0.70)    # poisson：判反
    start = ens.weights(["market", "poisson"])["market"]
    for _ in range(200):
        ens.update({"market": good, "poisson": bad}, 0)
    end = ens.weights(["market", "poisson"])["market"]
    assert end > start
    assert end > 0.9


def test_update_ignores_invalid_actual():
    ens = EnsemblePredictor(learn=True)
    ens.update({"market": (0.5, 0.3, 0.2)}, 7)
    assert ens.to_dict()["counts"]["market"] == 0


def test_learning_no_overflow_on_extreme_scores():
    """η 极大时 exp 必须被夹住，不能产生 inf 并把权重算成 NaN。"""
    ens = EnsemblePredictor(models=("a", "b"), eta=1e9, learn=True)
    for _ in range(50):
        ens.update({"a": (0.99, 0.005, 0.005), "b": (0.005, 0.005, 0.99)}, 0)
    w = ens.weights(["a", "b"])
    assert all(math.isfinite(v) for v in w.values())
    assert sum(w.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def test_state_roundtrip():
    ens = EnsemblePredictor(models=("market", "poisson"), eta=123.0, learn=True)
    ens.update({"market": (0.6, 0.25, 0.15), "poisson": (0.4, 0.3, 0.3)}, 0)
    restored = EnsemblePredictor.from_dict(ens.to_dict())
    assert restored.to_dict() == ens.to_dict()
    assert restored.weights(["market", "poisson"]) == pytest.approx(
        ens.weights(["market", "poisson"]))


def test_from_dict_tolerates_missing_fields():
    """缺失字段用默认值补全，不能抛异常 —— 线上旧状态文件可能不完整。"""
    ens = EnsemblePredictor.from_dict({})
    assert ens.models == list(MODEL_NAMES)
    assert ens.eta == DEFAULT_ETA
    assert ens.learn is False


def test_from_dict_rejects_corrupt_numbers():
    ens = EnsemblePredictor.from_dict({"scores": {"market": "abc"},
                                       "counts": {"market": None}})
    assert ens._scores["market"] == 0.0
    assert ens._counts["market"] == 0


# ---------------------------------------------------------------------------
# 集成路径与比分矩阵的耦合（analyzer.apply_market_adjustment）
# ---------------------------------------------------------------------------

def _base_analysis() -> dict:
    """构造一个最小可用的泊松分析结果（含 λ 与比分矩阵所需字段）。"""
    from analyzer import MatchAnalyzer
    az = MatchAnalyzer()
    res = az.calculate_prediction(
        {"attack": 1.2, "defense": 0.9},
        {"attack": 1.0, "defense": 1.0},
        league_avg_home=1.5, league_avg_away=1.2,
    )
    return res


def test_target_probs_drives_final_probabilities():
    """集成给定 target_probs 时，输出的三项概率必须与之一致。"""
    from analyzer import apply_market_adjustment
    a = _base_analysis()
    target = {"home": 0.5, "draw": 0.3, "away": 0.2}
    out = apply_market_adjustment(a, None, target_probs=target)
    assert out["market_used"] is True
    assert out["win_prob"] == pytest.approx(0.5, abs=1e-3)
    assert out["draw_prob"] == pytest.approx(0.3, abs=1e-3)
    assert out["loss_prob"] == pytest.approx(0.2, abs=1e-3)
    assert out.get("ensemble_used") is True


def test_target_probs_keeps_score_matrix_consistent():
    """最可能比分所属的结果，必须与最高概率的那一项一致。

    这是集成路径最容易踩的坑：只换三项概率而不缩放矩阵，会出现
    「最可能比分 1-0（主胜）」却「客胜概率最高」的矛盾展示。
    """
    from analyzer import apply_market_adjustment
    a = _base_analysis()
    # 故意给一个客胜占优的目标，检验矩阵是否跟着翻转
    out = apply_market_adjustment(
        a, None, target_probs={"home": 0.15, "draw": 0.2, "away": 0.65})
    assert out["loss_prob"] > out["win_prob"]
    best = out.get("best_score") or ""
    if best and "-" in best:
        h, aw = (int(x) for x in best.split("-"))
        assert aw >= h, f"最可能比分 {best} 与客胜最高矛盾"


def test_target_probs_invalid_falls_back_safely():
    """target_probs 非法（全零 / 非数字）时必须安全回退，不能产出 NaN。"""
    from analyzer import apply_market_adjustment
    a = _base_analysis()
    out = apply_market_adjustment(a, None, target_probs={"home": 0, "draw": 0,
                                                         "away": 0})
    assert out["market_used"] is False
    assert all(math.isfinite(v) for v in
               (out["win_prob"], out["draw_prob"], out["loss_prob"]))


def test_target_probs_skips_double_blending():
    """集成路径下不能再叠加一次 blend_weight —— 否则等于融合两遍。"""
    from analyzer import apply_market_adjustment
    a = _base_analysis()
    target = {"home": 0.5, "draw": 0.3, "away": 0.2}
    out = apply_market_adjustment(a, None, blend_weight=0.75,
                                  target_probs=target)
    # 若被二次融合，主胜概率会被拉向泊松值而偏离 0.5
    assert out["win_prob"] == pytest.approx(0.5, abs=1e-3)


# ---------------------------------------------------------------------------
# service 层：集成是否真的接进了预测主流程
# ---------------------------------------------------------------------------

def test_service_prediction_carries_ensemble_metadata():
    """每场预测必须带 ensemble 元信息（用了哪几路、各自权重）。

    这是「各方面结合给出最终结论」的可观测证据 —— 没有它就无法判断
    某场比赛到底是市场说了算，还是统计模型兜的底。
    """
    import asyncio

    from config import load_settings
    from service import PredictionService
    from tests.sample_data import NOW, FakeAPI, default_fixtures

    env = {"TELEGRAM_TOKEN": "t", "RAPID_API_KEY": "k", "CHAT_ID": "1",
           "SEASON": "2026", "TIMEZONE": "Asia/Shanghai"}
    settings = load_settings(env)
    api = FakeAPI({2026: default_fixtures()})
    svc = PredictionService(settings, api)
    preds = asyncio.run(svc.build_predictions(now=NOW))
    assert preds, "样例数据下应至少产出一场预测"
    for p in preds:
        ens = p.analysis.get("ensemble")
        assert ens is not None, "预测缺少 ensemble 元信息"
        assert "used" in ens and "weights" in ens
        probs = (p.analysis["win_prob"], p.analysis["draw_prob"],
                 p.analysis["loss_prob"])
        assert sum(probs) == pytest.approx(1.0, abs=1e-2)
        assert all(0.0 <= v <= 1.0 for v in probs)


def test_service_survives_without_odds():
    """没有赔率时不能崩，且必须回落到统计模型 + 基线（兜底链生效）。"""
    import asyncio

    from config import load_settings
    from service import PredictionService
    from tests.sample_data import NOW, FakeAPI, default_fixtures

    env = {"TELEGRAM_TOKEN": "t", "RAPID_API_KEY": "k", "CHAT_ID": "1",
           "SEASON": "2026", "TIMEZONE": "Asia/Shanghai"}
    settings = load_settings(env)
    # 注意：FakeAPI 的 odds=None 会走「默认赔率」分支，必须传空列表
    # 才能真正模拟「这场没有赔率」。
    api = FakeAPI({2026: default_fixtures()}, odds=[])
    svc = PredictionService(settings, api)
    preds = asyncio.run(svc.build_predictions(now=NOW))
    # 无赔率 → 无亚盘 → 按「无盘不推荐」策略整场不推荐（项目方向：准确率优先）。
    # 这里守的是「集成器在无市场路时仍能算出结论」，故绕过推荐门槛直接造一场：
    # 单独调 _predict_one 取到 Prediction 本体来断言集成口径。
    assert preds == [], "无亚盘时不应产生推荐"
    raw = asyncio.run(svc._predict_one(
        asyncio.run(svc._model_for(39)), NOW + timedelta(hours=2),
        default_fixtures()[0]))
    preds = [raw]
    for p in preds:
        ens = p.analysis.get("ensemble") or {}
        # 无赔率时 market 不应出现在已用列表里
        assert "market" not in ens.get("used", [])
        probs = (p.analysis["win_prob"], p.analysis["draw_prob"],
                 p.analysis["loss_prob"])
        assert sum(probs) == pytest.approx(1.0, abs=1e-2)
