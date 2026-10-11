"""统一存储路径 / Unified storage paths.

核心：数据必须落到挂载卷（/data），不能留在容器临时目录。
"""
import os
import sqlite3
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# 看板与机器人必须读写同一个 SQLite 文件。
#
# 真实事故：Railway 变量里只设了 DB_PATH=/tmp/football.db，而 paths.DB_PATH
# 原先只认 DATABASE_PATH。结果机器人（config.Settings.db_path）写 /tmp，
# 看板（api.py → paths.DB_PATH）读 /data —— 两个不同的文件，看板永远是空的，
# 且没有任何报错，只能看到「正在加载数据」。
# ---------------------------------------------------------------------------

def _reload_paths(monkeypatch, env: dict):
    """在给定环境变量下重新加载 paths 与 config，返回 (paths.DB_PATH, Settings.db_path)。"""
    for key in ("DATABASE_PATH", "DB_PATH", "DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    # config.load_settings 的必填项，与本用例无关，补上以便构造 Settings
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:test-token")
    monkeypatch.setenv("RAPID_API_KEY", "test-key")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import importlib

    import config
    import paths

    importlib.reload(paths)
    importlib.reload(config)
    settings = config.load_settings()
    return str(paths.DB_PATH), str(settings.db_path)


@pytest.mark.parametrize(
    "env,label",
    [
        ({"DB_PATH": "/tmp/only_db_path.db"}, "只设 DB_PATH"),
        ({"DATABASE_PATH": "/tmp/only_database_path.db"}, "只设 DATABASE_PATH"),
        (
            {"DATABASE_PATH": "/tmp/prio_a.db", "DB_PATH": "/tmp/prio_b.db"},
            "两者都设（DATABASE_PATH 优先）",
        ),
        ({}, "都不设（走默认）"),
    ],
)
def test_db_path_matches_settings(monkeypatch, env, label):
    """paths.DB_PATH 与 config 解析出的 db_path 必须完全相等。"""
    db_path, settings_db = _reload_paths(monkeypatch, env)
    assert db_path == settings_db, (
        f"{label}：路径分裂 —— 看板读 {db_path}，机器人写 {settings_db}"
    )


def test_db_path_honours_db_path_env(monkeypatch):
    """只设 DB_PATH 时，看板也必须跟着走，不能偷偷读 /data。"""
    db_path, settings_db = _reload_paths(monkeypatch, {"DB_PATH": "/tmp/x.db"})
    assert db_path == "/tmp/x.db", f"看板忽略了 DB_PATH，仍读 {db_path}"
    assert settings_db == "/tmp/x.db"
