"""赛前上下文信号（球员评估 / 伤停位置加权 / 天气）的单元测试。

红线 / The red line
-------------------
与 `test_prematch.py` 同一条：**没有信号时乘数必须精确等于 1.0**。
这是唯一能证明新代码没有悄悄改坏线上预测的方法。

    Same red line as `test_prematch.py`: with no signals the multipliers must be
    **exactly 1.0**. It is the only proof that the new code did not silently
    change live predictions.

另有一条本模块特有的红线：**天气默认关闭**。`DEFAULT_WEATHER_STRENGTH` 必须为
0 —— 公开研究结论互相矛盾，未经验证的信号不许默认生效。

    One more invariant specific to this module: **weather is off by default**.
    `DEFAULT_WEATHER_STRENGTH` must be 0 — published studies disagree, so an
    unvalidated signal must not be live by default.
"""
from __future__ import annotations

import math

import pytest

from context import (
    BASELINE_RATING,
    DEFAULT_RATING_STRENGTH,
    DEFAULT_WEATHER_STRENGTH,
    DEFENCE_SHARE,
    MAX_RATING_EFFECT,
    MAX_WEATHER_EFFECT,
    apply_weather,
    context_factors,
    merge_factors,
    player_ratings,
    position_weight,
    rating_effect,
    squad_positions,
    weather_effect,
    weighted_absence_load,
    xi_rating,
)
from weather import WeatherSample

HOME, AWAY = 1, 2


def _injury(team_id: int, pid: str, kind: str = "Missing Fixture") -> dict:
    return {"team": {"id": team_id}, "player": {"id": pid, "type": kind}}


def _xi(ids: list[str]) -> list[dict]:
    return [{"player": {"id": i, "pos": "M"}} for i in ids]


# ---- 红线 ------------------------------------------------------------------

def test_no_signals_is_exactly_neutral():
    """无任何信号 → 四个乘数与 weather 全部精确为 1.0。"""
    f = context_factors(HOME, AWAY)
    assert f["home"] == {"attack": 1.0, "concede": 1.0}
    assert f["away"] == {"attack": 1.0, "concede": 1.0}
    assert f["weather"] == 1.0


def test_weather_defaults_off():
    """天气默认关闭：这是本模块最重要的安全不变量。"""
    assert DEFAULT_WEATHER_STRENGTH == 0.0
    # 即使给了极端天气样本，强度为 0 也必须完全不生效
    assert weather_effect(WeatherSample(0, 20.0, 90, 99), DEFAULT_WEATHER_STRENGTH) == 0.0


def test_strength_zero_disables_rating():
    """强度 0 时，即使有满级评分也不改结果。"""
    xi = _xi([str(i) for i in range(11)])
    ratings = {str(i): (9.9, 3000) for i in range(11)}
    f = context_factors(HOME, AWAY, start_xi={str(HOME): xi},
                        ratings={str(HOME): ratings}, rating_strength=0.0)
    assert f["home"]["attack"] == 1.0
    assert f["home"]["concede"] == 1.0


# ---- 位置权重 --------------------------------------------------------------

def test_position_weight_known_and_unknown():
    assert position_weight("F") > position_weight("M") > position_weight("D")
    assert position_weight("Attacker") == position_weight("F")
    assert position_weight("Goalkeeper") == position_weight("G")
    # 无法识别的位置必须是中性 1.0，不能变成 0（那会让缺阵凭空消失）
    assert position_weight(None) == 1.0
    assert position_weight("边后卫") == 1.0


def test_squad_positions_two_shapes():
    """兼容 /players/squads 的嵌套形态与扁平形态。"""
    nested = [{"player": {"id": 7, "pos": "F"}}]
    flat = [{"id": 8, "position": "Defender"}]
    assert squad_positions(nested) == {"7": "F"}
    assert squad_positions(flat) == {"8": "Defender"}
    assert squad_positions(None) == {}
    assert squad_positions([{"player": {"id": 9}}]) == {}   # 无位置则跳过


def test_missing_striker_hurts_more_than_defender():
    """同样 1 人缺阵，前锋的负荷必须高于后卫。"""
    positions = {"p1": "F", "p2": "D"}
    fwd = weighted_absence_load([_injury(HOME, "p1")], HOME, positions)
    dfn = weighted_absence_load([_injury(HOME, "p2")], HOME, positions)
    assert fwd > dfn


def test_absence_load_degrades_gracefully_without_positions():
    """拿不到名单时退化为按人数计，不能变成 0 或直接抛错。"""
    assert weighted_absence_load([_injury(HOME, "p1")], HOME, None) == 1.0
    assert weighted_absence_load([_injury(HOME, "p1")], HOME, {}) == 1.0


def test_absence_load_ignores_other_team_and_doubtful_discounted():
    positions = {"p1": "F"}
    assert weighted_absence_load([_injury(AWAY, "p1")], HOME, positions) == 0.0
    certain = weighted_absence_load([_injury(HOME, "p1", "Missing Fixture")], HOME, positions)
    doubt = weighted_absence_load([_injury(HOME, "p1", "Questionable")], HOME, positions)
    assert 0 < doubt < certain


def test_player_without_id_counted_neutrally():
    """无 id 的记录仍算『有人缺阵』，只是无法按位置加权（中性 1.0）。

    不能跳过：跳过会系统性**低估**缺阵人数。也不能凭空套用别的位置。
    """
    bad = [{"team": {"id": HOME}, "player": {"name": "X", "type": "Missing Fixture"}}]
    # 即便 positions 里恰好有个空串键，也不该被命中
    assert weighted_absence_load(bad, HOME, {"": "F"}) == 1.0


def test_record_without_player_dict_is_skipped():
    """连 player 字段都没有的残缺记录不能算数。"""
    assert weighted_absence_load([{"team": {"id": HOME}}], HOME, {}) == 0.0
    assert weighted_absence_load([{"team": {"id": HOME}, "player": {}}], HOME, {}) == 0.0


# ---- 球员评估 --------------------------------------------------------------

def test_player_ratings_parses_and_picks_longest_stint():
    raw = [{
        "player": {"id": 5},
        "statistics": [
            {"games": {"rating": "6.50", "minutes": 200}},
            {"games": {"rating": "7.40", "minutes": 1500}},
        ],
    }]
    assert player_ratings(raw) == {"5": (7.4, 1500.0)}


def test_player_ratings_skips_garbage():
    assert player_ratings(None) == {}
    assert player_ratings([{"player": {"id": 1}, "statistics": [{"games": {}}]}]) == {}
    # rating 为 None / 0 / 非数字都不能进入均值
    assert player_ratings([{"player": {"id": 1}, "statistics": [
        {"games": {"rating": None, "minutes": 900}}]}]) == {}


def test_xi_rating_weighted_by_minutes():
    """出场时间多的人权重大 —— 这是『主力缺阵影响更大』的数学来源。"""
    ratings = {"a": (9.0, 3000), "b": (6.0, 100)}
    # b 只有 100 分钟，权重被压到 0.15 保底，均值应贴近 a
    got = xi_rating(_xi(["a", "b"]), ratings)
    assert got is not None
    assert got > 8.0


def test_xi_rating_none_when_no_data():
    assert xi_rating(None, {"a": (7.0, 900)}) is None
    assert xi_rating(_xi(["a"]), None) is None
    assert xi_rating(_xi(["zzz"]), {"a": (7.0, 900)}) is None   # 首发无人有评分


def test_rating_effect_direction_and_bounds():
    strong = rating_effect(7.8, 1.0)
    weak = rating_effect(6.0, 1.0)
    assert strong > 0 > weak
    assert abs(strong) <= MAX_RATING_EFFECT
    assert abs(weak) <= MAX_RATING_EFFECT
    assert rating_effect(BASELINE_RATING, 1.0) == 0.0


def test_rating_effect_saturates():
    """极端评分不许线性放大，必须被 MAX 截断。"""
    assert rating_effect(99.0, 1.0) <= MAX_RATING_EFFECT + 1e-12
    assert rating_effect(-99.0, 1.0) >= -MAX_RATING_EFFECT - 1e-12


def test_strong_xi_raises_attack_lowers_concede():
    xi = _xi([str(i) for i in range(11)])
    ratings = {str(i): (7.6, 2000) for i in range(11)}
    f = context_factors(HOME, AWAY, start_xi={str(HOME): xi},
                        ratings={str(HOME): ratings})
    assert f["home"]["attack"] > 1.0
    assert f["home"]["concede"] < 1.0
    # 防守端效应必须小于进攻端
    assert (1.0 - f["home"]["concede"]) < (f["home"]["attack"] - 1.0)
    assert math.isclose(1.0 - f["home"]["concede"],
                        (f["home"]["attack"] - 1.0) * DEFENCE_SHARE, rel_tol=1e-9)


def test_other_side_unaffected_when_only_home_has_data():
    """只给了主队数据，客队必须保持中性。"""
    xi = _xi([str(i) for i in range(11)])
    ratings = {str(i): (7.6, 2000) for i in range(11)}
    f = context_factors(HOME, AWAY, start_xi={str(HOME): xi},
                        ratings={str(HOME): ratings})
    assert f["away"] == {"attack": 1.0, "concede": 1.0}


# ---- 天气 ------------------------------------------------------------------

def test_weather_effect_none_and_comfortable():
    assert weather_effect(None, 1.0) == 0.0
    # 舒适天气几乎不影响
    assert weather_effect(WeatherSample(16, 0.0, 6, 60), 1.0) < 0.02


def test_weather_effect_severe_conditions():
    mild = weather_effect(WeatherSample(16, 0.0, 6, 60), 1.0)
    severe = weather_effect(WeatherSample(2, 8.0, 60, 95), 1.0)
    assert severe > mild
    assert severe <= MAX_WEATHER_EFFECT + 1e-12


def test_weather_effect_renormalises_on_missing_fields():
    """缺项时按实际拿到的项归一化，不能系统性低估。

    只给满格降水（4mm/h）时应得到满格效应，而不是被另外两项的 0 摊薄。
    """
    only_rain = weather_effect(WeatherSample(None, 4.0, None, None), 1.0)
    assert math.isclose(only_rain, MAX_WEATHER_EFFECT, rel_tol=1e-9)


def test_apply_weather_is_symmetric():
    """天气对双方对称：两个 λ 乘同一个数。"""
    h, a = apply_weather(1.6, 1.1, 0.9)
    assert math.isclose(h, 1.6 * 0.9)
    assert math.isclose(a, 1.1 * 0.9)
    assert apply_weather(1.6, 1.1, 1.0) == (1.6, 1.1)
    assert apply_weather(1.6, 1.1, None) == (1.6, 1.1)


# ---- 合并 ------------------------------------------------------------------

def test_merge_factors_multiplies_same_fields():
    a = {"home": {"attack": 1.1, "concede": 0.9}, "away": {"attack": 1.0, "concede": 1.0},
         "weather": 1.0, "detail": {"home": {"a": 1}}}
    b = {"home": {"attack": 1.2, "concede": 1.0}, "away": {"attack": 0.8, "concede": 1.1},
         "weather": 0.95, "detail": {"home": {"b": 2}}}
    m = merge_factors(a, b)
    assert math.isclose(m["home"]["attack"], 1.32)
    assert math.isclose(m["home"]["concede"], 0.9)
    assert math.isclose(m["away"]["attack"], 0.8)
    assert math.isclose(m["weather"], 0.95)
    assert m["detail"]["home"] == {"a": 1, "b": 2}


def test_merge_factors_skips_none_and_returns_none_when_empty():
    a = {"home": {"attack": 1.1}, "away": {}, "weather": 1.0, "detail": {}}
    assert merge_factors(a, None)["home"]["attack"] == 1.1
    assert merge_factors(None) is None
    assert merge_factors() is None


def test_rating_default_is_half_strength():
    """与 prematch 口径一致：默认半强度，不是满强度。"""
    assert DEFAULT_RATING_STRENGTH == 0.5
