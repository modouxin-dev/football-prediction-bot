"""测试隔离：任何测试都不得写入真实挂载卷 /data。

paths.py 在导入时就解析 DATA_DIR / DATABASE_PATH 等模块级常量，
本文件由 pytest 最先导入，因此在这里把路径改到临时目录，
后续所有模块导入都会拿到临时路径，从而隔离真实数据。

否则测试写入的假数据会被当作缓存命中，干扰赛程三态判断
（「接口故障」被误判为「窗口无比赛」）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.gettempdir()) / "football-test-storage"

for _sub in ("", "cache", "charts", "exports", "backups"):
    (_ROOT / _sub).mkdir(parents=True, exist_ok=True)

os.environ["DATA_DIR"] = str(_ROOT)
os.environ["DATABASE_PATH"] = str(_ROOT / "football.db")
os.environ["CACHE_DIR"] = str(_ROOT / "cache")
os.environ["CHART_DIR"] = str(_ROOT / "charts")


def _reset_db() -> None:
    """清空测试库，避免用例之间互相污染。

    query_fixtures 会把拉到的赛程落盘，若不清理，下一个用例会命中
    上一用例留下的缓存，导致「接口故障」被误判为「窗口无比赛」。
    """
    import sqlite3

    db = Path(os.environ["DATABASE_PATH"])
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(str(db))
        for table in ("matches", "predictions", "sync_log"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.Error:
                pass  # 表还没建
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def pytest_configure(config):  # noqa: D103
    _reset_db()


def pytest_runtest_setup(item):  # noqa: D103
    _reset_db()
