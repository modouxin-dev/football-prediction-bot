"""P1-x：/stats 校准仪表 — Brier / LogLoss / 单调性 / 样本守门。

背景（为什么要补这些用例）：此前同类改动只验证「指标能被计算和分组」，
没有验证「算出来是对的」——把 Brier 公式改坏（分母用错、单项乘错）后
全部用例依然全绿。本文件用可手算的固定向量直接断言数值，堵住这个盲区。
"""
import math
import os
import tempfile

import pytest

from repository import (
    BRIER_RANDOM,
    PredictionRepository,
    brier_logloss,
)

# 已知可手算的向量
PERFECT = {"主胜": 1.0, "平局": 0.0, "客胜": 0.0}
UNIFORM = {"主胜": 1 / 3, "平局": 1 / 3, "客胜": 1 / 3}


# ── brier_logloss 纯函数：数值断言 ──────────────────────────────

def test_perfect_prediction_is_zero():
    b, ll = brier_logloss(PERFECT, "主胜")
    assert b == pytest.approx(0.0)
    # 概率为 1 时 logloss 也应趋近 0（不是 floor 的 13.8）
    assert ll == pytest.approx(0.0)


def test_wrong_certainty_is_max_brier():
    """全押主胜、实际客胜 → 三项各贡献 (1-0)², (0-0)², (0-1)² = 2.0"""
    b, _ = brier_logloss(PERFECT, "客胜")
    assert b == pytest.approx(2.0)


def test_uniform_prediction_matches_random_baseline():
    """均匀 1/3 → (1/3-1)² + (1/3)² + (1/3)² = 6/9 = 2/3，即三分类随机基准。"""
    b, ll = brier_logloss(UNIFORM, "主胜")
    assert b == pytest.approx(2.0 / 3.0)
    assert b == pytest.approx(BRIER_RANDOM)
    assert ll == pytest.approx(math.log(3.0))


def test_brier_formula_uses_squared_error():
    """0.8/0.1/0.1 且实际主胜 → (0.8-1)² + 0.1² + 0.1² = 0.06"""
    b, ll = brier_logloss({"主胜": 0.8, "平局": 0.1, "客胜": 0.1}, "主胜")
    assert b == pytest.approx(0.06)
    assert ll == pytest.approx(-math.log(0.8))


def test_logloss_punishes_confident_miss():
    """越自信且猜错，LogLoss 越大；这一条同时钉住「取的是实际结果那一维」。"""
    _, ll_wrong = brier_logloss({"主胜": 0.9, "平局": 0.05, "客胜": 0.05}, "客胜")
    _, ll_mild = brier_logloss({"主胜": 0.4, "平局": 0.3, "客胜": 0.3}, "客胜")
    assert ll_wrong > ll_mild


def test_unnormalized_probs_are_normalized():
    """(2,1,1) 归一后为 (0.5,0.25,0.25)，实际主胜 → 0.25+0.0625+0.0625"""
    b, _ = brier_logloss({"主胜": 2.0, "平局": 1.0, "客胜": 1.0}, "主胜")
    assert b == pytest.approx(0.375)


@pytest.mark.parametrize("probs", [
    {"主胜": None, "平局": 0.3, "客胜": 0.3},
    {"主胜": float("nan"), "平局": 0.3, "客胜": 0.3},
    {"主胜": -0.1, "平局": 0.3, "客胜": 0.3},
    {"主胜": 0.0, "平局": 0.0, "客胜": 0.0},
    {},
])
def test_invalid_probs_return_none(probs):
    assert brier_logloss(probs, "主胜") is None


def test_unknown_actual_returns_none():
    assert brier_logloss(PERFECT, "让球胜") is None


# ── repository.stats 集成 ───────────────────────────────────────

def _repo_with(rows):
    """rows: (fixture_id, level_key, result, home_prob, draw_prob, away_prob,
              actual_home, actual_away)"""
    td = tempfile.mkdtemp()
    repo = PredictionRepository(os.path.join(td, "t.db"))
    conn = repo._connect()
    for (fid, lv, res, hp, dp, ap, ah, aa) in rows:
        conn.execute(
            "INSERT INTO predictions (fixture_id, season, league, home, away, kickoff, "
            "model_version, source, level_key, result, home_prob, draw_prob, away_prob, "
            "best_score, created_at, inputs, actual_home, actual_away) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(fid), 2026, "PL", "A", "B", "2026-08-01T12:00:00+00:00",
             "poisson-v0.1", "API-Football", lv, res, hp, dp, ap,
             "1-1", "2026-08-01T00:00:00+00:00", "{}", ah, aa),
        )
    conn.commit()
    return repo


def test_stats_returns_calibration_fields():
    repo = _repo_with([
        (1, "high", "主胜", 0.8, 0.1, 0.1, 2, 1),   # 中
        (2, "high", "主胜", 0.6, 0.2, 0.2, 0, 1),   # 未中
    ])
    st = repo.stats()
    assert st["total"] == 2 and st["hit"] == 1
    assert st["scored"] == 2
    # 手算：实际主胜 (0.8-1)²+0.1²+0.1²=0.06
    #       实际客胜 (0.6-0)²+0.2²+(0.2-1)²=0.36+0.04+0.64=1.04 → 均值 0.55
    assert st["brier"] == pytest.approx((0.06 + 1.04) / 2)
    assert st["logloss"] == pytest.approx((-math.log(0.8) + -math.log(0.2)) / 2)


def test_stats_skips_missing_probability():
    """概率缺失的场次只计入命中率，不计入校准指标——不能拿 None 当 0 算。

    第一条 Brier 特意取非零值（0.06）：若分母误用 total(=2) 而非 scored(=1)，
    结果会变成 0.03 而本用例仍会通过。非零值才能钉住分母。
    """
    repo = _repo_with([
        (1, "high", "主胜", 0.8, 0.1, 0.1, 2, 1),   # Brier 0.06
        (2, "high", "主胜", None, None, None, 3, 0),  # 无概率，跳过
    ])
    st = repo.stats()
    assert st["total"] == 2 and st["hit"] == 2
    assert st["scored"] == 1
    assert st["brier"] == pytest.approx(0.06)


def test_stats_per_level_brier():
    repo = _repo_with([
        (1, "high", "主胜", 1.0, 0.0, 0.0, 2, 1),
        (2, "low", "主胜", 1 / 3, 1 / 3, 1 / 3, 2, 1),
    ])
    st = repo.stats()
    assert st["by_level"]["high"]["brier"] == pytest.approx(0.0)
    assert st["by_level"]["low"]["brier"] == pytest.approx(BRIER_RANDOM)
    # 中间累加器不该外泄
    assert "brier_sum" not in st["by_level"]["high"]


def test_stats_empty_db_still_has_keys():
    repo = _repo_with([])
    st = repo.stats()
    assert st["brier"] is None and st["logloss"] is None and st["scored"] == 0


# ── 展示层：单调性判定与样本守门 ────────────────────────────────

from commands.admin import CALIBRATION_MIN_SAMPLE, _calibration_section  # noqa: E402

LEVEL_NAMES = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低", "unknown": "⚪ 未知"}


def _flat(blocks):
    return "\n".join(line for block in blocks for line in block)


def _mk(by_level, total):
    return {"by_level": by_level, "total": total,
            "brier": 0.2, "logloss": 0.98, "scored": total}


def test_monotonic_verdict_pass():
    st = _mk({
        "high": {"total": 40, "hit": 30, "brier": 0.15, "logloss": 0.8},
        "medium": {"total": 40, "hit": 20, "brier": 0.20, "logloss": 0.9},
        "low": {"total": 40, "hit": 14, "brier": 0.24, "logloss": 1.1},
    }, 120)
    text = _flat(_calibration_section(st, LEVEL_NAMES))
    assert "单调成立" in text


def test_monotonic_verdict_fails_when_inverted():
    st = _mk({
        "high": {"total": 40, "hit": 14, "brier": 0.24, "logloss": 1.1},
        "low": {"total": 40, "hit": 30, "brier": 0.15, "logloss": 0.8},
    }, 120)
    text = _flat(_calibration_section(st, LEVEL_NAMES))
    assert "单调不成立" in text


def test_sample_gate_hides_conclusion():
    """样本不足时只给数字，不作任何判定。"""
    st = _mk({"high": {"total": 20, "hit": 15, "brier": 0.15, "logloss": 0.8}},
             CALIBRATION_MIN_SAMPLE - 1)
    text = _flat(_calibration_section(st, LEVEL_NAMES))
    assert "不作结论" in text
    assert "单调成立" not in text and "单调不成立" not in text


def test_no_level_data_returns_nothing():
    assert _calibration_section({"by_level": {}, "total": 0}, LEVEL_NAMES) == []


def test_overall_shows_random_baseline_for_context():
    st = _mk({"high": {"total": 40, "hit": 20, "brier": 0.2, "logloss": 0.9}}, 150)
    text = _flat(_calibration_section(st, LEVEL_NAMES))
    assert "随机" in text
