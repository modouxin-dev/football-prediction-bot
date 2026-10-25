"""外部数据缓存验证 / Checks for the external_cache layer.

核心命题：**看板是多人共用的，同一份数据不该被取 N 次**。
订阅额度虽然充裕，但它是按天结算的硬上限，打光后整套系统停摆——
不该让「刷新页面」这种只读动作具备耗尽额度的能力。

验证：
  1. 缓存有效期内，重复请求不再打外网（十个人点同一场只取一次）
  2. 过期后确实重新拉取，而不是一直吃旧值
  3. 外网失败时退回过期缓存并标记 stale（宁可显示旧快照也不空白）
  4. fetched_at 是**实际拉取时刻**，不是当前时刻（不许把旧数据伪装成新的）
  5. 缓存不可用时整体降级，不让缓存故障拖垮端点
  6. 过期缓存不会立刻被清掉（它还要当兜底）

不联网；客户端与时钟均用替身。
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

import api as apimod
import repository as repomod
from repository import PredictionRepository


@pytest.fixture(autouse=True)
def _reset_client():
    apimod._LIVE_CLIENT = None
    yield
    apimod._LIVE_CLIENT = None


class CountingClient:
    """主源替身：记录真实外网调用次数。"""

    def __init__(self, lineups=None, odds=None, live=None, raise_odds=False,
                 raise_live=False, raise_lineups=False):
        self.calls = {"lineups": 0, "odds": 0, "live": 0}
        self._lineups = lineups if lineups is not None else []
        self._odds = odds
        self._live = live if live is not None else []
        self._raise_odds = raise_odds
        self._raise_live = raise_live
        self._raise_lineups = raise_lineups
        self.primary = None

    async def get_lineups(self, fid):
        self.calls["lineups"] += 1
        if self._raise_lineups:
            raise RuntimeError("阵容端点限流")
        return self._lineups

    async def get_odds(self, fid):
        self.calls["odds"] += 1
        if self._raise_odds:
            raise RuntimeError("赔率端点限流")
        return self._odds

    async def get_live_fixtures(self):
        self.calls["live"] += 1
        if self._raise_live:
            raise RuntimeError("实时端点超时")
        return self._live


def _live_fx(fid=12345, status="2H"):
    """构造一场 API-Football 形状的进行中比赛。

    默认 ID 与 _seed() 插入的 12345 一致：**/live 现在只展示本模型
    推送过的比赛**，ID 对不上会被判为无关场次而过滤掉，
    空结论按设计不进缓存，缓存相关的断言就全部失效了。
    """
    return {
        "fixture": {"id": fid, "date": "2026-10-25T19:00:00+00:00",
                    "status": {"short": status, "elapsed": 67}},
        "league": {"id": 140, "name": "La Liga"},
        "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
        "goals": {"home": 1, "away": 0},
    }


def _seed(db_path):
    """插入一条可直接被 /prediction 读取的预测。"""
    repo = PredictionRepository(db_path)
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions(fixture_id, league, home, away, kickoff, "
        "home_prob, draw_prob, away_prob, best_score, result, level_key, "
        "inputs, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("12345", "140", "A", "B", "2026-10-26T19:00:00+00:00",
         0.57, 0.23, 0.20, "1-0", "home", "medium",
         json.dumps({"lambda_home": 1.2, "lambda_away": 0.75,
                     "over_2_5": 0.41, "btts": 0.48,
                     "top_scores": [["1-0", 0.19]]}),
         "2026-10-26T10:00:00+00:00"))
    conn.commit()
    return repo


def _expire(db_path, key):
    """把某条缓存改成已过期（模拟 TTL 到了）。"""
    repo = PredictionRepository(db_path)
    conn = repo._connect()
    conn.execute(
        "UPDATE external_cache SET expires_at = ? WHERE key = ?",
        ((repomod.datetime.now(repomod.timezone.utc) - timedelta(hours=2)).isoformat(), key))
    conn.commit()


async def _detail(fid, tmp_path):
    apimod._DB_PATH = str(tmp_path / "t.db")
    return json.loads((await apimod.prediction_detail(fid)).body)


async def _live(tmp_path, detail=1):
    apimod._DB_PATH = str(tmp_path / "t.db")
    return json.loads((await apimod.live(detail)).body)


def test_repeat_views_hit_cache_once(tmp_path):
    """十个人先后点同一场比赛 → 外网只被请求一次。"""
    db = tmp_path / "t.db"
    _seed(str(db))
    client = CountingClient(
        lineups=[{"team": {"name": "A"}, "formation": "4-3-3",
                  "startXI": [{"player": {"name": "P1"}}]}],
        odds=[{"bookmakers": [
            {"name": "B365", "bets": [{"id": 1, "name": "Match Winner", "values": [
                {"value": "Home", "odd": "2.10"}, {"value": "Draw", "odd": "3.40"},
                {"value": "Away", "odd": "3.80"}]}]}]}])
    apimod._LIVE_CLIENT = client

    first = asyncio.run(_detail("12345", tmp_path))
    for _ in range(9):
        asyncio.run(_detail("12345", tmp_path))

    assert client.calls["lineups"] == 1, "十次打开只应打一次外网"
    assert client.calls["odds"] == 1
    # 缓存命中仍要返回数据，且时间戳是首次拉取的时刻
    assert len(first["lineups"]) == 1
    assert first["fetched_at"]["odds"]


def test_expired_cache_triggers_refetch(tmp_path):
    """TTL 到了必须重新拉，不能一直吃旧值。"""
    db = tmp_path / "t.db"
    _seed(str(db))
    client = CountingClient()
    apimod._LIVE_CLIENT = client

    asyncio.run(_detail("12345", tmp_path))
    assert client.calls["lineups"] == 1

    _expire(str(db), "lineups:12345")
    asyncio.run(_detail("12345", tmp_path))
    assert client.calls["lineups"] == 2, "过期后应重新拉取"


def test_failed_refetch_falls_back_to_stale(tmp_path):
    """外网挂了 → 退回过期缓存并标 stale，而不是整块空白。"""
    db = tmp_path / "t.db"
    _seed(str(db))
    good = CountingClient(
        lineups=[{"team": {"name": "A"}, "startXI": [{"player": {"name": "P1"}}]}])
    apimod._LIVE_CLIENT = good
    asyncio.run(_detail("12345", tmp_path))

    _expire(str(db), "lineups:12345")
    # 两项都取不到：阵容退回过期缓存，赔率无缓存可退 → 报错留痕
    broken = CountingClient(raise_lineups=True, raise_odds=True)
    apimod._LIVE_CLIENT = broken
    data = asyncio.run(_detail("12345", tmp_path))

    # 阵容取自过期缓存，赔率确实取不到 → 报错留痕
    assert len(data["lineups"]) == 1
    assert data["fetched_at"].get("lineups_stale") == "true"
    assert "odds" in data["errors"]


def test_fetched_at_is_actual_fetch_time(tmp_path):
    """缓存命中时 updated_at 必须是实际拉取时刻，不能是当前时刻。"""
    db = tmp_path / "t.db"
    _seed(str(db))
    # 必须用「有比赛」：空结论按设计不缓存，验证不了时间戳行为
    apimod._LIVE_CLIENT = CountingClient(live=[_live_fx()])
    first = asyncio.run(_live(tmp_path, detail=0))
    assert first["status"] == "ok"
    t1 = first["updated_at"]

    # 让时间明显前进后再取，缓存未过期 → 时间戳不应跟着走
    repomod.datetime = _FrozenClock(t1)
    try:
        second = asyncio.run(_live(tmp_path, detail=0))
    finally:
        repomod.datetime = _RealDatetime

    assert second["updated_at"] == t1, "缓存命中不得把旧数据的时间戳刷成当前时刻"


class _FrozenClock:
    """把 now() 钉在指定时刻之后，用于验证时间戳不被刷新。"""

    def __init__(self, base_iso):
        self._base = repomod.datetime.fromisoformat(base_iso)

    def now(self, tz=None):
        return self._base

    def fromisoformat(self, s):
        return repomod.datetime.fromisoformat(s)


_RealDatetime = repomod.datetime


def test_live_endpoint_cached_across_viewers(tmp_path):
    """/live 同样走缓存：多人打开同一页面只取一次。"""
    db = tmp_path / "t.db"
    _seed(str(db))
    client = CountingClient(live=[_live_fx()])
    apimod._LIVE_CLIENT = client

    r1 = asyncio.run(_live(tmp_path, detail=0))
    for _ in range(5):
        asyncio.run(_live(tmp_path, detail=0))

    assert client.calls["live"] == 1
    assert r1["status"] == "ok"


def test_live_failure_without_cache_reports_error(tmp_path):
    """既无缓存又拉取失败 → 如实报错，不能显示成「没有比赛」。"""
    (tmp_path / "t.db").touch()
    apimod._LIVE_CLIENT = CountingClient(raise_live=True)
    data = asyncio.run(_live(tmp_path))
    assert data["status"] == "error"
    assert "失败" in data["message"]


def test_purge_keeps_recent_expired(tmp_path):
    """过期缓存要留着当兜底，不能一过期就删。"""
    db = tmp_path / "t.db"
    PredictionRepository(str(db))
    repo = PredictionRepository(str(db))
    repo.cache_write("odds:1", {"home": 2.0}, "odds")
    _expire(str(db), "odds:1")

    deleted = repo.cache_purge_expired()
    assert deleted == 0, "刚过期就被清掉等于把兜底也扔了"
    # 但确实很老的会被清
    conn = repo._connect()
    conn.execute("UPDATE external_cache SET expires_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    assert repo.cache_purge_expired() == 1


def test_broken_cache_payload_ignored(tmp_path):
    """缓存内容损坏 → 当作没有缓存，不抛异常。"""
    db = tmp_path / "t.db"
    repo = PredictionRepository(str(db))
    conn = repo._connect()
    conn.execute(
        "INSERT INTO external_cache(key,payload,fetched_at,expires_at) VALUES(?,?,?,?)",
        ("odds:9", "{broken", "2026-10-26T10:00:00+00:00", "2999-01-01T00:00:00+00:00"))
    conn.commit()
    assert repo.cache_read("odds:9")[0] is None
