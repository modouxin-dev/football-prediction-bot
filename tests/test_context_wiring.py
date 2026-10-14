"""赛前上下文信号的**接入层**测试（api_client 新方法 + service 合并逻辑）。

单元测试（`test_context.py` / `test_weather.py`）证明数学是对的；本文件证明
它们**真的接上了** —— 拿不到数据时静默降级，拿到时才生效。

    Unit tests prove the math; this file proves it is actually **wired in** —
    degrading silently when data is missing, and taking effect when present.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api_client import APIError, FootballAPI
from service import PredictionService

HOME, AWAY = 1, 2


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _api() -> FootballAPI:
    return FootballAPI("dummy-key", provider="apisports")


# ---- api_client 新方法 -----------------------------------------------------

def test_get_squad_error_returns_empty(monkeypatch):
    api = _api()

    async def boom(path, params, ttl=0.0, fresh=False):
        raise APIError("403 forbidden")

    monkeypatch.setattr(api, "_get", boom)
    assert run(api.get_squad(33)) == []


def test_get_squad_returns_payload(monkeypatch):
    api = _api()
    payload = [{"player": {"id": 7, "pos": "F"}}]

    async def fake(path, params, ttl=0.0, fresh=False):
        assert path == "players/squads"
        assert params["team"] == 33
        return payload

    monkeypatch.setattr(api, "_get", fake)
    assert run(api.get_squad(33)) == payload


def test_get_team_player_stats_paginates_until_short_page(monkeypatch):
    api = _api()
    pages: list[int] = []

    async def fake(path, params, ttl=0.0, fresh=False):
        pages.append(params["page"])
        # 第一页满 20 人 → 继续翻；第二页只有 5 人 → 停止
        return [{"player": {"id": i}} for i in range(20 if params["page"] == 1 else 5)]

    monkeypatch.setattr(api, "_get", fake)
    out = run(api.get_team_player_stats(33, 2026))
    assert len(out) == 25
    assert pages == [1, 2]


def test_get_team_player_stats_caps_pages(monkeypatch):
    """接口一直返回满页时必须在上限处停下，不能把额度烧光。"""
    api = _api()
    pages: list[int] = []

    async def fake(path, params, ttl=0.0, fresh=False):
        pages.append(params["page"])
        return [{"player": {"id": 1}}] * 20      # 永远满页

    monkeypatch.setattr(api, "_get", fake)
    out = run(api.get_team_player_stats(33, 2026))
    assert pages == [1, 2, 3]
    assert len(out) == 60


def test_get_team_player_stats_error_midway_keeps_partial(monkeypatch):
    api = _api()

    async def fake(path, params, ttl=0.0, fresh=False):
        if params["page"] == 1:
            return [{"player": {"id": 1}}] * 20
        raise APIError("boom")

    monkeypatch.setattr(api, "_get", fake)
    # 第二页失败不应丢掉第一页已有数据
    assert len(run(api.get_team_player_stats(33, 2026))) == 20


# ---- service 合并逻辑 ------------------------------------------------------

class _FakeAPI:
    def __init__(self, lineups=None, squad=None, stats=None, injuries=None):
        self.lineups = lineups or []
        self.squad = squad or []
        self.stats = stats or []
        self.injuries = injuries or []

    async def get_injuries(self, fixture_id):
        return self.injuries

    async def get_lineups(self, fixture_id):
        return self.lineups

    async def get_squad(self, team_id):
        return self.squad

    async def get_team_player_stats(self, team_id, season):
        return self.stats


def _service(api, **over) -> PredictionService:
    svc = PredictionService.__new__(PredictionService)     # 跳过 __init__（不建库、不联网）
    base = dict(prematch_enabled=True, prematch_strength=0.5, prematch_tilt=0.0,
                rating_strength=0.5, weather_strength=0.0)
    base.update(over)
    svc.settings = SimpleNamespace(**base)
    svc.api = api
    svc._weather = None
    return svc


def _xi(team_id, ids):
    return {"team": {"id": team_id}, "formation": "4-4-2",
            "startXI": [{"player": {"id": i, "pos": "M"}} for i in ids]}


def test_service_merges_rating_into_factors():
    """拿到首发 + 评分时，评分效应必须真的进入因子。"""
    ids = [str(i) for i in range(11)]
    lineups = [_xi(HOME, ids), _xi(AWAY, ids)]
    stats = [{"player": {"id": i},
              "statistics": [{"games": {"rating": "7.9", "minutes": 2000}}]} for i in ids]
    svc = _service(_FakeAPI(lineups=lineups, stats=stats))
    # 必须给一个临近开赛的时间：不在 45 分钟窗口内根本不会去取阵容（省额度）
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    got = run(svc._prematch_factors(1, soon, HOME, AWAY, city=None, season=2026))
    assert got is not None
    assert got["home"]["attack"] > 1.0
    assert got["home"]["concede"] < 1.0
    assert got["away"]["attack"] > 1.0
    assert "xi_rating" in got["detail"]["home"]


def test_service_no_lineups_means_no_rating_effect():
    """没有首发名单就不该凭空产生评分效应（评分只对『今晚谁上』有意义）。"""
    ids = [str(i) for i in range(11)]
    stats = [{"player": {"id": i},
              "statistics": [{"games": {"rating": "7.9", "minutes": 2000}}]} for i in ids]
    svc = _service(_FakeAPI(lineups=[], stats=stats))
    got = run(svc._prematch_factors(1, None, HOME, AWAY, city=None, season=2026))
    # 无伤停、无阵容、无天气 → 应视为不修正
    assert got is None


def test_service_injury_position_weighting_applied():
    """同样 1 人缺阵，缺前锋的负荷必须大于缺后卫。"""
    squad = [{"player": {"id": "p1", "pos": "F"}}, {"player": {"id": "p2", "pos": "D"}}]

    def load_with(pid):
        inj = [{"team": {"id": HOME}, "player": {"id": pid, "type": "Missing Fixture"}}]
        svc = _service(_FakeAPI(injuries=inj, squad=squad))
        got = run(svc._prematch_factors(1, None, HOME, AWAY, city=None, season=2026))
        return got["detail"]["home"]["absence_load"]

    assert load_with("p1") > load_with("p2")


def test_service_weather_disabled_never_contacts_network():
    """weather_strength=0 时不得发生任何天气请求（连城市都不查）。"""
    svc = _service(_FakeAPI(), weather_strength=0.0)
    got = run(svc._prematch_factors(1, None, HOME, AWAY, city="Manchester", season=2026))
    assert got is None
    assert svc._weather is None                  # 天气客户端根本没被创建


def test_service_rating_zero_keeps_injury_weighting():
    """RATING_STRENGTH=0 只关评分，不关伤停（两者是独立开关）。"""
    squad = [{"player": {"id": "p1", "pos": "F"}}]
    inj = [{"team": {"id": HOME}, "player": {"id": "p1", "type": "Missing Fixture"}}]
    svc = _service(_FakeAPI(injuries=inj, squad=squad), rating_strength=0.0)
    got = run(svc._prematch_factors(1, None, HOME, AWAY, city=None, season=2026))
    assert got is not None
    assert got["home"]["attack"] < 1.0            # 伤停仍在生效
    assert got["detail"]["home"]["xi_rating"] is None


def test_service_api_without_new_methods_still_works():
    """旧版 API 对象（无 get_squad / get_team_player_stats）不能被搞崩。"""
    class _LegacyAPI:
        async def get_injuries(self, fixture_id):
            return [{"team": {"id": HOME}, "player": {"id": "p1", "type": "Missing Fixture"}}]

        async def get_lineups(self, fixture_id):
            return []

    svc = _service(_LegacyAPI())
    got = run(svc._prematch_factors(1, None, HOME, AWAY, city=None, season=2026))
    assert got is not None
    assert got["home"]["attack"] < 1.0            # 退化为按人数计，仍然生效
