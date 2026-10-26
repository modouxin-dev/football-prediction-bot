"""超期未结算的回填：把「已开赛但永远结算不了」的历史遗留补回统计。

背景（这是本文件的存在理由）
----------------------------
``pending()`` 加了时间下界后，超期行不再堵死结算队列——结算停摆被修好了。
但代价是：这些行**永远不会被结算**。它们既不进命中率统计，也不会自己消失，
只能作为「超期未结算 N 场」显示在看板上（当前线上 463 场）。

为什么按天补而不是按场补
------------------------
数据源一次请求返回当天全部比赛，故成本是「天数」而非「场次数」。
463 场若分布在 40 天里，就是 40 次请求，而不是 463 次。

为什么不重算预测
----------------
只回填**真实赛果**。预测概率是当时落盘的，拿现在的积分榜去重算会引入
前视偏差（look-ahead bias），命中率会虚高——那是自欺欺人。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from repository import PredictionRepository


def _mk(tmp_path, rows: list[tuple[str, str]]) -> PredictionRepository:
    """建库并插入 (fixture_id, kickoff) 列表。"""
    repo = PredictionRepository(str(tmp_path / "t.db"))
    conn = repo._connect()
    for fid, kick in rows:
        conn.execute(
            "INSERT INTO predictions (fixture_id, home, away, league, kickoff, "
            "home_prob, draw_prob, away_prob, best_score, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, "A", "B", "L", kick, 0.5, 0.3, 0.2, "1-0", "home", kick))
    conn.commit()
    return repo


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# ---- 日期枚举 ---------------------------------------------------------------


def test_stale_days_empty_when_nothing_stale(tmp_path):
    """近期未结算不算超期。"""
    repo = _mk(tmp_path, [("f1", _days_ago(1))])
    # 窗口默认 3 天，1 天前仍在窗口内
    assert repo.stale_pending_days() == []


def test_stale_days_lists_oldest_first(tmp_path):
    """由远及近：先把最老的补掉，总量才会单调下降。

    若由近及远，中途额度耗尽时剩下的永远是最老的那批——永远清不完。
    """
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(10)),
                          ("f3", _days_ago(20))])
    days = repo.stale_pending_days()
    assert len(days) == 3
    assert days == sorted(days)


def test_stale_days_deduplicates(tmp_path):
    """同一天多场只返回一天——成本是天数不是场次数。"""
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(30)),
                          ("f3", _days_ago(30))])
    assert len(repo.stale_pending_days()) == 1


def test_stale_days_respects_limit(tmp_path):
    """limit 是额度保护：超出部分本次不补。"""
    rows = [(f"f{i}", _days_ago(10 + i)) for i in range(10)]
    repo = _mk(tmp_path, rows)
    assert len(repo.stale_pending_days(limit=3)) == 3


def test_stale_days_skips_malformed_kickoff(tmp_path):
    """脏日期不得作为参数发出去。"""
    repo = PredictionRepository(str(tmp_path / "t.db"))
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions (fixture_id, home, away, league, kickoff, "
        "home_prob, draw_prob, away_prob, best_score, result, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("f1", "A", "B", "L", "not-a-date", 0.5, 0.3, 0.2, "1-0", "home",
         _days_ago(40)))
    conn.commit()
    assert repo.stale_pending_days() == []


def test_stale_count_for_day(tmp_path):
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(30)),
                          ("f3", _days_ago(20))])
    d30 = _days_ago(30)[:10]
    assert repo.stale_pending_count_for_day(d30) == 2


def test_stale_count_for_day_drops_after_settle(tmp_path):
    """回填后该日计数应下降——用于前后对账。"""
    repo = _mk(tmp_path, [("f1", _days_ago(30))])
    d30 = _days_ago(30)[:10]
    assert repo.stale_pending_count_for_day(d30) == 1
    repo.settle("f1", 1, 0)
    assert repo.stale_pending_count_for_day(d30) == 0


# ---- 回填编排 ---------------------------------------------------------------


class _FakeRepo:
    """最小替身：只提供回填方法用到的接口。"""

    def __init__(self, days: list[str], stale_before: int = 100):
        self._days = days
        self._stale = stale_before
        self.settled: list[tuple[str, int, int]] = []

    def stale_pending_days(self, limit: int = 60) -> list[str]:
        return self._days[:limit]

    def count_stale_pending(self) -> int:
        return self._stale

    def settle(self, fixture_id, home, away) -> bool:
        self.settled.append((str(fixture_id), int(home), int(away)))
        self._stale = max(0, self._stale - 1)
        return True


@pytest.mark.asyncio
async def test_backfill_no_stale_days_returns_zero():
    """没有超期行时直接返回，不浪费额度。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo([], stale_before=0)
    res = await svc.backfill_stale_settlements()
    assert res["scanned_days"] == 0
    assert res["settled"] == 0


@pytest.mark.asyncio
async def test_backfill_settles_finished_matches_only():
    """只有 status 为已完场才回填，未开赛/进行中不算。"""
    from service import PredictionService

    finished = [{"fixture": {"id": "1", "status": {"short": "FT"}},
                 "goals": {"home": 2, "away": 1}},
                {"fixture": {"id": "2", "status": {"short": "NS"}},
                 "goals": {"home": None, "away": None}}]
    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01"])

    async def _fetch(d):
        return finished

    svc._fetch_all_by_date = _fetch
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()
    assert res["settled"] == 1
    assert svc.repo.settled == [("1", 2, 1)]


@pytest.mark.asyncio
async def test_backfill_reports_failed_days_not_as_success():
    """取数失败的日期必须列出来，不能算进 days_done。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01", "2026-08-02"], stale_before=50)

    async def _fetch(d):
        if d == "2026-08-01":
            raise RuntimeError("额度耗尽")
        return []

    svc._fetch_all_by_date = _fetch
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()
    assert res["failed_days"] == ["2026-08-01"]
    assert res["days_done"] == 1


@pytest.mark.asyncio
async def test_backfill_decreases_remaining(tmp_path):
    """回填后剩余量必须下降——这是本功能唯一的价值证明。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01"], stale_before=463)

    async def _fetch(d):
        return [{"fixture": {"id": "1", "status": {"short": "FT"}},
                 "goals": {"home": 1, "away": 0}}]

    svc._fetch_all_by_date = _fetch
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()
    assert res["remaining_before"] == 463
    assert res["remaining_after"] == 462
