"""回填命令 / /backfill (Stage 9).

核心验证：**回填只补赛程与赛果，绝不伪造预测记录**。
"""
import sqlite3

import pytest

from repository import PredictionRepository


def _seed_matches(db, n, finished):
    """造 n 场比赛，其中前 finished 场有比分（已完赛）。"""
    repo = PredictionRepository(db)
    conn = repo._connect()
    for i in range(n):
        conn.execute(
            "INSERT OR REPLACE INTO matches (id,competition_code,season,utc_date,status,"
            "home_team_id,home_team_name,away_team_id,away_team_name,home_score,away_score,"
            "source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "PL", 2026, f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T15:00",
             "FT" if i < finished else "NS",
             "A", "A", "B", "B",
             1 if i < finished else None, 0 if i < finished else None,
             "t", "x"))
    conn.commit()
    return repo


def test_count_finished_only_counts_with_score(tmp_path):
    """只统计有比分的场次：未开赛的不能参与强度估计。"""
    db = str(tmp_path / "x.db")
    _seed_matches(db, 100, 60)
    assert PredictionRepository(db).count_finished_matches("PL") == 60


def test_count_finished_respects_competition(tmp_path):
    db = str(tmp_path / "x.db")
    repo = _seed_matches(db, 50, 30)
    conn = repo._connect()
    for i in range(100, 120):        # 另一个联赛，20 场已完赛
        conn.execute(
            "INSERT OR REPLACE INTO matches (id,competition_code,season,utc_date,status,"
            "home_team_id,home_team_name,away_team_id,away_team_name,home_score,away_score,"
            "source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "PD", 2026, f"2026-{(i % 28) + 1:02d}-01T15:00", "FT",
             "C", "C", "D", "D", 2, 1, "t", "x"))
    conn.commit()
    r = PredictionRepository(db)
    assert r.count_finished_matches("PL") == 30, "联赛间不应互相污染"
    assert r.count_finished_matches("PD") == 20
    assert r.count_finished_matches() == 50, "不传参数时应统计全部"


def test_count_finished_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    assert PredictionRepository(db).count_finished_matches("PL") == 0


def test_count_finished_never_raises(tmp_path):
    """库损坏时必须返回 0 而不是抛异常——回填是运维命令，不能让它崩。"""
    db = str(tmp_path / "broken.db")
    with open(db, "wb") as f:
        f.write(b"not a sqlite database at all")
    assert PredictionRepository(db).count_finished_matches("PL") == 0


def test_backfill_does_not_touch_predictions(tmp_path):
    """回填前 predictions 为空，回填后必须仍为空。

    这是本文件最重要的一条：回填若顺手写了预测，健康度曲线就会变成
    一条由「事后诸葛亮」画出来的假曲线。
    """
    db = str(tmp_path / "x.db")
    repo = _seed_matches(db, 80, 50)
    conn = repo._connect()
    n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert n == 0, "前提：回填前不应有任何预测记录"
    # 回填只写 matches；predictions 由日常推送产生
    assert repo.count_finished_matches("PL") == 50
    conn2 = repo._connect()
    assert conn2.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0


def test_backfill_command_is_registered():
    """/backfill 必须注册为管理员指令。"""
    from commands import build_dispatcher

    specs = {s.name: s for s in build_dispatcher().specs}
    assert "backfill" in specs
    assert specs["backfill"].admin_only, "回填属于运维操作，必须限管理员"
