"""赛前实时信号（伤停 / 首发阵容）的单元测试。

设计红线 / The red line of these tests
--------------------------------------
**没有任何信号时，模型必须与接入前逐位一致。** 这是唯一能证明「新代码没有
悄悄改坏线上预测」的方法，也是本模块最重要的不变量。

    With no signals at all, the model must be bit-for-bit identical to before
    this module existed. That is the only way to prove the new code did not
    silently change live predictions — and the single most important invariant
    in this module.
"""
from __future__ import annotations

import pytest

from analyzer import LeagueModel, MatchAnalyzer, TeamStrength
from data_source import FIXTURE_ID_METHODS, OPTIONAL_METHODS
from prematch import (
    CONCEDE_SHARE,
    MAX_LOSS,
    apply_to_lambdas,
    absence_load,
    formation_tilt,
    loss_factor,
    squad_factors,
    starters_tilt,
)

HOME, AWAY = 1, 2


def _model() -> LeagueModel:
    return LeagueModel(teams={HOME: TeamStrength(), AWAY: TeamStrength()})


def _injury(team_id: int, kind: str = "Missing Fixture", name: str = "X") -> dict:
    return {"team": {"id": team_id}, "player": {"name": name, "type": kind}}


# ── 红线：无信号必须完全等价 ──────────────────────────────────


def test_no_signals_returns_exactly_one():
    """无伤停无阵容 → 四个乘数精确等于 1.0（不是近似）。"""
    factors = squad_factors(HOME, AWAY)
    for side in ("home", "away"):
        assert factors[side]["attack"] == 1.0
        assert factors[side]["concede"] == 1.0


def test_predict_match_identical_without_prematch():
    """不传 prematch 与传 None，结果字典必须完全一致。"""
    model = _model()
    a = MatchAnalyzer().predict_match(model, HOME, AWAY)
    b = MatchAnalyzer().predict_match(model, HOME, AWAY, prematch=None)
    assert a == b


def test_zero_strength_is_a_no_op():
    """强度为 0 时即便有伤停也不修正（线上 A/B 的对照开关）。"""
    factors = squad_factors(HOME, AWAY, injuries=[_injury(HOME)] * 5, strength=0.0)
    assert factors["home"]["attack"] == 1.0
    assert factors["home"]["concede"] == 1.0


def test_empty_factors_leave_lambdas_untouched():
    assert apply_to_lambdas(1.7, 0.9, None) == (1.7, 0.9)
    assert apply_to_lambdas(1.7, 0.9, {}) == (1.7, 0.9)


# ── 方向正确性 ────────────────────────────────────────────────


def test_home_absence_weakens_home_and_strengthens_away():
    """主队缺员 → λ主 下降、λ客 上升，两者方向必须相反。"""
    model = _model()
    base = MatchAnalyzer().predict_match(model, HOME, AWAY)
    factors = squad_factors(HOME, AWAY, injuries=[_injury(HOME)] * 4, strength=1.0)
    adj = MatchAnalyzer().predict_match(model, HOME, AWAY, prematch=factors)
    assert adj["lambda_home"] < base["lambda_home"]
    assert adj["lambda_away"] > base["lambda_away"]


def test_away_absence_is_mirrored():
    """客队缺员是对称的：λ客 下降、λ主 上升。"""
    base = MatchAnalyzer().predict_match(_model(), HOME, AWAY)
    factors = squad_factors(HOME, AWAY, injuries=[_injury(AWAY)] * 4, strength=1.0)
    adj = MatchAnalyzer().predict_match(_model(), HOME, AWAY, prematch=factors)
    assert adj["lambda_away"] < base["lambda_away"]
    assert adj["lambda_home"] > base["lambda_home"]


def test_attack_hurt_more_than_conceding():
    """进攻端折损应大于失球端（少前锋比少后卫更致命）。"""
    factors = squad_factors(HOME, AWAY, injuries=[_injury(HOME)] * 3, strength=1.0)
    attack_drop = 1.0 - factors["home"]["attack"]
    concede_rise = factors["home"]["concede"] - 1.0
    assert attack_drop > concede_rise > 0
    assert attack_drop / concede_rise == pytest.approx(0.6 / CONCEDE_SHARE)


# ── 缺阵权重与饱和 ────────────────────────────────────────────


def test_doubtful_counts_less_than_certain():
    certain = absence_load([_injury(HOME, "Missing Fixture")] * 3, HOME)
    doubtful = absence_load([_injury(HOME, "Questionable")] * 3, HOME)
    assert doubtful < certain


def test_other_team_absences_do_not_count():
    assert absence_load([_injury(AWAY)] * 5, HOME) == 0.0


def test_saturation_never_breaks_the_floor():
    """再多伤停也不能把实力打到零：折损有硬上限。"""
    factors = squad_factors(HOME, AWAY, injuries=[_injury(HOME)] * 40, strength=1.0)
    assert factors["home"]["attack"] >= 1.0 - MAX_LOSS
    assert factors["home"]["concede"] <= 1.0 + MAX_LOSS * CONCEDE_SHARE


def test_more_absentees_mean_more_loss():
    few = loss_factor(1.0, 1.0)
    many = loss_factor(6.0, 1.0)
    assert many < few <= 1.0


def test_zero_absentees_is_exactly_one():
    assert loss_factor(0.0, 1.0) == 1.0
    assert loss_factor(-3.0, 1.0) == 1.0  # 异常负值也要安全


# ── 阵容倾向 ──────────────────────────────────────────────────


def _xi(*positions: str) -> list[dict]:
    return [{"player": {"pos": p, "name": f"P{i}"}} for i, p in enumerate(positions)]


def test_attacking_lineup_tilts_positive():
    assert starters_tilt(_xi("G", "D", "D", "D", "D", "M", "M", "M", "F", "F", "F")) > 0


def test_defensive_lineup_tilts_negative():
    assert starters_tilt(_xi("G", "D", "D", "D", "D", "D", "M", "M", "M", "M", "F")) < 0


def test_empty_or_bad_lineup_tilts_zero():
    assert starters_tilt([]) == 0.0
    assert starters_tilt(None) == 0.0
    assert starters_tilt([{"player": {"pos": "??"}}]) == 0.0


def test_formation_parsing():
    assert formation_tilt("4-3-3") == formation_tilt("4-3-3")
    assert formation_tilt("5-4-1") < 0          # 五后卫偏防守
    assert formation_tilt("4-3-3") >= 0
    assert formation_tilt(None) == 0.0
    assert formation_tilt("乱码") == 0.0


def test_tilt_only_applies_when_weighted():
    lineup = [{"team": {"id": HOME}, "formation": "4-3-3",
               "startXI": _xi("G", "D", "D", "D", "D", "M", "M", "M", "F", "F", "F")}]
    off = squad_factors(HOME, AWAY, lineups=lineup, tilt_weight=0.0)
    on = squad_factors(HOME, AWAY, lineups=lineup, tilt_weight=0.1)
    assert off["home"]["attack"] == 1.0
    assert on["home"]["attack"] > 1.0
    assert on["home"]["concede"] > 1.0   # 压上进攻必然更容易失球


def test_lineup_detail_is_reported():
    lineup = [{"team": {"id": HOME}, "formation": "4-4-2",
               "startXI": _xi("G", "D", "D", "D", "D", "M", "M", "M", "M", "F", "F")}]
    detail = squad_factors(HOME, AWAY, lineups=lineup)["detail"]
    assert detail["home"]["starters"] == 11
    assert detail["home"]["formation"] == "4-4-2"


# ── 健壮性 ────────────────────────────────────────────────────


def test_malformed_records_are_ignored():
    bad = [None, "x", {}, {"team": None, "player": None}, {"team": {"id": HOME}}]
    assert absence_load(bad, HOME) == 0.0
    factors = squad_factors(HOME, AWAY, injuries=bad, lineups=None)
    assert factors["home"]["attack"] == 1.0


def test_lambdas_stay_within_bounds():
    """外部数据再离谱也不能把 λ 推出模型的合法区间。"""
    wild = {"home": {"attack": 99.0, "concede": 99.0},
            "away": {"attack": 99.0, "concede": 99.0}}
    lo, hi = apply_to_lambdas(3.0, 3.0, wild)
    assert 0.05 <= lo <= 6.0
    assert 0.05 <= hi <= 6.0


def test_missing_keys_fall_back_to_one():
    lo, hi = apply_to_lambdas(2.0, 1.0, {"home": {}, "away": {}})
    assert (lo, hi) == (2.0, 1.0)


# ── 数据源接线 ────────────────────────────────────────────────


def test_lineups_registered_as_optional():
    """阵容是可选数据：取不到不能当成「两个源都失败」。"""
    assert "get_lineups" in OPTIONAL_METHODS


def test_lineups_routed_by_fixture_id():
    """按比赛 ID 查询：备用源 ID 不能拿去问主源。"""
    assert "get_lineups" in FIXTURE_ID_METHODS


def test_fallback_source_implements_lineups():
    """data_source 用 getattr 取方法，备用源缺实现会抛 AttributeError。"""
    from football_data import FootballDataAPI
    assert hasattr(FootballDataAPI, "get_lineups")
