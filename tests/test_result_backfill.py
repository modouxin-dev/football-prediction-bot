"""result 字段自愈 / Prediction direction backfill.

命中判定是 `result == 实际赛果`（analytics.model_health / repository.stats）。
历史库里 result 可能是 'pending' 或 NULL——那不是合法方向，永远不等于任何赛果，
于是这部分样本被悄悄判为「未命中」，命中率被压低而没有任何报错。

本文件锁住两件事：
1. 非法 result 能被按概率确定性恢复（概率没丢，方向可还原）
2. 恢复是幂等的，且不碰 actual_* / settled_at / 概率本身
"""
import sqlite3

import pytest

import analytics
from repository import PredictionRepository, VALID_RESULTS

# 与 repository._outcome 同一套口径
HOME, DRAW, AWAY = "主胜", "平局", "客胜"


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "b.db")


def _insert(db, fid, *, result, hp, dp, ap,
            actual_home=None, actual_away=None, settled=True):
    """插一条预测。默认已结算（有比分 + 结算时间），贴近真实历史行。"""
    repo = PredictionRepository(db)
    conn = repo._connect()
    conn.execute(
        "INSERT OR REPLACE INTO predictions (fixture_id,season,league,home,away,kickoff,"
        "model_version,source,level_key,result,home_prob,draw_prob,away_prob,best_score,"
        "created_at,inputs,actual_home,actual_away,settled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, 2026, "PL", "H", "A", "2026-02-01T15:00", "v1", "s", "high",
         result, hp, dp, ap, "1-0", "2026-02-01", "{}",
         actual_home, actual_away, "2026-02-02" if settled else None),
    )
    conn.commit()
    return repo


def _result_of(db, fid):
    conn = PredictionRepository(db)._connect()
    row = conn.execute(
        "SELECT result FROM predictions WHERE fixture_id=?", (fid,)).fetchone()
    return row["result"] if row else None


# ---- 批量回填 ---------------------------------------------------------------

def test_pending_is_restored_by_probability(db):
    """'pending' 按最大概率方向恢复：主胜概率最高 → 主胜。"""
    _insert(db, "f1", result="pending", hp=0.6, dp=0.2, ap=0.2,
            actual_home=2, actual_away=1)
    repo = PredictionRepository(db)
    assert repo.backfill_results() == 1
    assert _result_of(db, "f1") == HOME


def test_null_result_is_restored(db):
    """NULL 同样非法，必须恢复。"""
    _insert(db, "f2", result=None, hp=0.2, dp=0.2, ap=0.6,
            actual_home=0, actual_away=1)
    repo = PredictionRepository(db)
    assert repo.backfill_results() == 1
    assert _result_of(db, "f2") == AWAY


def test_draw_direction_is_restored(db):
    """平局概率最高时回填平局——三个方向都要覆盖，不能只测主客。"""
    _insert(db, "f3", result="pending", hp=0.2, dp=0.6, ap=0.2,
            actual_home=1, actual_away=1)
    PredictionRepository(db).backfill_results()
    assert _result_of(db, "f3") == DRAW


def test_backfill_is_idempotent(db):
    """重复调用零改动：正常库每次启动都跑一次，不能动到合法数据。"""
    _insert(db, "f4", result="pending", hp=0.6, dp=0.2, ap=0.2,
            actual_home=2, actual_away=1)
    repo = PredictionRepository(db)
    assert repo.backfill_results() == 1
    assert repo.backfill_results() == 0          # 第二次什么都不该改
    assert _result_of(db, "f4") == HOME          # 值保持不变


def test_valid_result_untouched(db):
    """已是合法方向的记录不能被改写，哪怕它和概率最大值不一致。"""
    _insert(db, "f5", result=AWAY, hp=0.6, dp=0.2, ap=0.2,
            actual_home=2, actual_away=1)
    repo = PredictionRepository(db)
    assert repo.backfill_results() == 0
    assert _result_of(db, "f5") == AWAY


def test_zero_probability_rows_are_left_alone(db):
    """概率全 0 的异常行不动：无法推断方向，宁可留着也不能凭空造一个。"""
    _insert(db, "f6", result="pending", hp=0.0, dp=0.0, ap=0.0,
            actual_home=2, actual_away=1)
    repo = PredictionRepository(db)
    assert repo.backfill_results() == 0
    assert _result_of(db, "f6") == "pending"


def test_backfill_preserves_scores_and_settlement(db):
    """只改 result，不得碰 actual_* / settled_at / 概率——否则命中判定口径就变了。"""
    _insert(db, "f7", result="pending", hp=0.6, dp=0.2, ap=0.2,
            actual_home=3, actual_away=0)
    PredictionRepository(db).backfill_results()
    conn = PredictionRepository(db)._connect()
    row = conn.execute(
        "SELECT result, actual_home, actual_away, settled_at, home_prob, draw_prob, away_prob "
        "FROM predictions WHERE fixture_id='f7'").fetchone()
    assert row["result"] == HOME
    assert (row["actual_home"], row["actual_away"]) == (3, 0)
    assert row["settled_at"] == "2026-02-02"
    assert (row["home_prob"], row["draw_prob"], row["away_prob"]) == (0.6, 0.2, 0.2)


# ---- settle 路径 ------------------------------------------------------------

def test_settle_repairs_illegal_result(db):
    """结算是最后一道关口：写比分时顺手把非法方向修掉，不让脏数据留到统计里。"""
    _insert(db, "f8", result="pending", hp=0.2, dp=0.2, ap=0.6,
            actual_home=None, actual_away=None, settled=False)
    repo = PredictionRepository(db)
    assert repo.settle("f8", 0, 2) is True
    assert _result_of(db, "f8") == AWAY


def test_settle_keeps_valid_result(db):
    """结算不改写合法方向——result 是预测方向，不是命中与否，不能被结算覆盖。"""
    _insert(db, "f9", result=HOME, hp=0.6, dp=0.2, ap=0.2,
            actual_home=None, actual_away=None, settled=False)
    PredictionRepository(db).settle("f9", 1, 1)
    assert _result_of(db, "f9") == HOME


# ---- 端到端：命中率真的被修正 -----------------------------------------------

def test_accuracy_is_restored_after_backfill(db):
    """最关键的验收：回填前命中率被压成 0，回填后恢复为真值。

    这条直接对应用户可见的看板数字，前面的单条断言都只是手段。
    """
    # 4 场全部预测正确（实际赛果都与最高概率方向一致）
    for i, (hp, dp, ap, hs, as_) in enumerate([
        (0.6, 0.2, 0.2, 2, 1),   # 主胜 → 2-1
        (0.2, 0.2, 0.6, 0, 1),   # 客胜 → 0-1
        (0.2, 0.6, 0.2, 1, 1),   # 平局 → 1-1
        (0.5, 0.3, 0.2, 3, 0),   # 主胜 → 3-0
    ]):
        _insert(db, f"g{i}", result="pending", hp=hp, dp=dp, ap=ap,
                actual_home=hs, actual_away=as_)

    before = analytics.model_health(db)
    assert before["settled"] == 4
    assert before["hit"] == 0, "非法 result 不该命中——这正是要修的现象"
    assert before["accuracy"] == 0.0

    PredictionRepository(db).backfill_results()

    after = analytics.model_health(db)
    assert after["hit"] == 4
    assert after["accuracy"] == 1.0


def test_wrong_prediction_still_counts_as_miss(db):
    """回填不是「一律判中」：方向错了照样算未命中，准确率不能虚高。"""
    _insert(db, "h1", result="pending", hp=0.2, dp=0.2, ap=0.6,  # 预测客胜
            actual_home=2, actual_away=1)                          # 实际主胜
    _insert(db, "h2", result="pending", hp=0.6, dp=0.2, ap=0.2,  # 预测主胜
            actual_home=1, actual_away=3)                          # 实际客胜
    PredictionRepository(db).backfill_results()
    health = analytics.model_health(db)
    assert health["hit"] == 0
    assert health["accuracy"] == 0.0


# ---- 常量契约 ---------------------------------------------------------------

def test_valid_results_matches_outcome_labels():
    """VALID_RESULTS 必须和 _outcome() 的输出口径一致，否则命中判定永远不成立。"""
    from repository import _outcome
    produced = {_outcome(1, 0), _outcome(0, 1), _outcome(1, 1)}
    assert produced == set(VALID_RESULTS)
