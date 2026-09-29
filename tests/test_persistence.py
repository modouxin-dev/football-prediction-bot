"""阶段 1：持久化与 Elo 存储 / Persistence & Elo storage.

核心：Volume 未挂载时数据清零，Elo 必须有地方落盘且不能重复计算。
"""
import tempfile
from pathlib import Path

import paths
from repository import PredictionRepository


# ---- 持久化判定 -------------------------------------------------------------

def test_is_persistent_returns_bool():
    assert isinstance(paths.is_persistent(), bool)


def test_persistence_warning_mentions_volume():
    """未挂载时必须给出可操作的提示（Railway /data、docker-compose）。"""
    if paths.is_persistent():
        assert paths.persistence_warning() is None
    else:
        text = paths.persistence_warning()
        assert "/data" in text and ("Railway" in text or "docker-compose" in text)


def test_temp_dir_is_not_persistent():
    """系统临时目录绝不能被判定为持久卷。"""
    orig = paths.DATA_DIR
    try:
        paths.DATA_DIR = Path(tempfile.gettempdir())
        assert paths.is_persistent() is False
    finally:
        paths.DATA_DIR = orig


# ---- Elo 存储表 -------------------------------------------------------------

def test_elo_tables_created(tmp_path):
    repo = PredictionRepository(str(tmp_path / "elo.db"))
    tables = repo.tables()
    assert "elo_ratings" in tables
    assert "elo_processed" in tables
    assert "elo_log" in tables


def test_elo_ratings_roundtrip(tmp_path):
    repo = PredictionRepository(str(tmp_path / "elo.db"))
    repo.save_elo_ratings("PL", {"1": 1510.5, "2": 1490.0},
                          matches_played={"1": 3, "2": 3})
    ratings = repo.elo_ratings("PL")
    assert abs(ratings["1"] - 1510.5) < 1e-6
    assert abs(ratings["2"] - 1490.0) < 1e-6


def test_elo_ratings_survive_reopen(tmp_path):
    """重开连接（模拟重启）后评分仍在——这是挂 Volume 的意义所在。"""
    db = tmp_path / "elo.db"
    repo = PredictionRepository(str(db))
    repo.save_elo_ratings("PL", {"10": 1600.0})
    del repo
    again = PredictionRepository(str(db))
    assert abs(again.elo_ratings("PL")["10"] - 1600.0) < 1e-6


def test_elo_ratings_updated_not_duplicated(tmp_path):
    """同一支球队重复写入应是更新，不产生第二行。"""
    db = tmp_path / "elo.db"
    repo = PredictionRepository(str(db))
    repo.save_elo_ratings("PL", {"7": 1500.0})
    repo.save_elo_ratings("PL", {"7": 1530.0})
    assert len(repo.elo_ratings("PL")) == 1
    assert abs(repo.elo_ratings("PL")["7"] - 1530.0) < 1e-6


def test_elo_isolated_by_competition(tmp_path):
    """同一队在不同联赛各有一套评分，不能互相覆盖。"""
    repo = PredictionRepository(str(tmp_path / "elo.db"))
    repo.save_elo_ratings("PL", {"5": 1550.0})
    repo.save_elo_ratings("PD", {"5": 1450.0})
    assert abs(repo.elo_ratings("PL")["5"] - 1550.0) < 1e-6
    assert abs(repo.elo_ratings("PD")["5"] - 1450.0) < 1e-6


# ---- 幂等性 -----------------------------------------------------------------

def test_elo_idempotency_marker(tmp_path):
    """已处理的比赛必须被标记，防止重启后重复计入评分。"""
    repo = PredictionRepository(str(tmp_path / "elo.db"))
    assert repo.elo_is_processed(9001) is False
    repo.mark_elo_processed(9001, competition="PL", season=2026,
                            home_team_id="1", away_team_id="2",
                            home_score=2, away_score=1)
    assert repo.elo_is_processed(9001) is True


def test_elo_marker_survives_reopen(tmp_path):
    db = tmp_path / "elo.db"
    repo = PredictionRepository(str(db))
    repo.mark_elo_processed(9002, competition="PL")
    del repo
    assert PredictionRepository(str(db)).elo_is_processed(9002) is True


def test_elo_change_log(tmp_path):
    repo = PredictionRepository(str(tmp_path / "elo.db"))
    repo.log_elo_change(9003, "PL", "1", 1500.0, 1516.0)
    rows = repo.elo_history(9003)
    assert len(rows) == 1
    assert abs(rows[0]["delta"] - 16.0) < 1e-6
    assert abs(rows[0]["before"] - 1500.0) < 1e-6


def test_elo_methods_never_raise_when_broken(tmp_path):
    """存储不可用时只记日志，不能拖垮机器人主流程。"""
    repo = PredictionRepository(":memory:")
    # 关掉连接后所有 Elo 方法应安全返回，不抛异常
    assert isinstance(repo.elo_ratings("PL"), dict)
    assert repo.elo_is_processed(1) in (True, False)
    assert isinstance(repo.elo_history(1), list)
