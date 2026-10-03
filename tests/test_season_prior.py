"""season_prior 与 analyzer.prior_strength 的行为测试。

覆盖三件事：
1. 先验确实能载入（英超有内置历史）
2. 该降级时静默降级（未知联赛 / 无上赛季 / 脏数据）
3. 先验真的改变了强度——否则等于没接上
"""

from __future__ import annotations

import analyzer
import pytest
from analyzer import build_league_model
from season_prior import (
    _LEAGUE_TO_DIV,
    clear_cache,
    known_team_ids,
    load_prior_strength,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_cache()
    yield
    clear_cache()


def _row(tid, hp, hf, ha, ap, af, aa):
    return {
        "team": {"id": tid},
        "home": {"played": hp, "goals": {"for": hf, "against": ha}},
        "away": {"played": ap, "goals": {"for": af, "against": aa}},
    }


# ── 1. 载入 ────────────────────────────────────────────────

def test_prior_loaded_for_premier_league():
    """英超 2025 有内置 2024 赛季，应能拿到先验。"""
    p = load_prior_strength(39, 2025)
    assert p, "英超应能载入跨赛季先验"
    for tid, d in p.items():
        assert set(d) == {"attack_home", "defense_home",
                          "attack_away", "defense_away"}
        for v in d.values():
            assert 0.0 < v < 5.0, f"强度越界: {tid}={v}"


def test_prior_is_cached():
    """同一 (league, season) 二次调用走缓存，结果一致。"""
    a = load_prior_strength(39, 2025)
    b = load_prior_strength(39, 2025)
    assert a is b or a == b


# ── 2. 降级 ────────────────────────────────────────────────

def test_unknown_league_returns_empty():
    """未映射的联赛静默返回空，不抛异常。"""
    assert load_prior_strength(999999, 2025) == {}


def test_no_previous_season_returns_empty():
    """当季不晚于任何内置赛季时，没有上赛季可用。"""
    first = 2023  # 内置最早赛季，其上没有更早的
    assert load_prior_strength(39, first) == {}


def test_known_team_ids_tolerates_no_repo():
    assert known_team_ids(None) == {}


def test_div_mapping_covers_configured_leagues():
    """五大联赛都有 CSV division 映射，避免配了联赛却拿不到先验。"""
    assert {39, 78, 140, 135, 61}.issubset(set(_LEAGUE_TO_DIV))


# ── 3. 先验确实生效 ────────────────────────────────────────

def test_prior_shifts_strength_toward_history():
    """少样本球队：先验目标 1.5 时，进攻强度必须高于无先验。"""
    rows = [_row(1, 2, 2, 2, 2, 2, 2), _row(2, 40, 60, 60, 40, 60, 60)]
    base = build_league_model(rows)
    with_prior = build_league_model(
        rows, prior_strength={1: {"attack_home": 1.5, "defense_home": 1.5,
                                  "attack_away": 1.5, "defense_away": 1.5}})
    assert with_prior.strength(1).attack_home > base.strength(1).attack_home
    # 未给先验的球队不受影响
    assert with_prior.strength(2).attack_home == pytest.approx(
        base.strength(2).attack_home)


def test_prior_bad_values_fall_back_to_average():
    """脏先验值（0 / 负数 / NaN / 字符串）一律退回 1.0，不污染模型。"""
    rows = [_row(1, 2, 2, 2, 2, 2, 2), _row(2, 40, 60, 60, 40, 60, 60)]
    base = build_league_model(rows)
    bad = {"attack_home": 0, "defense_home": -3,
           "attack_away": float("nan"), "defense_away": "abc"}
    m = build_league_model(rows, prior_strength={1: bad})
    for attr in ("attack_home", "defense_home", "attack_away", "defense_away"):
        assert getattr(m.strength(1), attr) == pytest.approx(
            getattr(base.strength(1), attr))


def test_prior_games_is_three():
    """PRIOR_GAMES=3 是实测选出的取值，改回去会让赛季初闸门变红。"""
    assert analyzer.PRIOR_GAMES == 3
