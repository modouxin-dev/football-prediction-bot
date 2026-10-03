"""可选数据失败不得触发主源冷却（P1-美化步骤 3）

背景（实测，非推测）：未开盘的比赛没有赔率，主源会直接拒绝，实测 164ms
快速失败。旧代码不区分方法，一律 _mark_primary_down，于是赔率缺失把健康的
主源拖进 10 分钟冷却；冷却期内连赛程、积分榜这些关键请求都被迫走备用源，
表现为 /status 突然显示「备用源生效中」+「套餐 Free」，而主源其实是好的。

修复：只有关键方法失败才进冷却。可选方法失败只记录错误，不影响后续调用。
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx

from api_client import APIError
from data_source import OPTIONAL_METHODS, DataSourceRouter
from football_data import FootballDataAPI

FAKE_TOKEN = "fd-secret-token-should-never-leak"
DAY = date(2026, 9, 25)


def run(coro):
    return asyncio.run(coro)


class StubPrimary:
    """主源桩：只对指定方法抛错，其余返回正常数据。"""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    async def _maybe(self, name, ok_value):
        self.calls.append(name)
        if name in self.fail:
            raise APIError(f"{name} 失败（模拟未开盘/无数据）")
        return ok_value

    async def get_fixtures(self, *a, **k):
        return await self._maybe("get_fixtures", [{"fixture": {"id": 101}}])

    async def get_standings(self, *a, **k):
        return await self._maybe("get_standings", [{"team": {"id": 1}}])

    async def get_odds(self, *a, **k):
        return await self._maybe("get_odds", [])

    async def get_h2h(self, *a, **k):
        return await self._maybe("get_h2h", [])

    async def get_team_form(self, *a, **k):
        return await self._maybe("get_team_form", [])

    async def get_injuries(self, *a, **k):
        return await self._maybe("get_injuries", [])

    async def get_available_seasons(self):
        return await self._maybe("get_available_seasons", [2026])

    async def get_account_status(self):
        return await self._maybe("get_account_status", {"plan": "Pro"})

    async def aclose(self):
        pass


class StubFallback:
    """备用源桩：任何方法都返回空列表，够用于验证「切换」有无发生。"""

    def __init__(self):
        self.calls = []

    async def _empty(self, name, *a, **k):
        self.calls.append(name)
        return []

    get_fixtures = lambda self, *a, **k: self._empty("get_fixtures")  # noqa: E731
    get_odds = lambda self, *a, **k: self._empty("get_odds")  # noqa: E731
    get_standings = lambda self, *a, **k: self._empty("get_standings")  # noqa: E731
    get_h2h = lambda self, *a, **k: self._empty("get_h2h")  # noqa: E731
    get_team_form = lambda self, *a, **k: self._empty("get_team_form")  # noqa: E731
    get_injuries = lambda self, *a, **k: self._empty("get_injuries")  # noqa: E731

    async def get_account_status(self):
        return {}

    async def aclose(self):
        pass


def test_optional_methods_set_is_pinned():
    """可选方法集合必须包含赔率——它是最容易因「未开盘」而失败的。"""
    assert "get_odds" in OPTIONAL_METHODS
    assert "get_fixtures" not in OPTIONAL_METHODS  # 关键方法，不能当可选


def test_odds_failure_does_not_cooldown_primary():
    """赔率失败（未开盘是常态）不得冷却主源。

    这是本次修复的核心：冷却会让后续赛程请求也被迫走备用源。
    """
    primary = StubPrimary(fail={"get_odds"})
    router = DataSourceRouter(primary, StubFallback(), mode="auto")

    run(router.get_odds(1001))

    assert not router._primary_cooling(), "赔率失败不应把主源拖进冷却"


def test_key_data_still_uses_primary_after_odds_failure():
    """赔率失败后，赛程请求仍应走主源（这才是「主源健康」的真实证据）。"""
    primary = StubPrimary(fail={"get_odds"})
    router = DataSourceRouter(primary, StubFallback(), mode="auto")

    run(router.get_odds(1001))
    result = run(router.get_fixtures(39, 2026, DAY, DAY))

    assert router.source == "api-football", "赛程仍应走主源"
    assert result == [{"fixture": {"id": 101}}]


def test_critical_method_failure_still_cooldowns():
    """对照组：关键方法（赛程）失败必须照常冷却，否则主源真挂了没人知道。"""
    primary = StubPrimary(fail={"get_fixtures"})
    router = DataSourceRouter(primary, StubFallback(), mode="auto")

    run(router.get_fixtures(39, 2026, DAY, DAY))

    assert router._primary_cooling(), "关键方法失败必须进冷却"


def test_optional_failure_does_not_record_switch():
    """没真正切走就不记 last_switch——否则排查时会指向一次并未发生的切换。"""
    primary = StubPrimary(fail={"get_odds"})
    router = DataSourceRouter(primary, StubFallback(), mode="auto")

    assert router.last_switch is None
    run(router.get_odds(1001))

    assert router.last_switch is None, "未进冷却不算降级，不该写 last_switch"


def test_critical_failure_records_switch():
    """对照组：关键方法失败要留下降级事件，供 /diag 排查。"""
    primary = StubPrimary(fail={"get_fixtures"})
    router = DataSourceRouter(primary, StubFallback(), mode="auto")

    run(router.get_fixtures(39, 2026, DAY, DAY))

    assert router.last_switch is not None
    assert router.last_switch["from"] == "api-football"
    assert router.last_switch["method"] == "get_fixtures"


def test_optional_failure_still_records_error_for_diag():
    """不冷却不等于不记录：/diag 要能看到赔率失败过，否则无从排查。"""
    primary = StubPrimary(fail={"get_odds"})
    router = DataSourceRouter(primary, StubFallback(), mode="auto")

    run(router.get_odds(1001))

    assert "api-football" in router._last_errors
    assert not router._primary_cooling()


def test_success_clears_error():
    """成功一次就该清掉旧错误，避免 /diag 一直显示历史失败。"""
    primary = StubPrimary(fail={"get_odds"})
    router = DataSourceRouter(primary, StubFallback(), mode="auto")

    run(router.get_odds(1001))
    assert "api-football" in router._last_errors

    primary.fail.clear()  # 主源恢复
    run(router.get_fixtures(39, 2026, DAY, DAY))

    assert "api-football" not in router._last_errors
