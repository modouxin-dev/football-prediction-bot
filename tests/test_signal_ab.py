"""赛前信号对照：数据层测试 / Signal A/B ledger — data-layer tests.

覆盖用户评审里的数据库层要求：粒度、原始强度、幂等迁移、UPSERT、
信号撤回、部分失败、测试隔离、索引。
"""
from __future__ import annotations

import sqlite3

import pytest

from repository import PredictionRepository
from signal_ab import (CHANGE_APPLIED, CHANGE_REVOKED, SIGNAL_INJURY,
                       SIGNAL_LINEUP, STATUS_OK, STATUS_PARTIAL_FAILURE,
                       describe, hit, log_loss, normalise_probs, outcome_index,
                       rps)


@pytest.fixture()
def repo(tmp_path):
    return PredictionRepository(str(tmp_path / "ab.db"))


# ---- 粒度与幂等 ------------------------------------------------------------


def test_one_row_per_fixture_and_signal_type(repo):
    """一行 = 一场比赛 × 一种信号；同类型重复写入是 UPDATE 不是 INSERT。"""
    for _ in range(3):
        assert repo.save_signal_ab(
            fixture_id=1, signal_type=SIGNAL_INJURY,
            pure_baseline=[0.5, 0.3, 0.2], signal_prediction=[0.45, 0.3, 0.25])
    assert len(repo.signal_ab_rows()) == 1


def test_different_signal_types_are_separate_rows(repo):
    """同一场的伤停与阵容必须分开存，否则无法拆开评估每种信号的价值。"""
    repo.save_signal_ab(fixture_id=1, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.45, 0.3, 0.25])
    repo.save_signal_ab(fixture_id=1, signal_type=SIGNAL_LINEUP,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    rows = repo.signal_ab_rows()
    assert len(rows) == 2
    assert {r["signal_type"] for r in rows} == {SIGNAL_INJURY, SIGNAL_LINEUP}


def test_upsert_keeps_latest_strength(repo):
    """重新预测时更新同一行：强度以最新版为准，不追加历史。"""
    repo.save_signal_ab(fixture_id=7, signal_type=SIGNAL_INJURY,
                        signal_strength=1.0, pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    repo.save_signal_ab(fixture_id=7, signal_type=SIGNAL_INJURY,
                        signal_strength=4.0, pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.4, 0.3, 0.3])
    rows = repo.signal_ab_rows()
    assert len(rows) == 1
    assert rows[0]["signal_strength"] == 4.0


def test_unique_index_exists(repo):
    """唯一索引是 UPSERT 生效的前提，缺了它就会静默产生重复行。"""
    conn = repo._connect()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ux_signal_ab" in names
    assert "idx_signal_ab_processed" in names


# ---- 字段语义 --------------------------------------------------------------


def test_raw_strength_is_stored_not_normalised(repo):
    """存原始值：归一化逻辑会变，原始值永远能重新算。"""
    repo.save_signal_ab(fixture_id=2, signal_type=SIGNAL_INJURY,
                        signal_strength=3.75, signal_strength_norm=0.62,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    row = repo.signal_ab_rows()[0]
    assert row["signal_strength"] == 3.75
    assert row["signal_strength_norm"] == 0.62


def test_revoked_signal_is_recorded(repo):
    """信号消失（伤停复出）本身是一次变化，必须单独标记。"""
    repo.save_signal_ab(fixture_id=3, signal_type=SIGNAL_INJURY,
                        signal_strength=2.0, pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.45, 0.3, 0.25])
    repo.save_signal_ab(fixture_id=3, signal_type=SIGNAL_INJURY,
                        signal_strength=0.0, change_type=CHANGE_REVOKED,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    row = repo.signal_ab_rows()[0]
    assert row["change_type"] == CHANGE_REVOKED
    assert row["signal_strength"] == 0.0


def test_partial_failure_keeps_baseline(repo):
    """signal 版算不出来时仍要留下 baseline —— 否则无法区分
    「信号没用」和「信号根本没算出来」。"""
    ok = repo.save_signal_ab(
        fixture_id=4, signal_type=SIGNAL_INJURY,
        pure_baseline=[0.5, 0.3, 0.2], signal_prediction=None,
        error="信号强度计算异常")
    assert ok is True
    row = repo.signal_ab_rows()[0]
    assert row["status"] == STATUS_PARTIAL_FAILURE
    assert row["pure_baseline"] is not None
    assert row["signal_prediction"] is None
    assert "异常" in (row["error"] or "")


def test_invalid_probs_are_not_written(repo):
    """非法概率写 NULL，绝不写入半截数据。"""
    repo.save_signal_ab(fixture_id=5, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[float("nan"), 0.3, 0.3])
    row = repo.signal_ab_rows()[0]
    assert row["signal_prediction"] is None


def test_ok_status_when_no_error(repo):
    repo.save_signal_ab(fixture_id=6, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    assert repo.signal_ab_rows()[0]["status"] == STATUS_OK


def test_human_note_persisted(repo):
    repo.save_signal_ab(fixture_id=8, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2],
                        human_note="伤停信号：有效缺阵负荷 2.00 人")
    assert "缺阵负荷" in repo.signal_ab_rows()[0]["human_note"]


# ---- 迁移幂等 --------------------------------------------------------------


def test_column_migration_is_idempotent(repo):
    """补列跑两次不能报错，也不能重复补。"""
    conn = repo._connect()
    conn.execute("DROP TABLE IF EXISTS schema_migrations")
    conn.commit()
    repo._ensure_signal_ab_columns()
    cols_1 = {r["name"] for r in conn.execute("PRAGMA table_info(signal_ab)")}
    repo._ensure_signal_ab_columns()
    cols_2 = {r["name"] for r in conn.execute("PRAGMA table_info(signal_ab)")}
    assert cols_1 == cols_2
    assert "is_archived" in cols_2


def test_migration_marker_prevents_rescan(repo):
    """标记存在时直接跳过，连数据库都不再访问。

    验证手法：让 _connect 抛异常。若方法真的走了跳过分支，就碰不到
    数据库、不会抛；若它重新扫表，测试立刻失败。
    """
    assert repo._migration_done("signal_ab_extra_columns_v1") is True
    repo._migration_done = lambda _name: True          # type: ignore[assignment]
    original = repo._connect
    repo._connect = lambda: (_ for _ in ()).throw(     # type: ignore[assignment]
        AssertionError("标记存在时不应访问数据库"))
    try:
        repo._ensure_signal_ab_columns()   # 不抛即通过
    finally:
        repo._connect = original           # type: ignore[assignment]


def test_migration_reruns_when_marker_absent(repo):
    """标记被删掉后必须真的重跑一遍（幂等不是"永不执行"）。"""
    conn = repo._connect()
    conn.execute("DELETE FROM schema_migrations WHERE name=?",
                 ("signal_ab_extra_columns_v1",))
    conn.commit()
    assert repo._migration_done("signal_ab_extra_columns_v1") is False
    repo._ensure_signal_ab_columns()
    assert repo._migration_done("signal_ab_extra_columns_v1") is True


def test_registry_seeded(repo):
    conn = repo._connect()
    rows = list(conn.execute("SELECT signal_type FROM signal_registry"))
    assert {r["signal_type"] for r in rows} >= {SIGNAL_INJURY, SIGNAL_LINEUP}


# ---- 隔离与回填 ------------------------------------------------------------


def test_test_rows_are_filtered_by_default(repo):
    repo.save_signal_ab(fixture_id=9, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2], is_test=True)
    assert repo.signal_ab_rows() == []
    assert len(repo.signal_ab_rows(include_test=True)) == 1


def test_archived_rows_hidden_by_default(repo):
    repo.save_signal_ab(fixture_id=10, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    conn = repo._connect()
    conn.execute("UPDATE signal_ab SET is_archived=1 WHERE fixture_id='10'")
    conn.commit()
    assert repo.signal_ab_rows() == []
    assert len(repo.signal_ab_rows(include_archived=True)) == 1


def test_settle_only_updates_unsettled(repo):
    repo.save_signal_ab(fixture_id=11, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    assert repo.settle_signal_ab([(11, 2, 1)]) == 1
    # 第二次回填不应覆盖已有比分
    assert repo.settle_signal_ab([(11, 5, 5)]) == 0
    row = repo.signal_ab_rows(settled_only=True)[0]
    assert (row["actual_home"], row["actual_away"]) == (2, 1)
    assert row["is_processed"] == 1


def test_unsettled_query(repo):
    repo.save_signal_ab(fixture_id=12, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])
    assert len(repo.unsettled_signal_ab()) == 1
    repo.settle_signal_ab([(12, 1, 1)])
    assert len(repo.unsettled_signal_ab()) == 0


def test_for_fixture_returns_all_signals(repo):
    for t in (SIGNAL_INJURY, SIGNAL_LINEUP):
        repo.save_signal_ab(fixture_id=13, signal_type=t,
                            pure_baseline=[0.5, 0.3, 0.2],
                            signal_prediction=[0.5, 0.3, 0.2])
    assert len(repo.signal_ab_for_fixture(13)) == 2


def test_write_failure_returns_false_without_raising(repo):
    """写入失败必须被吞掉：对照挂了不该让预测发不出去。"""
    repo.save_signal_ab(fixture_id=14, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2])

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    original = repo._connect
    repo._connect = boom              # type: ignore[assignment]
    ok = repo.save_signal_ab(fixture_id=15, signal_type=SIGNAL_INJURY,
                             pure_baseline=[0.5, 0.3, 0.2],
                             signal_prediction=[0.5, 0.3, 0.2])
    repo._connect = original          # type: ignore[assignment]
    assert ok is False


def test_config_snapshot_stored(repo):
    repo.save_signal_ab(fixture_id=16, signal_type=SIGNAL_INJURY,
                        pure_baseline=[0.5, 0.3, 0.2],
                        signal_prediction=[0.5, 0.3, 0.2],
                        config_snapshot={"strength": 0.5, "tilt": 0.0})
    assert "strength" in (repo.signal_ab_rows()[0]["config_snapshot"] or "")


# ---- 权重审计 --------------------------------------------------------------


def test_weight_log_is_append_only(repo):
    repo.log_signal_weight(signal_type=SIGNAL_INJURY, old_weight=0.5,
                           new_weight=0.6, applied=False, reason="建议")
    repo.log_signal_weight(signal_type=SIGNAL_INJURY, old_weight=0.6,
                           new_weight=0.7, applied=True, reason="采纳")
    hist = repo.signal_weight_history(SIGNAL_INJURY)
    assert len(hist) == 2                      # 追加，不覆盖
    assert hist[0]["new_weight"] == 0.7        # 最新在前
    assert hist[1]["old_weight"] == 0.5        # 旧值永久保留
    assert hist[0]["applied"] == 1
    assert hist[1]["applied"] == 0


# ---- 纯函数 ----------------------------------------------------------------


def test_normalise_probs_accepts_dict_and_tuple():
    assert normalise_probs({"home": 2, "draw": 1, "away": 1}) == [0.5, 0.25, 0.25]
    assert normalise_probs([2, 1, 1]) == [0.5, 0.25, 0.25]


def test_normalise_probs_rejects_junk():
    assert normalise_probs(None) is None
    assert normalise_probs([0, 0, 0]) is None
    assert normalise_probs([float("nan"), 1, 1]) is None
    assert normalise_probs([-1, 1, 1]) is None


def test_outcome_index():
    assert outcome_index(2, 1) == 0
    assert outcome_index(1, 1) == 1
    assert outcome_index(0, 3) == 2
    assert outcome_index(None, 1) is None


def test_log_loss_and_rps_prefer_correct_direction():
    good, bad = [0.6, 0.25, 0.15], [0.15, 0.25, 0.6]
    assert log_loss(good, 0) < log_loss(bad, 0)
    assert rps(good, 0) < rps(bad, 0)


def test_rps_punishes_near_miss_less_than_reversal():
    """RPS 承认顺序：猜平（相邻）比猜反（主胜↔客胜）罚得更轻。"""
    draw_guess, away_guess = [0.15, 0.6, 0.25], [0.15, 0.25, 0.6]
    assert rps(draw_guess, 0) < rps(away_guess, 0)


def test_metrics_return_none_when_incomplete():
    assert log_loss(None, 0) is None
    assert log_loss([0.5, 0.3, 0.2], None) is None
    assert log_loss([0.0, 0.5, 0.5], 0) is None   # 概率为 0 时不做 -inf 猜测


def test_hit():
    assert hit([0.5, 0.3, 0.2], 0) == 1
    assert hit([0.5, 0.3, 0.2], 2) == 0
    assert hit(None, 0) is None


def test_describe_mentions_key_number():
    assert "缺阵负荷" in describe(SIGNAL_INJURY, {"absence_load": 2.0})
    assert "信号" in describe("unknown_type", {})


def test_change_types_are_valid():
    assert CHANGE_APPLIED == "applied"
    assert CHANGE_REVOKED == "revoked"
