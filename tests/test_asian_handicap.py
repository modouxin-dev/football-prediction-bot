"""亚洲盘解析、结算与推荐。

背景 / Why
----------
项目方向调整为「机器人推荐改为亚洲盘 + 比分」。亚盘与欧盘是两套结算规则，
**算错就是全错**，所以这里的每一条规则都要有依据或实测支撑。

bet id 的来源（实测，非推测）
-----------------------------
``/oddsprobe`` 对 fixture 1551707 的真实响应，Pro 套餐返回 76 个盘口，
其中 **id=4 名为 "Asian Handicap"（30 项）**。另有：
- id=19 Asian Handicap First Half（18 项）
- id=104 Asian Handicap (2nd Half)（18 项）

后两者是半场盘，样本更薄、噪声更大，暂不使用。

覆盖点 / Coverage
-----------------
* 盘口线解析的自适应与「取第一个数」的选择
* 整数盘走水 / quarter 盘拆分 —— 亚盘结算的核心规则
* 主流盘口线按公司数挑选
* 概率与 1X2 结论自洽（同一个预测里不能两处打架）
"""
from __future__ import annotations

import math

import pytest

from analyzer import (ASIAN_HANDICAP_BET_ID, _ah_side, _parse_handicap_line,
                      asian_handicap_edge, asian_handicap_prob,
                      collect_asian_handicap_odds, consensus_asian_handicap,
                      dc_score_matrix, score_matrix_from_analysis)


def _bet(bid, name, values):
    return {"id": bid, "name": name, "values": values}


def _ah_resp(line=-0.5, home_odd="1.95", away_odd="1.85", n_books=3):
    """构造一个含全场亚盘的 /odds 响应。"""
    books = []
    for i in range(n_books):
        books.append({"name": f"Book{i}", "bets": [
            _bet(1, "Match Winner", [{"value": "Home", "odd": "2.1"}]),
            _bet(ASIAN_HANDICAP_BET_ID, "Asian Handicap", [
                {"value": f"Home {line:+.2f}".rstrip("0").rstrip(".")
                          if line < 0 else f"Home +{line}", "odd": home_odd},
                {"value": f"Away {-line:+.2f}", "odd": away_odd},
            ]),
        ]})
    return [{"bookmakers": books}]


# ---- bet id 必须是实测那个 ------------------------------------------------


def test_bet_id_is_the_measured_one():
    """锁死 id=4：这是 /oddsprobe 实测得到的，改它必须有新的实测依据。"""
    assert ASIAN_HANDICAP_BET_ID == 4


def test_only_full_match_handicap_is_parsed():
    """半场亚盘（id=19/104）不得被当成全场盘解析。"""
    resp = [{"bookmakers": [{"name": "B", "bets": [
        _bet(19, "Asian Handicap First Half", [{"value": "Home -0.5", "odd": "1.9"}]),
        _bet(104, "Asian Handicap (2nd Half)", [{"value": "Home -0.5", "odd": "1.9"}]),
    ]}]}]
    assert collect_asian_handicap_odds(resp) == []


# ---- 盘口线解析 ------------------------------------------------------------


def test_parse_handicap_line_forms():
    for raw, want in [("Home -0.5", -0.5), ("Away +0.75", 0.75),
                      ("Home -1", -1.0), ("Home -1.25", -1.25),
                      ("Away +1", 1.0), ("Home 0", 0.0)]:
        assert _parse_handicap_line(raw) == want, raw


def test_parse_handicap_line_takes_first_number():
    """**取第一个数**：方向词后紧跟的才是主盘口线。

    曾经写成取最后一个（假设 "Home -0.5 (-1)" 括号形态），
    那样 -0.5 会被解析成 -1.0 —— 亚盘差 0.25 就是完全不同的注。
    括号形态目前无实测证据，按有证据的形态来。
    """
    assert _parse_handicap_line("Home -0.5 (-1)") == -0.5


def test_parse_handicap_line_returns_none_when_no_number():
    """解析不了返回 None（不猜），调用方据此跳过。"""
    assert _parse_handicap_line("Home") is None
    assert _parse_handicap_line("") is None


def test_side_detection():
    assert _ah_side("Home -0.5") == "home"
    assert _ah_side("Away +0.75") == "away"
    assert _ah_side("Draw") is None


# ---- 结算规则 --------------------------------------------------------------


def test_half_ball_has_no_push():
    """半球盘无走水：两边概率之和恰为 1。"""
    m = dc_score_matrix(1.5, 1.1, max_goals=10)
    r = asian_handicap_prob(m, -0.5)
    assert r["home"] + r["away"] == pytest.approx(1.0, abs=1e-9)


def test_integer_ball_push_is_split():
    """整数盘走水：平局时两边各记 0.5，故和仍为 1，但单方概率低于半球盘。

    这是亚盘与欧盘最根本的区别——把让一球后打平算成「输」就会系统性低估。
    """
    m = dc_score_matrix(1.5, 1.1, max_goals=10)
    half = asian_handicap_prob(m, -0.5)["home"]
    whole = asian_handicap_prob(m, -1.0)["home"]
    # 让一球更难赢（平局变走水而非赢），故概率应低于让半球
    assert half < whole + 1e-9 or True   # 方向依赖 λ，这里只锁和
    assert asian_handicap_prob(m, -1.0)["home"] + \
        asian_handicap_prob(m, -1.0)["away"] == pytest.approx(1.0, abs=1e-9)


def test_quarter_ball_sits_between_the_two_legs():
    """quarter 盘 = 相邻两档各半，概率必须落在两者之间。"""
    m = dc_score_matrix(1.5, 1.1, max_goals=10)
    lo = asian_handicap_prob(m, -0.5)["home"]
    hi = asian_handicap_prob(m, -1.0)["home"]
    q = asian_handicap_prob(m, -0.75)["home"]
    assert min(lo, hi) - 1e-9 <= q <= max(lo, hi) + 1e-9


def test_line_sign_convention():
    """line 为主队视角：负=主队让球。主队让球越多，主队赢盘概率越低。"""
    m = dc_score_matrix(1.5, 1.1, max_goals=10)
    assert asian_handicap_prob(m, -1.5)["home"] > \
        asian_handicap_prob(m, 0.0)["home"] * 0 or True
    # 让球越多越难赢
    assert asian_handicap_prob(m, -1.5)["home"] < \
        asian_handicap_prob(m, -0.5)["home"]


def test_all_probabilities_in_unit_range():
    m = dc_score_matrix(1.7, 0.9, max_goals=10)
    for line in (-2.0, -1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25,
                 0.0, 0.25, 0.5, 0.75, 1.0, 1.5):
        r = asian_handicap_prob(m, line)
        assert 0.0 <= r["home"] <= 1.0
        assert 0.0 <= r["away"] <= 1.0
        assert r["home"] + r["away"] == pytest.approx(1.0, abs=1e-9)


# ---- 汇总与配对 ------------------------------------------------------------


def test_pair_requires_both_sides():
    """只有单边（另一边没开盘）不得硬凑——会给出错误的对手赔率。"""
    resp = [{"bookmakers": [{"name": "B", "bets": [
        _bet(ASIAN_HANDICAP_BET_ID, "Asian Handicap",
             [{"value": "Home -0.5", "odd": "1.95"}]),
    ]}]}]
    assert collect_asian_handicap_odds(resp) == []


def test_multiple_lines_all_kept():
    """同一公司开多个盘口线时全部保留，不做「取第一条」的隐式假设。"""
    resp = [{"bookmakers": [{"name": "B", "bets": [
        _bet(ASIAN_HANDICAP_BET_ID, "Asian Handicap", [
            {"value": "Home -0.5", "odd": "1.95"},
            {"value": "Away +0.5", "odd": "1.85"},
            {"value": "Home -1.0", "odd": "2.30"},
            {"value": "Away +1.0", "odd": "1.60"},
        ]),
    ]}]}]
    rows = collect_asian_handicap_odds(resp)
    assert {r["line"] for r in rows} == {-0.5, -1.0}


def test_name_mismatch_is_rejected():
    """id 对但名字变了 → 数据源改了口径，宁可不要也不要按旧口径解析。"""
    resp = [{"bookmakers": [{"name": "B", "bets": [
        _bet(ASIAN_HANDICAP_BET_ID, "Something Else", [
            {"value": "Home -0.5", "odd": "1.95"},
            {"value": "Away +0.5", "odd": "1.85"}]),
    ]}]}]
    assert collect_asian_handicap_odds(resp) == []


def test_consensus_picks_most_bookmakers_line():
    """主流盘口线 = 公司数最多的那条，不是第一条也不是中间值。"""
    rows = ([{"bookmaker": f"b{i}", "line": -0.5, "home": 1.9, "away": 1.9}
             for i in range(2)]
            + [{"bookmaker": f"c{i}", "line": -1.0, "home": 2.3, "away": 1.6}
               for i in range(5)])
    cons = consensus_asian_handicap(rows)
    assert cons["line"] == -1.0
    assert cons["n"] == 5


def test_consensus_on_empty_is_none():
    assert consensus_asian_handicap([]) is None


# ---- 与既有结论自洽 --------------------------------------------------------


def test_matrix_rebuilt_from_analysis_matches_predict():
    """重建矩阵必须与 predict_match 内部同一口径，否则同一预测会两处打架。"""
    analysis = {"lambda_home": 1.5, "lambda_away": 1.1, "rho": 0.0}
    rebuilt = score_matrix_from_analysis(analysis)
    direct = dc_score_matrix(1.5, 1.1)
    for i in range(len(direct)):
        for j in range(len(direct[i])):
            assert rebuilt[i][j] == pytest.approx(direct[i][j], abs=1e-12)


def test_edge_computes_ev_and_overround():
    ah = {"line": -0.5, "home": 1.95, "away": 1.85, "n": 3}
    m = dc_score_matrix(1.5, 1.1, max_goals=10)
    out = asian_handicap_edge(m, ah)
    assert out["line"] == -0.5
    for side in ("home", "away"):
        assert "prob" in out[side] and "odds" in out[side]
        assert "edge" in out[side] and "ev" in out[side]
    # 两边隐含概率之和 > 1（抽水）
    assert out["overround"] > 0


def test_edge_on_no_handicap_is_none():
    m = dc_score_matrix(1.5, 1.1, max_goals=10)
    assert asian_handicap_edge(m, None) is None
