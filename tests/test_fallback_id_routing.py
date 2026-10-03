"""备用源比赛 ID 的路由守卫（全部 Mock，不需要真实 Token）。

背景（线上真实事故）：
主源冷却期间用备用源拉赛程，比赛 ID 是 "fd-5001" 这类字符串。
随后查赔率时把这个字符串传给主源，主源直接拒绝：
    "The Fixture field must contain an integer."
主源因此被判定为故障 → 进入 10 分钟冷却 → 更依赖备用源 →
产出更多 fd- ID → 主源继续被污染，形成自我维持的降级闭环。

修复：按 ID 归属路由。带备用源前缀的 ID 只走备用源，绝不问主源。
"""
import asyncio

import pytest

from data_source import DataSourceError, DataSourceRouter

FALLBACK_FIXTURE_ID = "fd-5001"
PRIMARY_FIXTURE_ID = 1234567


def run(coro):
    return asyncio.run(coro)


class RecordingPrimary:
    """主源桩：记录是否被调用，以及收到的 ID。"""

    def __init__(self, odds=None):
        self.odds = odds if odds is not None else [{"book": "primary"}]
        self.odds_calls = []
        self.injury_calls = []

    async def get_odds(self, fixture_id, fresh=False):
        self.odds_calls.append(fixture_id)
        return self.odds

    async def get_injuries(self, fixture_id):
        self.injury_calls.append(fixture_id)
        return [{"player": "primary"}]

    async def get_fixtures(self, *a, **k):
        return [{"fixture": {"id": PRIMARY_FIXTURE_ID}}]


class RecordingFallback:
    """备用源桩：同样记录调用。"""

    def __init__(self, odds=None):
        self.odds = odds if odds is not None else [{"book": "fallback"}]
        self.odds_calls = []
        self.injury_calls = []

    async def get_odds(self, fixture_id, fresh=False):
        self.odds_calls.append(fixture_id)
        return self.odds

    async def get_injuries(self, fixture_id):
        self.injury_calls.append(fixture_id)
        return [{"player": "fallback"}]

    async def get_fixtures(self, *a, **k):
        return [{"fixture": {"id": FALLBACK_FIXTURE_ID}}]


def make_router(primary=None, fallback=None):
    primary = primary or RecordingPrimary()
    fallback = fallback or RecordingFallback()
    return DataSourceRouter(primary, fallback, mode="auto"), primary, fallback


# ---- 核心守卫 ----------------------------------------------------------------


def test_fallback_id_never_sent_to_primary():
    """备用源 ID 查赔率时，主源一次都不能被调用。"""
    router, primary, fallback = make_router()
    result = run(router.get_odds(FALLBACK_FIXTURE_ID))

    assert primary.odds_calls == [], "主源不应收到备用源 ID"
    assert fallback.odds_calls == [FALLBACK_FIXTURE_ID]
    assert result == [{"book": "fallback"}]


def test_fallback_id_never_marks_primary_down():
    """关键：备用源 ID 不得把健康的主源拖进冷却。"""
    router, primary, fallback = make_router()
    run(router.get_odds(FALLBACK_FIXTURE_ID))

    assert router.last_error("api-football") is None
    assert not router._primary_cooling(), "主源不应因备用源 ID 进入冷却"


def test_injuries_also_routed_by_id():
    """伤停同样是按比赛 ID 查询，受同一守卫保护。"""
    router, primary, fallback = make_router()
    result = run(router.get_injuries(FALLBACK_FIXTURE_ID))

    assert primary.injury_calls == []
    assert fallback.injury_calls == [FALLBACK_FIXTURE_ID]
    assert result == [{"player": "fallback"}]


def test_primary_id_still_goes_to_primary():
    """回归保护：数字 ID（主源比赛）仍必须走主源，不能被误伤。"""
    router, primary, fallback = make_router()
    result = run(router.get_odds(PRIMARY_FIXTURE_ID))

    assert primary.odds_calls == [PRIMARY_FIXTURE_ID]
    assert fallback.odds_calls == []
    assert result == [{"book": "primary"}]


def test_primary_id_falls_back_when_empty():
    """主源 ID 但主源无数据 → 仍应回落备用源（原有行为不变）。"""
    router, primary, fallback = make_router(primary=RecordingPrimary(odds=[]))
    result = run(router.get_odds(PRIMARY_FIXTURE_ID))

    assert primary.odds_calls == [PRIMARY_FIXTURE_ID]
    assert fallback.odds_calls == [PRIMARY_FIXTURE_ID]
    assert result == [{"book": "fallback"}]


# ---- 边界：备用源未配置 --------------------------------------------------------


def test_fallback_id_without_fallback_returns_empty():
    """拿着备用源 ID 却没有备用源：可选数据静默返回空，不中断预测。"""
    router = DataSourceRouter(RecordingPrimary(), None, mode="auto")
    assert run(router.get_odds(FALLBACK_FIXTURE_ID)) == []
    assert run(router.get_injuries(FALLBACK_FIXTURE_ID)) == []


def test_primary_source_not_polluted_after_fallback_id():
    """备用源 ID 用过后，主源仍可被正常调用（闭环不再自我维持）。"""
    router, primary, fallback = make_router()
    run(router.get_odds(FALLBACK_FIXTURE_ID))
    # 紧接着用主源 ID 查询，必须仍然走主源
    result = run(router.get_odds(PRIMARY_FIXTURE_ID))

    assert primary.odds_calls == [PRIMARY_FIXTURE_ID]
    assert result == [{"book": "primary"}]
    assert router.source == "api-football"
