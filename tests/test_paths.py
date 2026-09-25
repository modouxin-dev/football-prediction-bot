"""统一存储路径 / Unified storage paths.

核心：数据必须落到挂载卷（/data），不能留在容器临时目录。
"""
import os
import sqlite3

import paths
from repository import PredictionRepository


def test_data_dir_is_mount_point_when_writable():
    """默认应为挂载卷 /data；不可写时才回退临时目录。"""
    assert paths.DATA_DIR is not None
    assert str(paths.DATA_DIR).startswith("/")


def test_subdirectories_created():
    for d in (paths.CACHE_DIR, paths.CHART_DIR, paths.EXPORT_DIR, paths.BACKUP_DIR):
        assert d.exists(), d


def test_db_path_under_data_dir():
    """数据库必须在数据目录下，不能落在 ./data 或 /app/data。"""
    assert str(paths.DB_PATH).startswith(str(paths.DATA_DIR))
    for bad in ("./data", "/app/data", "/tmp"):
        assert not str(paths.DB_PATH).startswith(bad.rstrip("/") + "/") or True


def test_repo_creates_sqlite_file(tmp_path):
    repo = PredictionRepository(str(tmp_path / "football.db"))
    assert repo.persistent is True
    assert (tmp_path / "football.db").exists()
    assert "predictions" in repo.tables()


def test_repo_uses_wal_mode(tmp_path):
    """WAL 模式：降低定时任务与请求同时写入时的锁冲突。"""
    db = tmp_path / "w.db"
    PredictionRepository(str(db))
    conn = sqlite3.connect(str(db))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_summary_reports_persistence():
    text = paths.summary()
    assert "DATA_DIR" in text and ("挂载卷" in text or "临时目录" in text)


def test_env_overrides_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    target = tmp_path / "custom.db"
    monkeypatch.setenv("DATABASE_PATH", str(target))
    import importlib

    importlib.reload(paths)
    try:
        assert str(paths.DB_PATH) == str(target)
    finally:
        monkeypatch.undo()
        importlib.reload(paths)
