"""预测决策链可追溯性。

没有它，复盘只能看到最终概率，无法回答：
- 是模型算错了，还是市场把结论带偏了？
- 温度校准到底动了多少？
- Elo 有没有真的介入？

本文件的测试覆盖三件事：
1. 阶段链真实记录每一步的概率，且末阶段 == 落库展示概率（自洽）
2. 输入特征如实留档，取不到时是 None 而非 0
3. trace 落进 inputs 并能在 /prediction 详情里取回
"""
from __future__ import annotations

import json
import math

import pytest

from analyzer import LeagueModel, MatchAnalyzer, TeamStrength


def _model() -> LeagueModel:
    """构造含两队真实强度的联赛模型（避免测试依赖外网积分榜）。"""
    return LeagueModel(
        avg_home_goals=1.5, avg_away_goals=1.2,
        teams={
            1: TeamStrength(attack_home=1.2, defense_home=0.9,
                            attack_away=1.0, defense_away=1.1),
            2: TeamStrength(attack_home=1.0, defense_home=1.05,
                            attack_away=1.1, defense_away=0.95),
        },
    )


# ---- 阶段链 -----------------------------------------------------------------

def test_pre_temperature_probs_recorded():
    """温度前的概率必须留档，否则无法把「泊松算的」和「温度改的」分开。"""
    m = _model()

    res = MatchAnalyzer().predict_match(m, 1, 2, temperature=1.15)
    pre = res.get("pre_temperature_probs")
    assert pre is not None, "未记录温度前概率"
    assert len(pre) == 3
    assert abs(sum(pre) - 1.0) < 1e-3
    # 温度 >1 会拉平概率：最大值应变小
    assert max(pre) >= max(res["win_prob"], res["draw_prob"], res["loss_prob"])


def test_features_recorded_with_all_keys():
    """输入特征必须留齐，缺一项就无法回答「为什么是这个数」。"""
    m = _model()

    res = MatchAnalyzer().predict_match(m, 1, 2, temperature=1.0,
                                  prematch={"home": {"attack": 0.95}})
    ft = res.get("features")
    assert ft is not None
    for key in ("home_attack", "home_defense", "away_attack", "away_defense",
                "league_avg_home", "league_avg_away", "rho",
                "prematch_applied"):
        assert key in ft, f"features 缺 {key}"
    assert ft["prematch_applied"] is True


def test_elo_factor_none_when_not_enabled():
    """未启用 Elo 时必须是 None，不能填 1.0。

    填 1.0 会被读成「启用了但影响为零」，而真实的语义是「根本没启用」。
    """
    m = _model()

    res = MatchAnalyzer().predict_match(m, 1, 2)
    assert res["features"]["elo_factor"] is None


def test_elo_factor_recorded_when_enabled():
    """启用 Elo 时必须记录真实系数。"""
    m = _model()

    res = MatchAnalyzer().predict_match(m, 1, 2, elo_factor=1.08)
    assert res["features"]["elo_factor"] == pytest.approx(1.08, abs=1e-4)


# ---- 阶段链自洽 -------------------------------------------------------------

def test_build_trace_stage_chain():
    """阶段链：三个阶段按顺序，末阶段 = 展示概率。"""
    from service import PredictionService

    trace = PredictionService._build_trace(
        analysis={"pre_temperature_probs": [0.44, 0.27, 0.29],
                  "features": {"rho": -0.02}},
        prematch=None,
        model_probs=(0.43, 0.28, 0.29),
        final_probs=(0.34, 0.23, 0.43),
        market_probs=(0.30, 0.21, 0.49),
        weights={"model": 0.25, "market": 0.75},
        used=["model", "market"], fallback=False, temperature=1.08)

    keys = [s["key"] for s in trace["stages"]]
    assert keys == ["model", "temperature", "ensemble"]
    assert trace["stages"][-1]["probs"] == [0.34, 0.23, 0.43]
    assert trace["final"] == [0.34, 0.23, 0.43]
    assert trace["market"] == {"home": 0.30, "draw": 0.21, "away": 0.49}
    # 可 JSON 序列化（落库必需）
    json.dumps(trace, ensure_ascii=False)


def test_build_trace_no_market_is_none_not_zero():
    """无赔率时市场口径必须是 None，不能填 0。

    填 0 会被读成「市场给主胜 0%」，而真实的语义是「没有市场数据」。
    """
    from service import PredictionService

    trace = PredictionService._build_trace(
        analysis={"pre_temperature_probs": [0.4, 0.3, 0.3]},
        prematch=None,
        model_probs=(0.4, 0.3, 0.3), final_probs=(0.4, 0.3, 0.3),
        market_probs=None, weights={"model": 1.0},
        used=["model"], fallback=False, temperature=1.0)
    assert trace["market"] is None


def test_build_trace_missing_pre_temperature_is_none():
    """取不到温度前概率时记 None，不编造。"""
    from service import PredictionService

    trace = PredictionService._build_trace(
        analysis={}, prematch=None,
        model_probs=(0.4, 0.3, 0.3), final_probs=(0.4, 0.3, 0.3),
        market_probs=None, weights={}, used=[], fallback=False,
        temperature=1.0)
    assert trace["stages"][0]["probs"] is None
    assert trace["features"] is None
