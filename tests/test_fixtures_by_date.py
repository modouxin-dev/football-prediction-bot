"""「今日赛程」按日拉取主路径的测试。

背景：原先逐个联赛轮询（17 个联赛 × 赛季降级 × 空结果重试）会把一次点击
放大到上百次请求，且国际比赛日欧洲联赛集体休战时整体空窗。
现在改为一次请求拿当天全部比赛，再按配置联赛筛选；
配置联赛当天无球时回退国家队/洲际赛事。
"""
from __future__ import annotations

import asyncio
from datetime import date

from config import load_settings
from service import PredictionService

SETTINGS = load_settings(
    {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555",
     "SEASON": "2026", "ADMIN_ID": "555", "LEAGUE_IDS": "39,140"}
)


def _fx(fid: str, league_id: int, league_name: str, country: str = "England") -> dict:
    return {
        "fixture": {"id": fid, "date": "2026-10-04T14:00:00+00:00",
                    "status": {"short": "NS"}},
        "league": {"id": league_id, "name": league_name, "season": 2026,
                   "round": "Regular", "country": country},
        "teams": {"home": {"id": "1", "name": "A"}, "away": {"id": "2", "name": "B"}},
        "goals": {"home": None, "away": None},
    }


class _FakeAPI:
    """只实现按日拉取，其余接口一律不可用（确保测试走的是新路径）。"""

    def __init__(self, by_day: dict[date, list[dict]]):
        self._by_day = by_day
        self.calls: list[date] = []

    async def get_fixtures_by_date(self, day):
        self.calls.append(day)
        return list(self._by_day.get(day, []))

    async def get_fixtures(self, *args, **kwargs):
        raise AssertionError("主路径不该回退到逐联赛轮询")


def _service(api) -> PredictionService:
    svc = PredictionService.__new__(PredictionService)
    svc.settings = SETTINGS
    svc.api = api
    svc.using_upcoming = False
    svc.using_national_fallback = False
    svc.fixture_day_label = ""
    svc.last_note = None
    svc.season_in_use = SETTINGS.season
    svc._season_by_league = {}
    return svc


# ---- 筛选策略 -----------------------------------------------------------------

def test_picks_configured_leagues_first():
    """配置联赛当天有球时，只返回配置联赛的比赛。"""
    day = date(2026, 10, 4)
    api = _FakeAPI({day: [
        _fx("1", 39, "Premier League"),
        _fx("2", 140, "La Liga"),
        _fx("3", 71, "Serie A Brasil"),  # 未配置，应被丢弃
    ]})
    got = asyncio.run(_service(api)._fetch_all_by_date(day))
    assert [f["fixture"]["id"] for f in got] == ["1", "2"]
    assert api.calls == [day]  # 一次请求拿全天


def test_falls_back_to_national_when_configured_leagues_idle():
    """配置联赛集体休战（国际比赛日）→ 回退国家队赛事，不再空白。"""
    day = date(2026, 10, 4)
    api = _FakeAPI({day: [
        _fx("1", 999, "World Cup - Qualification Europe", country="Europe"),
        _fx("2", 888, "UEFA Nations League", country="Europe"),
        _fx("3", 777, "Some Random Cup", country="Brazil"),  # 既非配置也非国家队
    ]})
    svc = _service(api)
    got = asyncio.run(svc._fetch_all_by_date(day))
    assert [f["fixture"]["id"] for f in got] == ["1", "2"]
    assert svc.using_national_fallback is True


def test_national_detection_by_name_when_country_missing():
    """country 字段缺失时，靠赛事名关键字也能认出国家队赛事。"""
    fx = _fx("1", 999, "Friendly International", country="")
    assert PredictionService._is_national(fx) is True


def test_club_league_is_not_mistaken_for_national():
    """俱乐部联赛不能被误判成国家队赛事（否则筛选会全乱）。"""
    assert PredictionService._is_national(_fx("1", 39, "Premier League", "England")) is False


def test_empty_day_returns_empty_without_touching_polling():
    day = date(2026, 10, 4)
    api = _FakeAPI({})
    got = asyncio.run(_service(api)._fetch_all_by_date(day))
    assert got == []
    assert api.calls == [day]


# ---- 请求量 -------------------------------------------------------------------

def test_single_request_per_day_replaces_polling():
    """17 个联赛的旧写法要发 17+ 次请求；新写法每天只发 1 次。"""
    day = date(2026, 10, 4)
    api = _FakeAPI({day: [_fx(str(i), 39, "Premier League") for i in range(20)]})
    asyncio.run(_service(api)._fetch_all_by_date(day))
    assert len(api.calls) == 1
