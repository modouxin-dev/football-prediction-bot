"""数据库并发与鲁棒性 / DB concurrency & robustness.

云环境（Railway Volume）下 SQLite 写锁冲突远比本地频繁，
必须验证：退避重试真的会被触发，且总耗时可控。
"""
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import repository
from repository import PredictionRepository
from elo import EloEngine


def test_atomic_commits(tmp_path):
    repo = PredictionRepository(str(tmp_path / "a.db"))
    with repo.atomic() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO elo_ratings VALUES ('A','PL',1500,0,NULL,'t')"
        )
    assert repo.elo_ratings("PL")["A"] == 1500.0


def test_atomic_rolls_back_on_error(tmp_path):
    """事务中抛异常必须整体回滚，不能留下半截数据。"""
    repo = PredictionRepository(str(tmp_path / "a.db"))
    try:
        with repo.atomic() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO elo_ratings VALUES ('A','PL',1500,0,NULL,'t')"
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert repo.elo_ratings("PL") == {}


def test_atomic_restores_isolation_level(tmp_path):
    """事务结束必须还原 isolation_level，否则会影响其它方法的既有行为。"""
    repo = PredictionRepository(str(tmp_path / "a.db"))
    conn = repo._connect()
    before = conn.isolation_level
    with repo.atomic():
        pass
    assert conn.isolation_level == before


def test_atomic_restores_busy_timeout(tmp_path):
    """事务结束后 busy_timeout 必须还原为 15s，不能长期停在 100ms。"""
    repo = PredictionRepository(str(tmp_path / "a.db"))
    with repo.atomic():
        pass
    conn = repo._connect()
    cur = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert cur == repository.CONNECTION_BUSY_TIMEOUT_MS


def test_backoff_is_triggered_on_lock(tmp_path, monkeypatch):
    """核心：锁冲突时退避真的被调用，不是摆设。"""
    db = tmp_path / "lock.db"
    repo = PredictionRepository(str(db))
    holder = sqlite3.connect(str(db), timeout=15)
    holder.execute("BEGIN IMMEDIATE")

    calls = {"n": 0}
    original = repository._sleep_backoff

    def spy(attempt, reason=""):
        calls["n"] += 1
        return original(attempt, reason)

    monkeypatch.setattr(repository, "_sleep_backoff", spy)
    try:
        started = time.time()
        try:
            with repo.atomic(retries=2):
                pass
        except sqlite3.OperationalError:
            pass
        elapsed = time.time() - started
    finally:
        holder.rollback()
        holder.close()

    assert calls["n"] == 2, f"退避应被调用 2 次，实际 {calls['n']}"
    # 优化前是 (retries+1) × 15s = 45s 的长阻塞；现在必须控制在 2s 内
    assert elapsed < 2.0, f"锁冲突总耗时 {elapsed:.1f}s，过长"


def test_backoff_delay_grows_exponentially(monkeypatch):
    """退避步长按 2 的幂增长（指数退避），且带 50%~100% 抖动。

    注意：抖动是随机的，直接比较相邻两次的比值会偶发失败
    （最坏情况 slept[0] 取 100%、slept[1] 取 50%，比值降到 1.0）。
    因此改为断言每次延迟都落在「该档基准 × [0.5, 1.0]」区间内——
    这既验证了指数增长，又是确定性的。
    """
    slept = []
    monkeypatch.setattr(repository.time, "sleep", lambda s: slept.append(s))
    for attempt in range(4):
        repository._sleep_backoff(attempt, "t")
    base = repository.BACKOFF_BASE_SECONDS
    cap = repository.BACKOFF_MAX_SECONDS
    assert len(slept) == 4
    for attempt, got in enumerate(slept):
        expected = min(base * (2 ** attempt), cap)
        assert expected * 0.5 - 1e-9 <= got <= expected + 1e-9, (
            f"attempt={attempt} 退避 {got}s 不在 [{expected*0.5}, {expected}] 内")
    # 指数增长：理论上界严格递增（在触及上限前）
    assert base * 2 > base and min(base * 4, cap) > min(base * 2, cap)


def test_concurrent_elo_writes_no_duplicates():
    """并发写入：不报错、不重复计数。"""
    db = Path(tempfile.mkdtemp()) / "c.db"
    errors = []

    def worker(n):
        try:
            eng = EloEngine(repo=PredictionRepository(str(db)), competition="PL")
            for i in range(40):
                eng.apply_match(n * 1000 + i, f"T{n % 6}", f"T{(n + 1) % 6}", 2, 1)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入出错：{errors[:3]}"
    conn = PredictionRepository(str(db))._connect()
    total = conn.execute("SELECT COUNT(*) FROM elo_processed").fetchone()[0]
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT fixture_id) FROM elo_processed").fetchone()[0]
    assert total == 400, f"应为 400 场，实际 {total}"
    assert distinct == 400, "存在重复计算的比赛"


def test_concurrent_elo_log_is_paired():
    """每场比赛应恰好产生 2 条变更记录（主队 + 客队各一条）。"""
    db = Path(tempfile.mkdtemp()) / "c.db"

    def worker(n):
        eng = EloEngine(repo=PredictionRepository(str(db)), competition="PL")
        for i in range(25):
            eng.apply_match(n * 1000 + i, f"T{n % 5}", f"T{(n + 1) % 5}", 2, 1)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = PredictionRepository(str(db))._connect()
    logs = conn.execute("SELECT COUNT(*) FROM elo_log").fetchone()[0]
    proc = conn.execute("SELECT COUNT(*) FROM elo_processed").fetchone()[0]
    assert logs == proc * 2


# ============================================================================
# WAL 与并发读写（Step 3）
# ============================================================================

def test_wal_is_enabled(tmp_path):
    """WAL 必须真正生效：读写不互相阻塞，是并发正确性的基础。

    审计要求「强制执行 PRAGMA journal_mode=WAL」；本项目在
    repository._connect() 中已启用，这里断言它确实生效，
    防止将来被无意改回 delete 模式。
    """
    repo = PredictionRepository(str(tmp_path / "w.db"))
    conn = repo._connect()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"journal_mode={mode}，应为 wal"


def test_concurrent_read_during_heavy_writes():
    """写入 400 场的同时开一个持续读取的线程：不得出现 database is locked。

    这是 Railway 上的真实场景——定时任务写、用户命令读，同时进行。
    """
    db = Path(tempfile.mkdtemp()) / "rw.db"
    # 先在**主线程**建好库：否则读线程会卡在构造函数的建表 DDL 上等写锁，
    # 等它建完表，写线程早已跑完、stop 也已置位，循环一次都进不去，
    # 于是断言 reads["n"] > 0 偶发失败——测的是启动竞态，不是并发读写。
    PredictionRepository(str(db))
    stop = threading.Event()
    ready = threading.Event()   # 读线程已成功读满一轮，写线程才准开工
    read_errors: list[str] = []
    reads = {"n": 0}

    def reader():
        repo = PredictionRepository(str(db))
        while not stop.is_set():
            try:
                repo.elo_ratings("PL")
                repo.stats()
                reads["n"] += 1
                ready.set()   # 第一次成功后放行写线程，确保真的在并发
            except Exception as exc:
                read_errors.append(f"{type(exc).__name__}: {exc}")
                return
            time.sleep(0.001)

    def writer(n):
        eng = EloEngine(repo=PredictionRepository(str(db)), competition="PL")
        for i in range(40):
            eng.apply_match(n * 1000 + i, f"T{n % 6}", f"T{(n + 1) % 6}", 2, 1)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    # 等读线程真正跑起来再开写；超时则视为环境异常，让下面的断言给出明确信息
    ready.wait(timeout=10)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    stop.set()
    t.join(timeout=5)

    assert not read_errors, f"读取线程报错：{read_errors[:3]}"
    assert reads["n"] > 0, "读取线程一次都没成功"
    conn = PredictionRepository(str(db))._connect()
    assert conn.execute("SELECT COUNT(*) FROM elo_processed").fetchone()[0] == 400
    print(f"\n    并发读写：读取 {reads['n']} 次，写入 400 场，0 错误")
