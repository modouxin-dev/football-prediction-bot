"""信号权重治理与导出 / Weight governance & export.

覆盖用户评审里的安全与可维护性要求：
  * 默认只有读权限，代码 bug 不可能意外改掉权重
  * 应用前必须二次确认；即便只提建议也留痕
  * 权重历史只追加，旧值永久保留
  * 发布级配置快照、对照视图、CSV 导出都可用
"""
from __future__ import annotations

import pytest

from repository import PredictionRepository
from signal_ab import SIGNAL_INJURY
from signal_admin import (apply_weight, export_csv, save_config_snapshot,
                          scale_probs, simulate_text, simulate_weight)


@pytest.fixture()
def repo(tmp_path):
    return PredictionRepository(str(tmp_path / "adm.db"))


def _row(sig_type, strength, base_w, sig_w, home, away):
    def _p(w):
        rest = round(1.0 - w, 4)
        return '{"home": %s, "draw": %s, "away": %s}' % (w, rest / 2, rest / 2)
    return {"signal_type": sig_type, "signal_strength": strength,
            "pure_baseline": _p(base_w), "partial_baseline": _p(base_w),
            "signal_prediction": _p(sig_w),
            "actual_home": home, "actual_away": away}


# ---- 模拟 -----------------------------------------------------------------


def test_scale_zero_means_signal_does_nothing():
    """scale=0 → 回到 base（信号完全不生效）；scale=1 → 等于 signal。"""
    base, sig = [0.40, 0.30, 0.30], [0.50, 0.28, 0.22]
    assert scale_probs(base, sig, 0.0) == pytest.approx(base)
    assert scale_probs(base, sig, 1.0) == pytest.approx(sig)


def test_simulate_reports_direction_not_magnitude():
    """把有效信号放大 → Log Loss 变差（过度修正）。方向必须判对。"""
    rows = [_row(SIGNAL_INJURY, 0.5, 0.40, 0.46, 2, 1) for _ in range(40)]
    sim = simulate_weight(rows, SIGNAL_INJURY, new_weight=3.0,
                          current_weight=1.0, min_samples=10)
    assert sim["sufficient"] is True
    assert sim["scale"] == 3.0
    assert sim["logloss_delta"] is not None


def test_simulate_refuses_when_samples_insufficient():
    """样本不足时明说不足，绝不给"看起来有结论"的数字。"""
    rows = [_row(SIGNAL_INJURY, 0.5, 0.40, 0.46, 2, 1) for _ in range(3)]
    sim = simulate_weight(rows, SIGNAL_INJURY, new_weight=2.0, min_samples=30)
    assert sim["sufficient"] is False
    assert "样本不足" in simulate_text(sim)


def test_simulate_ignores_weak_rows():
    """弱信号场不进模拟 —— 它们本来就不动，混进来只会显得"没变化"。"""
    rows = ([_row(SIGNAL_INJURY, 0.5, 0.40, 0.46, 2, 1) for _ in range(40)]
            + [_row(SIGNAL_INJURY, 0.01, 0.40, 0.40, 0, 3) for _ in range(10)])
    sim = simulate_weight(rows, SIGNAL_INJURY, new_weight=2.0, min_samples=10)
    assert sim["n"] == 40


# ---- 权限与确认 ------------------------------------------------------------


def test_read_only_mode_never_applies(repo):
    """默认只读：确认了也不应用 —— 线上不该有能力改权重。"""
    ok, msg = apply_weight(repo, signal_type=SIGNAL_INJURY, old_weight=1.0,
                           new_weight=1.5, write_enabled=False, confirm=True)
    assert ok is False and "只读" in msg
    hist = repo.signal_weight_history(SIGNAL_INJURY)
    assert len(hist) == 1 and hist[0]["applied"] == 0   # 建议也要留痕


def test_apply_requires_confirmation(repo):
    """给了写权限但没确认 → 不应用。确认是最后一道人为关卡。"""
    ok, msg = apply_weight(repo, signal_type=SIGNAL_INJURY, old_weight=1.0,
                           new_weight=1.5, write_enabled=True, confirm=False)
    assert ok is False and "确认" in msg
    assert repo.signal_weight_history(SIGNAL_INJURY)[0]["applied"] == 0


def test_apply_succeeds_only_with_both(repo):
    ok, msg = apply_weight(repo, signal_type=SIGNAL_INJURY, old_weight=1.0,
                           new_weight=1.5, write_enabled=True, confirm=True)
    assert ok is True and "已应用" in msg
    assert repo.signal_weight_history(SIGNAL_INJURY)[0]["applied"] == 1


def test_weight_history_is_append_only(repo):
    """旧权重必须永久保留，否则回答不了"那段时间差是不是权重不好"。"""
    apply_weight(repo, signal_type=SIGNAL_INJURY, old_weight=1.0,
                 new_weight=1.5, write_enabled=True, confirm=True)
    apply_weight(repo, signal_type=SIGNAL_INJURY, old_weight=1.5,
                 new_weight=0.8, write_enabled=True, confirm=True)
    hist = repo.signal_weight_history(SIGNAL_INJURY)
    assert len(hist) == 2
    assert {h["old_weight"] for h in hist} == {1.0, 1.5}


# ---- 快照、视图、导出 ------------------------------------------------------


def test_config_snapshot_roundtrip(repo):
    """快照存进去还能读出来，且带版本号 —— 半年后靠它回溯。"""
    assert save_config_snapshot(repo, version="abc1234",
                                payload={"prematch_strength": 0.5})
    snaps = repo.config_snapshots()
    assert len(snaps) == 1 and snaps[0]["version"] == "abc1234"
    assert "prematch_strength" in snaps[0]["payload"]


def test_view_expands_probabilities(repo):
    """视图把 JSON 概率展开成数值列，外部分析不必各自解析 JSON。"""
    repo.save_signal_ab(fixture_id=99, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.4, 0.3, 0.3],
                        partial_baseline=[0.4, 0.3, 0.3],
                        signal_prediction=[0.5, 0.28, 0.22],
                        signal_strength=0.5)
    row = repo.signal_ab_view(signal_type=SIGNAL_INJURY)[0]
    assert row["signal_home"] == pytest.approx(0.5)
    assert row["pure_draw"] == pytest.approx(0.3)


def test_export_csv_has_header_and_rows(repo):
    for i in range(3):
        repo.save_signal_ab(fixture_id=200 + i, signal_type=SIGNAL_INJURY,
                            pure_baseline=[0.4, 0.3, 0.3],
                            signal_prediction=[0.45, 0.29, 0.26],
                            signal_strength=0.5)
    text = export_csv(repo.signal_ab_view())
    lines = text.strip().splitlines()
    assert lines[0].startswith("fixture_id")
    assert len(lines) == 4
    assert "injury" in text


def test_export_tolerates_junk_rows():
    """导出遇到无法识别的行要跳过，不能整个崩掉。"""
    assert export_csv([object(), None]).strip().count("\n") == 0
