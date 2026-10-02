"""配额计数：把 /status 里的「? / ?」换成真实发出的请求次数。

免费层额度确实存在（100 次/天），但备用源 football-data.org 免费层
没有额度查询端点，因此只能自己数。本测试锁定「实际拉取次数」语义：
- 主源成功 → 只记主源 1 次；
- 主源失败后切备用源 → 两次网络往返都算，共 2 次；
- 主源冷却中 → 不发请求，不计数；
- 计数回调抛异常 → 不影响数据获取。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import paths
import repository
from data_source import DataSourceRouter


class StubPrimary:
    def __init__(self, result=None, error=None):
        self.result = [{"fixture": {"id": 1}}] if result is None else result
        self.error = error
        self.calls = 0

    async def get_fixtures(self, *a, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class StubFallback:
    def __init__(self, result=None):
        self.result = [{"fixture": {"id": 2}}] if result is None else result
        self.calls = 0

    async def get_fixtures(self, *a, **kw):
        self.calls += 1
        return self.result


@pytest.fixture()
def quota_db(tmp_path, monkeypatch):
    """把配额表指向独立临时库，避免污染其它用例。"""
    db = tmp_path / "quota.db"
    monkeypatch.setattr(paths, "DB_PATH", db, raising=False)
    return db


def _table_counts(db) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT day, source, n FROM api_quota").fetchall()
    finally:
        conn.close()
    return {(d, s): n for d, s, n in rows}


def test_bump_accumulates_same_source(quota_db):
    repository.bump_quota("api-football")
    repository.bump_quota("api-football")
    repository.bump_quota("football-data")

    counts = _table_counts(quota_db)
    today = repository._quota_day()
    assert counts.get((today, "api-football")) == 2
    assert counts.get((today, "football-data")) == 1


def test_quota_today_isolates_other_days(quota_db):
    """昨天的计数不能混进今天。"""
    conn = sqlite3.connect(str(quota_db))
    conn.executescript(repository.QUOTA_SCHEMA)
    conn.execute("INSERT INTO api_quota(day, source, n) VALUES(?,?,?)",
                 ("1999-01-01", "api-football", 999))
    conn.commit()
    conn.close()

    repository.bump_quota("api-football")
    today = repository.quota_today()
    assert today == {"api-football": 1}


def test_counts_persist_across_reopen(quota_db):
    repository.bump_quota("football-data", 3)
    assert repository.quota_today()["football-data"] == 3


def test_bump_never_raises(monkeypatch, tmp_path):
    """计数是旁路功能：底层炸了也不能影响机器人。"""
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "no" / "dir" / "x.db",
                        raising=False)
    repository.bump_quota("api-football")  # 不应抛出
    assert repository.quota_today() == {}


def test_primary_success_counts_once(quota_db):
    calls: list[str] = []
    router = DataSourceRouter(StubPrimary(), StubFallback(), quota_sink=calls.append)
    asyncio.run(router.get_fixtures(39, 2026, "2026-10-02", "2026-10-03"))

    assert calls == ["api-football"]


def test_primary_failure_then_fallback_counts_twice(quota_db):
    """主源失败那次也是真实网络请求，同样消耗额度 —— 这正是额度消耗偏快的原因。"""
    calls: list[str] = []

    def sink(src):
        calls.append(src)
        repository.bump_quota(src)

    primary = StubPrimary(error=RuntimeError("boom"))
    router = DataSourceRouter(primary, StubFallback(), quota_sink=sink)
    asyncio.run(router.get_fixtures(39, 2026, "2026-10-02", "2026-10-03"))

    assert calls == ["api-football", "football-data"]
    assert repository.quota_today() == {"api-football": 1, "football-data": 1}


def test_primary_cooling_skips_primary(quota_db):
    calls: list[str] = []
    router = DataSourceRouter(StubPrimary(error=RuntimeError("boom")),
                              StubFallback(), quota_sink=calls.append)
    asyncio.run(router.get_fixtures(39, 2026, "2026-10-02", "2026-10-03"))
    calls.clear()

    # 冷却期内不再尝试主源，因此只记备用源
    asyncio.run(router.get_fixtures(39, 2026, "2026-10-02", "2026-10-03"))
    assert calls == ["football-data"]


def test_sink_exception_does_not_break_fetch(quota_db):
    def boom(_src):
        raise RuntimeError("sink down")

    router = DataSourceRouter(StubPrimary(), StubFallback(), quota_sink=boom)
    result = asyncio.run(router.get_fixtures(39, 2026, "2026-10-02", "2026-10-03"))
    assert result  # 数据照常拿到
