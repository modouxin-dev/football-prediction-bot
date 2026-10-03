"""storage_check.py 的退出码契约（总控审计 2026-10-03 · 4.1）。

它是运维脚本，机器人不调用它，所以不会被运行时用例覆盖到。
这里锁住最关键的一点：**有问题时必须以非 0 退出**，否则 cron /
健康检查会误以为一切正常。
"""
import importlib.util
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "storage_check", ROOT / "storage_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(db):
    """以子进程方式跑，才能拿到真实退出码。"""
    import subprocess
    p = subprocess.run([sys.executable, str(ROOT / "storage_check.py"), db],
                       capture_output=True, text=True)
    return p.returncode, p.stdout


def test_healthy_db_exits_zero(tmp_path):
    db = tmp_path / "h.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE matches(id INTEGER, home_score INTEGER)")
    con.executemany("INSERT INTO matches VALUES(?,?)", [(i, 1) for i in range(80)])
    con.execute("CREATE TABLE predictions(id TEXT)")
    con.commit()
    con.close()
    code, out = _run(str(db))
    assert code == 0, out
    assert "完整性校验通过" in out
    assert "恢复脚本 restore.sh 存在" in out


def test_missing_db_exits_nonzero(tmp_path):
    code, out = _run(str(tmp_path / "nope.db"))
    assert code == 1
    assert "数据库文件不存在" in out


def test_corrupt_db_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage")
    code, out = _run(str(bad))
    assert code == 1
    assert "ERR" in out
