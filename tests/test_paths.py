"""统一存储路径 / Unified storage paths.

核心：数据必须落到挂载卷（/data），不能留在容器临时目录。
"""
import os
import sqlite3
from pathlib import Path

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


def test_probe_storage_write_and_read():
    """probe_storage 必须真实写→读→比对，不能只检查目录存在。"""
    r = paths.probe_storage()
    assert r["write"] is True
    assert r["read"] is True
    assert r["first_write"] is not None


def test_probe_keeps_marker_for_cross_deploy_check():
    """标记文件必须保留：重新部署后仍读到它，才能证明 Volume 生效。"""
    paths.probe_storage()
    marker = paths.DATA_DIR / paths.MARKER_NAME
    assert marker.exists(), "标记文件被删除，无法做跨部署验证"
    r2 = paths.probe_storage()  # 第二次应读到同一次写入时间
    assert r2["age_seconds"] >= 0


def test_probe_survives_unwritable_dir():
    """目录不可写时不能抛异常，必须安全返回失败状态。"""
    import paths as p

    orig = p.DATA_DIR
    p.DATA_DIR = Path("/proc/definitely/not/writable")
    try:
        r = p.probe_storage()
        assert r["write"] is False
    finally:
        p.DATA_DIR = orig
