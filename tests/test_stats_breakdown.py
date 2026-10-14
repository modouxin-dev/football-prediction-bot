"""/stats 细分维度：按预测方向、按联赛。

为什么要这两个维度：
    只有总命中率看不出模型偏在哪。若「主胜预测」命中 58% 而「平局预测」
    只有 30%，总分会被拉到中间值，使用者既不知道该信哪类，也不知道
    该改哪类。按方向拆开后，偏科一眼可见。

    按联赛同理：模型在英超的样本和表现，不能代表它在巴甲的表现。
"""

from __future__ import annotations

import sqlite3

import pytest

import repository
from repository import PredictionRepository


def _seed(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """写入已结算的预测记录。

    只填统计真正读取的列：result 是**预测**方向，actual_* 是**真实**比分。
    """
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO predictions (fixture_id, league, result, home_prob, "
            "draw_prob, away_prob, actual_home, actual_away, kickoff, level_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"fx-{i}",
                r.get("league", "PL"),
                r["result"],
                r.get("home_prob", 0.34),
                r.get("draw_prob", 0.33),
                r.get("away_prob", 0.33),
                r["ah"],
                r["aa"],
                f"2026-10-0{(i % 9) + 1}T12:00:00+00:00",
                "high",
            ),
        )
    conn.commit()


def _repo() -> PredictionRepository:
    repo = PredictionRepository()
    conn = repo._connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS predictions ("
        "fixture_id TEXT PRIMARY KEY, season INTEGER, league TEXT, home TEXT, "
        "away TEXT, kickoff TEXT, model_version TEXT, source TEXT, level_key TEXT, "
        "result TEXT, home_prob REAL, draw_prob REAL, away_prob REAL, "
        "best_score TEXT, created_at TEXT, inputs TEXT, actual_home INTEGER, "
        "actual_away INTEGER, settled_at TEXT)"
    )
    conn.commit()
    return repo


# ---- 契约 1：按预测方向拆分 ----------------------------------------------

def test_by_result_splits_three_directions():
    """三个方向各自统计：命中数 / 总数 / 命中率。"""
    repo = _repo()
    _seed(repo._connect(), [
        # 预测主胜：3 中 2
        {"result": "主胜", "ah": 2, "aa": 1},
        {"result": "主胜", "ah": 1, "aa": 0},
        {"result": "主胜", "ah": 0, "aa": 2},  # 错
        # 预测平局：2 中 1
        {"result": "平局", "ah": 1, "aa": 1},
        {"result": "平局", "ah": 2, "aa": 0},  # 错
        # 预测客胜：1 中 1
        {"result": "客胜", "ah": 0, "aa": 3},
    ])
    st = repo.stats()
    by = st["by_result"]
    assert set(by) == {"主胜", "平局", "客胜"}
    assert by["主胜"]["total"] == 3 and by["主胜"]["hit"] == 2
    assert by["平局"]["total"] == 2 and by["平局"]["hit"] == 1
    assert by["客胜"]["total"] == 1 and by["客胜"]["hit"] == 1


def test_by_result_rate_is_ratio_not_percentage():
    """命中率是 0~1 的小数，不是 67 这种整数——避免调用方再除一次 100。"""
    repo = _repo()
    _seed(repo._connect(), [{"result": "主胜", "ah": 1, "aa": 0},
                            {"result": "主胜", "ah": 0, "aa": 1}])
    rate = repo.stats()["by_result"]["主胜"]["rate"]
    assert 0.0 <= rate <= 1.0
    assert abs(rate - 0.5) < 1e-9


# ---- 契约 2：按联赛拆分 --------------------------------------------------

def test_by_league_groups_by_league_column():
    repo = _repo()
    _seed(repo._connect(), [
        {"league": "PL", "result": "主胜", "ah": 2, "aa": 1},
        {"league": "PL", "result": "主胜", "ah": 0, "aa": 1},  # 错
        {"league": "PD", "result": "客胜", "ah": 0, "aa": 2},
    ])
    by = repo.stats()["by_league"]
    assert by["PL"]["total"] == 2 and by["PL"]["hit"] == 1
    assert by["PD"]["total"] == 1 and by["PD"]["hit"] == 1


def test_by_league_empty_name_falls_back_not_dropped():
    """联赛名为空/空串时归入「其它」，不能让这场比赛凭空消失。

    早期记录或降级路径可能没写 league。丢弃会让总数对不上——
    用户拿「各项之和 ≠ 总场次」来质疑统计，比分组难看严重得多。
    """
    repo = _repo()
    _seed(repo._connect(), [
        {"league": "", "result": "主胜", "ah": 1, "aa": 0},
        {"league": None, "result": "主胜", "ah": 3, "aa": 0},
    ])
    by = repo.stats()["by_league"]
    assert by["其它"]["total"] == 2
    assert repo.stats()["total"] == 2


def test_breakdown_totals_match_overall():
    """两个细分维度的场次之和都必须等于总场次，否则统计口径不一致。"""
    repo = _repo()
    _seed(repo._connect(), [
        {"league": "PL", "result": "主胜", "ah": 2, "aa": 1},
        {"league": "PD", "result": "平局", "ah": 1, "aa": 1},
        {"league": "SA", "result": "客胜", "ah": 1, "aa": 2},
    ])
    st = repo.stats()
    assert sum(v["total"] for v in st["by_result"].values()) == st["total"]
    assert sum(v["total"] for v in st["by_league"].values()) == st["total"]


# ---- 契约 3：空库与异常 --------------------------------------------------

def test_empty_db_has_breakdown_keys():
    """空库时两个键存在且为空字典，而不是 KeyError。

    老库（升级前）调用方直接 st["by_result"] 会炸，这里保证字段恒定存在。
    """
    repo = _repo()
    st = repo.stats()
    assert st["by_result"] == {}
    assert st["by_league"] == {}
    assert st["total"] == 0


def test_unsettled_rows_excluded():
    """未结算（actual 为空）的记录不进任何细分维度。"""
    repo = _repo()
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions (fixture_id, league, result, kickoff) "
        "VALUES ('fx-pending','PL','主胜','2026-10-01T12:00:00+00:00')"
    )
    conn.commit()
    st = repo.stats()
    assert st["total"] == 0
    assert st["by_result"] == {}
    assert st["by_league"] == {}


def test_breakdown_survives_null_result():
    """result 为空的记录：不能让整个统计抛异常。"""
    repo = _repo()
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions (fixture_id, league, result, actual_home, "
        "actual_away, kickoff) VALUES ('fx-nr','PL',NULL,1,0,'2026-10-01T12:00:00+00:00')"
    )
    conn.commit()
    st = repo.stats()
    assert st["total"] == 1
    assert st["by_result"] == {}  # 方向未知，不计入方向维度
