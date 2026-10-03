"""射正数（Shots on Target）作为强度指标的测试。

为什么值得单独测
----------------
一场比赛射正 4~6 次，进球只有 1~2 个。同样的赛季进度下射正数样本量大得多，
噪声更小、收敛更快，赛季初尤其明显。football-data.co.uk 的 CSV 自带该字段。

两条必须钉死的边界
------------------
1. **半份数据整盘作废**：只给 HST 不给 AST，会让一侧用射正、另一侧用进球，
   两个量纲混算出的强度毫无意义。宁可退回进球口径。
2. **λ 基准必须是进球**：射正均值约 4~5，若误当成场均进球，
   模型会输出「预期进球 4 个」——数值荒谬但不会报错，是最危险的静默错误。
"""

from __future__ import annotations

import pytest

from backtest import WalkForwardBacktester, _blank_stats, _standings_row_from_history
from backtest_corpus import fixture_to_match
from football_data_uk import to_fixture


# ---- 一、解析层：CSV → fixture ---------------------------------------------

def _csv_row(**over):
    row = {
        "Date": "12/08/2023", "Time": "20:00",
        "HomeTeam": "Arsenal", "AwayTeam": "Leeds",
        "FTHG": "2", "FTAG": "1",
        "HST": "7", "AST": "3",
    }
    row.update(over)
    return row


def test_shots_on_target_extracted():
    """完整射正字段必须出现在 fixture 里。"""
    fx = to_fixture(_csv_row(), season=2023)
    assert fx["shotsOnTarget"] == {"home": 7, "away": 3}


def test_half_shots_dropped_entirely():
    """只有主队射正 → 整盘作废（不能半射正半进球混算）。"""
    fx = to_fixture(_csv_row(AST=""), season=2023)
    assert "shotsOnTarget" not in fx


def test_missing_shots_keeps_fixture_usable():
    """没有射正数不影响基本可用性——生产上 API-Football 默认就没有。"""
    fx = to_fixture(_csv_row(HST="", AST=""), season=2023)
    assert "shotsOnTarget" not in fx
    assert fx["score"]["fullTime"]["home"] == 2


def test_zero_shots_on_target_is_valid():
    """0 射正是合法值，不能当成缺失。

    与 _to_score 同理：0-0 的射正分布真实存在，若把 0 当缺失，
    弱队的数据会被系统性丢弃，强度反而偏乐观。
    """
    fx = to_fixture(_csv_row(HST="0", AST="0"), season=2023)
    assert fx["shotsOnTarget"] == {"home": 0, "away": 0}


# ---- 二、语料层：fixture → match -------------------------------------------

def test_corpus_carries_shots():
    m = fixture_to_match({
        "id": "x1", "utcDate": "2023-08-12T20:00:00Z", "competition": "PL",
        "season": {"startDate": "2023-08-01"},
        "homeTeam": {"id": "a", "name": "A"}, "awayTeam": {"id": "b", "name": "B"},
        "score": {"fullTime": {"home": 2, "away": 1}},
        "shotsOnTarget": {"home": 7, "away": 3},
    })
    assert m["home_sot"] == 7 and m["away_sot"] == 3


def test_corpus_without_shots_is_none():
    """无射正时字段为 None，建模层据此退回进球口径。"""
    m = fixture_to_match({
        "id": "x2", "utcDate": "2023-08-12T20:00:00Z", "competition": "PL",
        "season": {"startDate": "2023-08-01"},
        "homeTeam": {"id": "a", "name": "A"}, "awayTeam": {"id": "b", "name": "B"},
        "score": {"fullTime": {"home": 2, "away": 1}},
    })
    assert m["home_sot"] is None and m["away_sot"] is None


# ---- 三、建模层：口径切换与 λ 量纲 -----------------------------------------

def test_standings_row_switches_metric():
    """use_sot=True 且有累积时，standings 行走射正数。"""
    st = _blank_stats("t1")
    st["home_played"] = st["away_played"] = 5
    st["home_for"] = st["away_for"] = 8      # 进球口径
    st["home_against"] = st["away_against"] = 4
    st["home_sot_played"] = st["away_sot_played"] = 5
    st["home_sot_for"] = st["away_sot_for"] = 22   # 射正口径
    st["home_sot_against"] = st["away_sot_against"] = 11

    goals_row = _standings_row_from_history(st, False)
    assert goals_row["home"]["goals"]["for"] == 8

    sot_row = _standings_row_from_history(st, True)
    assert sot_row["home"]["goals"]["for"] == 22


def test_standings_row_falls_back_without_sot():
    """没有射正累积的球队，即使 use_sot=True 也必须用进球口径。"""
    st = _blank_stats("t2")
    st["home_played"] = 3; st["home_for"] = 5; st["home_against"] = 2
    row = _standings_row_from_history(st, True)
    assert row["home"]["goals"]["for"] == 5


def test_lambda_stays_in_goal_scale_under_sot():
    """λ 必须是进球量纲，不能是射正量纲。

    射正均值约 4~5，若误用会输出「预期进球 4 个」。

    ⚠️ 构造要点：两队必须**对称**交替主客场，让攻防强度都 ≈1.0。
    若只有一方有主场数据，defense_away 会偏离 1.0（实测 0.394），
    恰好把 λ 抵消回合理区间，量纲错误就测不出来了 —— 这个坑踩过一次。
    """
    t = WalkForwardBacktester(min_history=30, use_sot=True)
    # 交替主客场：两队各打 15 主 15 客，数据完全对称
    for i in range(30):
        if i % 2 == 0:
            h, a = "teamA", "teamB"
        else:
            h, a = "teamB", "teamA"
        t._observe(h, a, 2, 1, 6, 3)
    _, lh, la = t._predict_detail("teamA", "teamB")
    assert lh is not None
    # 进球口径下 λ 应接近真实场均进球（2.0 / 1.0）
    assert 0.05 <= lh <= 4.0, f"λ主={lh} 疑似用了射正量纲（射正均值 6）"
    assert 0.05 <= la <= 4.0, f"λ客={la} 疑似用了射正量纲"


def test_sot_off_keeps_legacy_behavior():
    """use_sot 默认关闭 —— 拿不到射正数的数据源必须保持原行为。"""
    t = WalkForwardBacktester(min_history=30)
    assert t.use_sot is False
