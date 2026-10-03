"""单场技术统计（射正数）取数链路的离线测试。

生产要用射正数建模，必须先确认两件事：端点在这个套餐下可用、
字段名能对上。这两点只有真实请求能证明，所以代码侧先把解析、
路由、落库三处各自钉死，等 /sotprobe 在真实环境确认后再开开关。

全部用例离线，不发起任何网络请求。
"""
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from api_client import (
    FootballAPI,
    SHOT_ON_TARGET_TYPES,
    parse_shots_on_target,
    stat_types_of,
)
from data_source import DataSourceRouter
from repository import PredictionRepository


# ---- 采样响应 ---------------------------------------------------------------

def _payload(home_sot, away_sot, *, home_id=33, away_id=34,
             shot_type="Shots on Goal"):
    return [
        {"team": {"id": home_id, "name": "Home"},
         "statistics": [{"type": "Shots off Goal", "value": 5},
                        {"type": shot_type, "value": home_sot}]},
        {"team": {"id": away_id, "name": "Away"},
         "statistics": [{"type": "Shots off Goal", "value": 9},
                        {"type": shot_type, "value": away_sot}]},
    ]


# ---- 解析（纯函数） ---------------------------------------------------------

def test_parses_home_and_away_by_team_id():
    got = parse_shots_on_target(_payload(7, 3), home_team_id=33, away_team_id=34)
    assert got == {"home": 7, "away": 3}


def test_team_id_match_ignores_response_order():
    """响应顺序不可靠：按 ID 匹配时，顺序颠倒也要得到正确归属。"""
    payload = list(reversed(_payload(7, 3)))
    got = parse_shots_on_target(payload, home_team_id=33, away_team_id=34)
    assert got == {"home": 7, "away": 3}


def test_alternative_field_name_is_accepted():
    """部分文档写作 Shots on Target，只认一个名字会静默失效。"""
    got = parse_shots_on_target(_payload(4, 2, shot_type="Shots on Target"),
                                home_team_id=33, away_team_id=34)
    assert got == {"home": 4, "away": 2}


def test_string_value_is_coerced():
    payload = _payload("7", "3")
    got = parse_shots_on_target(payload, home_team_id=33, away_team_id=34)
    assert got == {"home": 7, "away": 3}


def test_percentage_value_is_rejected_not_silently_used():
    """百分比是比例不是次数，当成计数会让强度量纲直接错掉。"""
    payload = _payload("57%", "43%")
    assert parse_shots_on_target(payload, home_team_id=33, away_team_id=34) is None


def test_missing_on_either_side_returns_none():
    """半份数据比没有更危险：一侧射正、一侧进球，量纲不一致。"""
    assert parse_shots_on_target(_payload(7, None), home_team_id=33,
                                away_team_id=34) is None


def test_single_team_response_returns_none():
    assert parse_shots_on_target(_payload(7, 3)[:1], home_team_id=33,
                                away_team_id=34) is None


def test_empty_response_returns_none():
    assert parse_shots_on_target([], home_team_id=33, away_team_id=34) is None
    assert parse_shots_on_target(None, home_team_id=33, away_team_id=34) is None


def test_zero_is_a_real_value_not_missing():
    assert parse_shots_on_target(_payload(0, 0), home_team_id=33,
                                away_team_id=34) == {"home": 0, "away": 0}


def test_falls_back_to_response_order_without_ids():
    got = parse_shots_on_target(_payload(6, 2))
    assert got == {"home": 6, "away": 2}


def test_stat_types_lists_available_field_names():
    types = stat_types_of(_payload(7, 3))
    assert "Shots off Goal" in types
    assert types.count("Shots off Goal") == 1  # 去重，便于人工看字段清单


def test_documented_shot_type_is_in_accepted_set():
    assert "Shots on Goal" in SHOT_ON_TARGET_TYPES
    assert "Shots on Target" in SHOT_ON_TARGET_TYPES


# ---- 客户端 -----------------------------------------------------------------

def make_api(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("retry_delay", 0)
    return FootballAPI("K", client=client, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_client_requests_statistics_endpoint_with_fixture_param():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"errors": [], "response": _payload(5, 1)})

    got = run(make_api(handler).get_fixture_statistics(1557417))
    assert seen["path"].endswith("/fixtures/statistics")
    assert seen["params"] == {"fixture": "1557417"}
    assert len(got) == 2


def test_client_propagates_error_for_upstream_degradation():
    """端点不支持时原样抛出，由上层决定降级（不该被当成故障）。"""

    def handler(request):
        return httpx.Response(403, json={"errors": {"plan": "not allowed"}})

    with pytest.raises(Exception):
        run(make_api(handler).get_fixture_statistics(1))


# ---- 路由 -------------------------------------------------------------------

class _Primary:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc
        self.calls = 0

    async def get_fixture_statistics(self, fixture_id):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class _NoStatsFallback:
    """备用源 football-data.org 没有单场技术统计能力。"""

    async def get_fixtures(self, *a, **k):
        return []


def test_router_returns_primary_payload():
    r = DataSourceRouter(_Primary(result=_payload(7, 3)), _NoStatsFallback())
    assert run(r.get_fixture_statistics(1))


def test_optional_failure_returns_empty_without_cooling_primary():
    """统计取不到是常态（套餐不含/该场未收录），绝不能把健康的主源拖进冷却。

    一旦冷却，赛程与积分榜这些关键请求都会被逼去走备用源。
    """
    r = DataSourceRouter(_Primary(exc=RuntimeError("403")), _NoStatsFallback())
    assert run(r.get_fixture_statistics(1)) == []
    assert not r._primary_cooling()
    assert r.last_switch is None


def test_fallback_without_the_method_does_not_raise():
    """备用源没实现该方法时，可选数据静默为空，不能报「两个源都不可用」。"""
    r = DataSourceRouter(_Primary(exc=RuntimeError("boom")), _NoStatsFallback())
    assert run(r.get_fixture_statistics(1)) == []


# ---- 落库 -------------------------------------------------------------------

def _repo(tmp_path):
    return PredictionRepository(str(tmp_path / "t.db"))


def test_new_db_has_stat_columns(tmp_path):
    repo = _repo(tmp_path)
    cols = {r["name"] for r in
            repo._connect().execute("PRAGMA table_info(matches)")}
    assert {"home_sot", "away_sot", "stats_fetched"} <= cols


def test_legacy_db_gets_columns_added(tmp_path):
    """旧库不会被 CREATE TABLE IF NOT EXISTS 补列，必须显式 ALTER。

    挂载卷上的库跨部署保留，漏了这一步就等于新字段永远写不进去。
    """
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE matches (id INTEGER PRIMARY KEY, competition_code TEXT NOT NULL, "
        "utc_date TEXT NOT NULL, home_team_id TEXT, away_team_id TEXT, "
        "home_score INTEGER, away_score INTEGER, source TEXT NOT NULL)")
    conn.commit()
    conn.close()

    repo = PredictionRepository(str(db))
    cols = {r["name"] for r in
            repo._connect().execute("PRAGMA table_info(matches)")}
    assert {"home_sot", "away_sot", "stats_fetched"} <= cols


def test_save_and_query_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    repo.save_matches("PL", [{
        "id": 1, "utcDate": "2026-10-01T19:00:00Z",
        "status": "FT", "matchday": 7,
        "homeTeam": {"id": 33, "name": "H"},
        "awayTeam": {"id": 34, "name": "A"},
        "score": {"fullTime": {"home": 2, "away": 1}},
        "season": {"startDate": "2026-08-01"},
    }])

    pending = repo.finished_without_stats("PL")
    assert [p["id"] for p in pending] == [1]

    assert repo.save_match_stats(1, 7, 3)
    assert repo.finished_without_stats("PL") == []  # 探测过就不再重复请求

    got = repo.latest_finished("PL", limit=1)[0]
    assert got["home_sot"] == 7 and got["away_sot"] == 3


def test_missing_stats_is_still_marked_as_probed(tmp_path):
    """探测不到也要置 stats_fetched：否则没有统计的比赛每天被重复拉取。"""
    repo = _repo(tmp_path)
    repo.save_matches("PL", [{
        "id": 2, "utcDate": "2026-10-01T19:00:00Z",
        "status": "FT", "matchday": 7,
        "homeTeam": {"id": 33, "name": "H"},
        "awayTeam": {"id": 34, "name": "A"},
        "score": {"fullTime": {"home": 0, "away": 0}},
        "season": {"startDate": "2026-08-01"},
    }])
    assert repo.save_match_stats(2, None, None)
    assert repo.finished_without_stats("PL") == []
    got = repo.latest_finished("PL", limit=1)[0]
    assert got["home_sot"] is None and got["home_score"] == 0
