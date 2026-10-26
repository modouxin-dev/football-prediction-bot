"""推荐闸门：置信度阈值 + 赔率区间。

为什么要有这道闸门
------------------
「有亚盘才推荐」只解决了**能不能算**的问题，没解决**值不值得推**的问题。
这道闸门解决后者，两道筛子各管一件事，不能互相替代。

置信度阈值：实测标定，可信
--------------------------
用 openfootball 真实赛果（英超 2020-21~2024-25 + 西甲/德甲/意甲，
**3158 场 walk-forward，无前视**）标定：

    阈值    覆盖率    命中率
    0.00     100%     52.18%
    0.45    89.9%     54.00%
    0.55    69.6%     57.30%   ← 默认
    0.65    49.5%     61.33%
    0.70    40.5%     62.44%

⚠️ 必须说清楚：这是**用覆盖率换准确率**，不是模型变强了。
推得越少当然越准。所以拦截数必须可观测——否则无从判断是不是砍过头。

赔率区间：未标定，只防极端
--------------------------
- 下限 1.30：赔率 1.1 的比赛命中率能到 80%+，但押它是负 EV。
  用这种比赛刷出来的"高准确率"是假的，会污染所有统计。
- 上限 5.00：超过 5 倍基本是彩票，方差极大，几百场样本内完全不可信。

⚠️ 这两个数**没有真实赔率标定过**（历史赔率取不到），只用于挡住两端。
要精细调，得先攒真实赔率再看分组统计。所以默认值设得很宽松。

覆盖点 / Coverage
-----------------
* 置信度不足的场次被拦下并计数
* 赔率过低 / 过高的场次分别被拦下并计数
* 阈值设为 0 = 该项不限（保持旧行为）
* 全被拦下时必须留痕，不能静默变成"今天没比赛"
* 闸门统计里每个原因都有数，便于判断误杀
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from service import Prediction, PredictionService


def _mk(asian=None, win=0.60, draw=0.20, loss=0.20):
    """只关心 asian / analysis 的 Prediction，其余字段用占位。

    与 test_no_handicap_no_recommend.py 保持一致，避免各写一套构造方式。
    """
    return Prediction(
        fixture_id=1, league="L", round_label="", venue="",
        home="A", away="B", home_id=1, away_id=2,
        kickoff=datetime.now(timezone.utc),
        analysis={"win_prob": win, "draw_prob": draw, "loss_prob": loss},
        home_strength=None, away_strength=None,
        model=None, fixture={}, asian=asian,
    )


def _ah(home_odds=1.95, away_odds=1.95, ph=0.60, pa=0.40):
    return {"line": -0.5, "n": 5, "prob": {"home": ph, "away": pa},
            "home": {"prob": ph, "odds": home_odds, "edge": 0.0, "ev": 0.0},
            "away": {"prob": pa, "odds": away_odds, "edge": 0.0, "ev": 0.0},
            "overround": 0.04}


class _S:
    def __init__(self, **kw):
        self.rec_min_confidence = kw.get("rec_min_confidence", 0.0)
        self.rec_min_odds = kw.get("rec_min_odds", 0.0)
        self.rec_max_odds = kw.get("rec_max_odds", 0.0)


def _svc(**kw):
    svc = PredictionService.__new__(PredictionService)
    svc.settings = _S(**kw)
    svc.last_note = None
    return svc


def test_confidence_gate_rejects_low_confidence():
    """置信度低于阈值的场次被拦下，并计入 low_confidence。"""
    svc = _svc(rec_min_confidence=0.55)
    kept = svc.gate_predictions([_mk(_ah(), win=0.40, draw=0.30, loss=0.30)])
    assert kept == []
    assert svc.last_gate["low_confidence"] == 1
    assert svc.last_gate["pass"] == 0


def test_confidence_gate_keeps_high_confidence():
    svc = _svc(rec_min_confidence=0.55)
    kept = svc.gate_predictions([_mk(_ah(), win=0.70, draw=0.15, loss=0.15)])
    assert len(kept) == 1
    assert svc.last_gate["pass"] == 1


def test_odds_lower_bound_rejects_short_price():
    """赔率过低（1.1 那种刷准确率的）被拦下。"""
    svc = _svc(rec_min_odds=1.30)
    kept = svc.gate_predictions([_mk(_ah(home_odds=1.12), win=0.80)])
    assert kept == []
    assert svc.last_gate["odds_low"] == 1


def test_odds_upper_bound_rejects_lottery():
    svc = _svc(rec_max_odds=5.00)
    kept = svc.gate_predictions([_mk(_ah(home_odds=8.0), win=0.60)])
    assert kept == []
    assert svc.last_gate["odds_high"] == 1


def test_odds_within_range_passes():
    svc = _svc(rec_min_odds=1.30, rec_max_odds=5.00)
    kept = svc.gate_predictions([_mk(_ah(home_odds=2.10), win=0.60)])
    assert len(kept) == 1


def test_zero_threshold_disables_that_filter():
    """阈值 0 = 不限。保证旧行为不被意外改变。"""
    svc = _svc(rec_min_confidence=0.0, rec_min_odds=0.0, rec_max_odds=0.0)
    kept = svc.gate_predictions([
        _mk(_ah(home_odds=1.05), win=0.35, draw=0.33, loss=0.32)])
    assert len(kept) == 1
    assert svc.last_gate["pass"] == 1


def test_no_handicap_counted_separately():
    """无亚盘与置信度不足是两个原因，不能混成一个数——
    混了就无法判断该调阈值还是该查数据源。"""
    svc = _svc(rec_min_confidence=0.55)
    svc.gate_predictions([_mk(None, win=0.90),
                          _mk(_ah(), win=0.30, draw=0.35, loss=0.35)])
    assert svc.last_gate["no_handicap"] == 1
    assert svc.last_gate["low_confidence"] == 1
    assert svc.last_gate["pass"] == 0


def test_all_rejected_must_leave_a_note():
    """全被拦下时必须说明原因，不能静默变成「今天没比赛」。"""
    svc = _svc(rec_min_confidence=0.90)
    svc.gate_predictions([_mk(_ah(), win=0.50, draw=0.25, loss=0.25)])
    assert svc.last_note and "闸门" in svc.last_note


def test_gate_counts_every_reason():
    """统计字典必须包含全部原因键——缺一个键，那类误杀就永远看不见。"""
    svc = _svc(rec_min_confidence=0.55, rec_min_odds=1.30, rec_max_odds=5.0)
    svc.gate_predictions([])
    for k in ("in", "no_handicap", "low_confidence", "odds_low",
              "odds_high", "no_odds", "pass"):
        assert k in svc.last_gate


def test_recommended_odds_uses_recommended_side_only():
    """取推荐那一侧的赔率，不是两侧均值。

    均值会把「稳的一侧 1.3」和「险的一侧 4.5」混成 2.9，
    这个数字对应的注根本不存在。
    """
    p = _mk(_ah(home_odds=1.40, away_odds=3.20, ph=0.62, pa=0.38))
    assert p.asian_side() == "home"
    assert p.recommended_odds() == pytest.approx(1.40)


def test_recommended_odds_none_without_handicap():
    assert _mk(None).recommended_odds() is None


def test_swapped_bounds_are_normalized():
    """上下限写反时自动纠正，不能让区间变成空的（那会拦掉一切）。"""
    svc = _svc(rec_min_odds=5.0, rec_max_odds=1.3)
    kept = svc.gate_predictions([_mk(_ah(home_odds=2.0), win=0.60)])
    assert len(kept) == 1
