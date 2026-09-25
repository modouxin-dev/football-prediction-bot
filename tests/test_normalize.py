"""P0-2：数据源统一契约（两个源必须产出同构数据）。"""
import httpx

import normalize
from football_data import FootballDataAPI
from tests.sample_data import fixture

FD_PREFIX = normalize.FALLBACK_PREFIX


def _fd_client(handler):
    return FootballDataAPI("tok", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ---- 契约校验 ---------------------------------------------------------------
def test_contract_accepts_api_football_shape():
    """主源原生结构本身就是契约基准，必须校验通过。"""
    fx = fixture(1001, 1, "曼城", 2, "利物浦", __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))
    fx["teams"]["home"]["id"] = str(fx["teams"]["home"]["id"])  # 契约要求 ID 为字符串
    fx["teams"]["away"]["id"] = str(fx["teams"]["away"]["id"])
    fx["fixture"]["id"] = str(fx["fixture"]["id"])
    assert normalize.validate_fixture(fx) == []


def test_contract_rejects_non_dict():
    assert normalize.validate_fixture("not a fixture")


def test_contract_requires_string_ids():
    """ID 必须统一为字符串（备用源用 fd- 前缀，主源用纯数字字符串）。"""
    fx = {
        "fixture": {"id": 123, "date": "2026-09-25T10:00:00Z", "status": {"short": "NS"}},
        "league": {"id": 39, "name": "PL", "season": 2026},
        "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": "2", "name": "B"}},
    }
    problems = normalize.validate_fixture(fx)
    assert any("fixture.id" in p for p in problems)
    assert any("teams.home.id" in p for p in problems)


def test_contract_detects_unknown_status():
    fx = {
        "fixture": {"id": "123", "date": "2026-09-25T10:00:00Z", "status": {"short": "WHATEVER"}},
        "league": {"id": 39, "name": "PL", "season": 2026},
        "teams": {"home": {"id": "1", "name": "A"}, "away": {"id": "2", "name": "B"}},
    }
    assert any("status.short" in p for p in normalize.validate_fixture(fx))


# ---- 备用源转换 -------------------------------------------------------------
def _fd_match_payload():
    return {"matches": [{
        "id": 6001,
        "utcDate": "2026-09-25T18:00:00Z",
        "status": "SCHEDULED",
        "matchday": 7,
        "venue": "Etihad",
        "competition": {"id": 2021, "code": "PL", "name": "Premier League"},
        "homeTeam": {"id": 65, "name": "Manchester City FC"},
        "awayTeam": {"id": 64, "name": "Liverpool FC"},
        "score": {"fullTime": {"home": None, "away": None}},
    }]}


def test_fallback_output_matches_primary_shape():
    """备用源转换后的结构必须与主源同构（键结构一致）。"""
    import asyncio
    from datetime import datetime, timezone

    fd = _fd_client(lambda r: httpx.Response(200, json=_fd_match_payload()))
    converted = asyncio.run(fd.get_fixtures(39, 2026, __import__("datetime").date(2026, 9, 25),
                                           __import__("datetime").date(2026, 9, 25)))
    assert converted, "备用源应有数据"

    primary = fixture(1001, 1, "曼城", 2, "利物浦", datetime.now(timezone.utc))
    # 对齐契约：ID 统一字符串；补齐比分与赛季字段（真实主源响应本就包含）
    primary["teams"]["home"]["id"] = "1"
    primary["teams"]["away"]["id"] = "2"
    primary["fixture"]["id"] = "1001"
    primary.setdefault("goals", {"home": None, "away": None})
    primary["league"].setdefault("season", 2026)
    assert normalize.same_shape(primary, converted[0])


def test_fallback_ids_are_prefixed():
    """备用源 ID 必须带 fd- 前缀，避免与主源数字 ID 撞车。"""
    import asyncio
    from datetime import date

    fd = _fd_client(lambda r: httpx.Response(200, json=_fd_match_payload()))
    fx = asyncio.run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))[0]
    assert fx["fixture"]["id"].startswith(FD_PREFIX)
    assert fx["teams"]["home"]["id"].startswith(FD_PREFIX)
    assert normalize.is_fallback_id(fx["teams"]["away"]["id"])
    assert normalize.validate_fixture(fx) == []


def test_status_is_mapped_to_primary_enum():
    """状态字段必须统一成主源的 status.short 枚举。"""
    import asyncio
    from datetime import date

    for fd_status, expected in [("SCHEDULED", "NS"), ("FINISHED", "FT"),
                                ("IN_PLAY", "LIVE"), ("POSTPONED", "PST")]:
        payload = _fd_match_payload()
        payload["matches"][0]["status"] = fd_status
        fd = _fd_client(lambda r, p=payload: httpx.Response(200, json=p))
        fx = asyncio.run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))[0]
        assert fx["fixture"]["status"]["short"] == expected, fd_status


def test_time_is_iso8601_utc():
    """时间字段统一为 ISO-8601 UTC 字符串，展示层再按时区换算。"""
    import asyncio
    from datetime import date

    fd = _fd_client(lambda r: httpx.Response(200, json=_fd_match_payload()))
    fx = asyncio.run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))[0]
    assert fx["fixture"]["date"] == "2026-09-25T18:00:00Z"
