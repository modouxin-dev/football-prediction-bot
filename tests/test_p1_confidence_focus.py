"""P0/P1 复核：伤停硬编码矛盾 + 焦点战按真实信心等级排序。

字段依据（远端 main 实测）：
- 信心等级真实字段是 predictions.level_key，取值 high/medium/low/unknown
  LEVEL_NAMES = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低"}
- 不存在 pick_status / confidence_for / compute_confidence（全仓 0 命中）
"""
import pytest
from datetime import datetime, timezone

from analyzer import calculate_prediction_level
from views import digest


def _mk(home, away, home_prob, draw_prob, away_prob, lid=39, fid=1, kickoff=None):
    class P:
        pass
    p = P()
    p.home, p.away = home, away
    p.analysis = {"win_prob": home_prob, "draw_prob": draw_prob,
                  "loss_prob": away_prob}
    p.fixture_id = fid
    p.kickoff = kickoff or datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)
    p.fixture = {"league": {"id": lid}}
    return p


def test_level_key_high_is_most_confident():
    """high 是最有把握的一档，不是最低。"""
    lv = calculate_prediction_level(
        {"home_win": 0.75, "draw": 0.15, "away_win": 0.10})
    assert lv["key"] == "high"
    lv2 = calculate_prediction_level(
        {"home_win": 0.36, "draw": 0.33, "away_win": 0.31})
    assert lv2["key"] == "low"


def test_focus_ranks_high_confidence_first():
    """焦点战应优先高信心场次，而不是只按联赛优先级。"""
    strong = _mk("A", "B", 0.80, 0.12, 0.08, lid=140, fid=1)   # 西甲 高信心
    weak = _mk("C", "D", 0.36, 0.33, 0.31, lid=39, fid=2)      # 英超 低信心
    picked = digest._pick_focus([weak, strong], limit=1)
    assert picked[0].fixture_id == 1, "高信心场次应被选为焦点战"


def test_focus_is_deterministic():
    """同一批输入多次调用结果一致。"""
    items = [_mk("A", "B", 0.80, 0.12, 0.08, fid=1),
             _mk("C", "D", 0.36, 0.33, 0.31, fid=2),
             _mk("E", "F", 0.55, 0.25, 0.20, fid=3)]
    a = [p.fixture_id for p in digest._pick_focus(items, limit=3)]
    b = [p.fixture_id for p in digest._pick_focus(list(reversed(items)), limit=3)]
    assert a == b


def test_analysis_view_has_no_hardcoded_injury_contradiction():
    """深度分析页不得再写死「套餐不支持」（与真实伤停分节矛盾）。"""
    import inspect
    import views.analysis as va
    src = inspect.getsource(va)
    assert "当前套餐不支持" not in src


def test_factors_no_unconditional_injury_unknown():
    """有伤停名单时，不得再往不确定因素里塞「未提供伤停」。"""
    from formatkit import _factors
    report = {
        "has_team_data": True,
        "model": {
            "home_strength": _mk_strength(), "away_strength": _mk_strength(),
        },
        "home_form": {"played": 5, "win": 3, "draw": 1, "lose": 1},
        "away_form": {"played": 5, "win": 2, "draw": 1, "lose": 2},
        "h2h": {"played": 4, "win": 2, "draw": 1, "lose": 1},
        "injuries": {"home": ["张三（膝盖）"], "away": ["李四（停赛）"]},
    }
    _, _, unknowns = _factors(report)
    assert not any("伤停" in u for u in unknowns), f"有伤停却仍报未提供：{unknowns}"


def _mk_strength():
    class S:
        attack_home = 1.0
        defense_away = 1.0
        attack_away = 1.0
        defense_home = 1.0
        games_home = 10
        games_away = 10
    return S()
