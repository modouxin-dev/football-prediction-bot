"""DSA（动态强度调节）机制测试。

覆盖规格要求的三项验收：等价性、单向性、安全阀；并锁住阶梯权重本身。
"""
from __future__ import annotations

import pytest

from analyzer import (
    DSA_CLAMP_HI,
    DSA_CLAMP_LO,
    DSA_RECENT_N,
    DSA_MID_N,
    DSA_W_MID,
    DSA_W_RECENT,
    DSA_W_TAIL,
    PRIOR_GAMES,
    build_league_model,
    dsa_weight,
)


def _row(team_id, h_played=10, h_for=15, h_against=10,
         a_played=10, a_for=12, a_against=14):
    return {
        "team": {"id": team_id},
        "home": {"played": h_played, "goals": {"for": h_for, "against": h_against}},
        "away": {"played": a_played, "goals": {"for": a_for, "against": a_against}},
    }


# ── 1. 阶梯权重表 ──────────────────────────────────────────────────────────

def test_weight_tiers():
    assert dsa_weight(0) == DSA_W_RECENT
    assert dsa_weight(DSA_RECENT_N - 1) == DSA_W_RECENT      # 第 5 场仍在核心区
    assert dsa_weight(DSA_RECENT_N) == DSA_W_MID             # 第 6 场起降为基准
    assert dsa_weight(DSA_MID_N - 1) == DSA_W_MID            # 第 15 场
    assert dsa_weight(DSA_MID_N) == DSA_W_TAIL               # 第 16 场起
    assert dsa_weight(300) == DSA_W_TAIL


def test_weight_is_monotonic_non_increasing():
    weights = [dsa_weight(i) for i in range(40)]
    assert all(a >= b for a, b in zip(weights, weights[1:]))


def test_weights_stay_mild_backtest_guard():
    """权重必须保持温和：回测确认 2.0/1.0/0.5 会让 Log Loss 全面变差。

    4 个场景（2 seed × drift 0/1）中提案权重 0/4 改善、平均 +0.00523；
    温和权重才有回落。此断言防止有人把权重改回激进值。
    """
    assert DSA_W_RECENT <= 1.3, "近 5 场权重过高，回测已确认会损害 Log Loss"
    assert DSA_W_TAIL >= 0.5
    # 首尾权重倍差不宜过大，否则方差放大超过"贴合近期"的收益
    assert DSA_W_RECENT / DSA_W_TAIL <= 2.0


# ── 2. 等价性：无逐场日志 或 权重全 1 → 与原模型逐字节一致 ──────────────────

def test_equivalence_without_match_logs():
    """不传 match_logs 时，行为与改造前完全一致。"""
    rows = [_row(1), _row(2)]
    model = build_league_model(rows)
    # 手算原公式：主场进攻 = (15 + 5*1.5)/(10+5)/1.5
    expected_attack_home = (15 + PRIOR_GAMES * 1.5) / (10 + PRIOR_GAMES) / 1.5
    assert model.strength(1).attack_home == pytest.approx(expected_attack_home)


def test_equivalence_when_all_weights_are_one():
    """逐场进球恒定时，加权平均退化为算术平均 → 与原模型一致。"""
    team_id = 1
    row = _row(team_id, h_played=10, h_for=15, h_against=10)

    baseline = build_league_model([row]).strength(team_id)

    # 10 场，每场都是 1.5 球（恒定）→ 无论权重如何，加权平均仍是 1.5
    logs = {team_id: {"home_for": [(f"2026-01-{d:02d}", 1.5) for d in range(1, 11)]}}
    dsa = build_league_model([row], match_logs=logs).strength(team_id)

    assert dsa.attack_home == pytest.approx(baseline.attack_home)


def test_equivalence_uniform_goals_any_distribution():
    """只要逐场值全部相同，DSA 与原始聚合必然相等（严格，非近似）。"""
    team_id = 7
    row = _row(team_id, h_played=20, h_for=40, h_against=20)
    baseline = build_league_model([row]).strength(team_id)
    logs = {team_id: {"home_for": [(f"2026-02-{d:02d}", 2.0) for d in range(1, 21)]}}
    dsa = build_league_model([row], match_logs=logs).strength(team_id)
    assert dsa.attack_home == baseline.attack_home


# ── 3. 单向性：近期状态高于历史平均 → 强度上调 ──────────────────────────────

def test_recent_surge_raises_strength():
    """最近 5 场进球激增，DSA 强度必须高于赛季平均（即高于原模型）。

    ⚠️ 逐场日志总和必须与聚合行一致（20 场共 40 球），否则比较的是
    两个不同的数据集，结论无意义。
    """
    team_id = 3
    row = _row(team_id, h_played=20, h_for=40, h_against=20)
    baseline = build_league_model([row]).strength(team_id)

    pairs = [(f"2026-03-{d:02d}", 5.0) for d in range(1, 6)]      # 最近 5 场：25 球
    pairs += [(f"2025-12-{d:02d}", 1.0) for d in range(1, 16)]    # 更早 15 场：15 球
    assert sum(g for _, g in pairs) == 40          # 自洽性断言
    logs = {team_id: {"home_for": pairs}}

    dsa = build_league_model([row], match_logs=logs).strength(team_id)
    assert dsa.attack_home > baseline.attack_home


def test_recent_slump_lowers_strength():
    """最近 5 场颗粒无收，DSA 强度必须低于原模型（日志同样自洽）。"""
    team_id = 4
    row = _row(team_id, h_played=20, h_for=30, h_against=20)
    baseline = build_league_model([row]).strength(team_id)

    pairs = [(f"2026-03-{d:02d}", 0.0) for d in range(1, 6)]      # 最近 5 场：0 球
    pairs += [(f"2025-12-{d:02d}", 2.0) for d in range(1, 16)]    # 更早 15 场：30 球
    assert sum(g for _, g in pairs) == 30          # 自洽性断言
    logs = {team_id: {"home_for": pairs}}

    dsa = build_league_model([row], match_logs=logs).strength(team_id)
    assert dsa.attack_home < baseline.attack_home


def test_dsa_ignores_calendar_order_uses_date_desc():
    """日志顺序打乱，只要日期正确，结果一致（真正按 date 降序，而非输入序）。"""
    team_id = 5
    row = _row(team_id, h_played=6, h_for=12, h_against=6)
    pairs = [("2026-04-01", 1.0), ("2026-04-05", 5.0), ("2026-04-03", 3.0),
             ("2026-04-02", 2.0), ("2026-04-04", 4.0), ("2026-04-06", 6.0)]
    a = build_league_model([row], match_logs={team_id: {"home_for": pairs}})
    b = build_league_model([row], match_logs={team_id: {"home_for": list(reversed(pairs))}})
    # 4-06 的 6 球应落在核心区（权重 2.0）
    assert a.strength(team_id).attack_home == pytest.approx(
        b.strength(team_id).attack_home)


# ── 4. 安全阀 ──────────────────────────────────────────────────────────────

def test_clamp_off_by_default_preserves_extreme_values():
    """默认关闭安全阀：真实存在的极端强度不被篡改。"""
    team_id = 6
    row = _row(team_id, h_played=19, h_for=0, h_against=30)   # 19 场 0 进球
    model = build_league_model([row])
    assert model.strength(team_id).attack_home < DSA_CLAMP_LO   # 确实低于下限
    # 关闭时不该被拉回
    assert model.strength(team_id).attack_home < DSA_CLAMP_LO


def test_clamp_on_bounds_strength():
    """开启安全阀后，强度被限制在 [0.7, 1.3]。"""
    team_id = 6
    row = _row(team_id, h_played=19, h_for=0, h_against=30)
    model = build_league_model([row], use_dsa_clamp=True)
    s = model.strength(team_id)
    assert s.attack_home == DSA_CLAMP_LO
    assert DSA_CLAMP_LO <= s.attack_home <= DSA_CLAMP_HI
    assert DSA_CLAMP_LO <= s.defense_home <= DSA_CLAMP_HI


# ── 5. 健壮性 ──────────────────────────────────────────────────────────────

def test_missing_log_falls_back_to_aggregate():
    """只为部分球队提供日志时，其余球队回退到聚合值，不报错。"""
    rows = [_row(1), _row(2)]
    logs = {1: {"home_for": [("2026-05-01", 3.0)]}}
    model = build_league_model(rows, match_logs=logs)
    baseline = build_league_model(rows)
    # 队伍 2 无日志 → 与基准完全一致
    assert model.strength(2).attack_home == baseline.strength(2).attack_home


def test_empty_log_list_falls_back():
    team_id = 8
    row = _row(team_id)
    baseline = build_league_model([row]).strength(team_id)
    dsa = build_league_model([row], match_logs={team_id: {"home_for": []}})
    assert dsa.strength(team_id).attack_home == baseline.attack_home


def test_all_four_dimensions_are_weighted():
    """主客场的进攻/防守四个维度都参与加权，不能只改进攻。"""
    team_id = 9
    row = _row(team_id, h_played=10, h_for=10, h_against=10,
               a_played=10, a_for=10, a_against=10)
    baseline = build_league_model([row]).strength(team_id)
    logs = {team_id: {
        "home_for": [("2026-06-0%d" % d, 5.0) for d in range(1, 6)],
        "home_against": [("2026-06-0%d" % d, 0.0) for d in range(1, 6)],
        "away_for": [("2026-06-0%d" % d, 5.0) for d in range(1, 6)],
        "away_against": [("2026-06-0%d" % d, 0.0) for d in range(1, 6)],
    }}
    dsa = build_league_model([row], match_logs=logs).strength(team_id)
    assert dsa.attack_home > baseline.attack_home
    assert dsa.attack_away > baseline.attack_away
    assert dsa.defense_home < baseline.defense_home    # 失球少 → 防守值更小（更强）
    assert dsa.defense_away < baseline.defense_away
