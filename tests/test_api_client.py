import asyncio
from datetime import date

import httpx
import pytest

from api_client import APIError, FootballAPI


def make_api(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("retry_delay", 0)
    return FootballAPI("SECRET-KEY", client=client, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_success_returns_response_and_sends_rapidapi_headers():
    seen = {}

    def handler(request):
        seen["headers"], seen["url"] = request.headers, str(request.url)
        return httpx.Response(200, json={"errors": [], "response": [{"ok": 1}]}, headers={"x-ratelimit-requests-remaining": "42"})

    api = make_api(handler)
    assert run(api.get_odds(7)) == [{"ok": 1}]
    assert seen["headers"]["x-rapidapi-key"] == "SECRET-KEY"
    assert seen["headers"]["x-rapidapi-host"] == "api-football-v1.p.rapidapi.com"
    assert "SECRET-KEY" not in seen["url"]  # 密钥只放在请求头里
    assert api.quota_remaining == "42"


def test_direct_api_sports_provider_uses_its_own_host_and_header():
    seen = {}

    def handler(request):
        seen["host"], seen["key"] = request.url.host, request.headers.get("x-apisports-key")
        return httpx.Response(200, json={"errors": [], "response": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    run(FootballAPI("K", provider="apisports", client=client).get_odds(1))
    assert seen == {"host": "v3.football.api-sports.io", "key": "K"}


def test_fixtures_request_uses_date_range_not_invalid_status_value():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"errors": [], "response": []})

    run(make_api(handler).get_fixtures(39, 2026, date(2026, 9, 24), date(2026, 9, 26)))
    assert seen["params"] == {"league": "39", "season": "2026", "from": "2026-09-24", "to": "2026-09-26"}


def test_http_403_is_translated_into_a_readable_reason_and_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, json={"message": "You are not subscribed to this API."})

    with pytest.raises(APIError) as exc:
        run(make_api(handler).get_odds(1))
    assert "403" in str(exc.value) and "未订阅" in str(exc.value) and "You are not subscribed" in str(exc.value)
    assert len(calls) == 1


def test_errors_field_becomes_api_error_and_plan_hint_is_added():
    def handler(request):
        return httpx.Response(200, json={"errors": {"plan": "Free plans do not have access to this season"}, "response": []})

    with pytest.raises(APIError) as exc:
        run(make_api(handler).get_standings(39, 2026))
    assert "Free plans" in str(exc.value) and "套餐" in str(exc.value)


def test_server_errors_and_429_are_retried_then_succeed():
    statuses = iter([500, 429, 200])

    def handler(request):
        code = next(statuses)
        return httpx.Response(code, json={"errors": [], "response": [1]} if code == 200 else {})

    assert run(make_api(handler).get_odds(1)) == [1]


def test_gives_up_after_retries_with_last_error():
    def handler(request):
        return httpx.Response(429, json={"message": "Too many requests"})

    with pytest.raises(APIError, match="429"):
        run(make_api(handler, retries=2).get_odds(1))


def test_network_errors_are_retried_and_reported_without_leaking_details():
    def handler(request):
        raise httpx.ConnectTimeout("boom")

    with pytest.raises(APIError, match="ConnectTimeout"):
        run(make_api(handler, retries=1).get_odds(1))


def test_per_minute_rate_limit_in_errors_field_is_retried():
    payloads = iter([{"errors": {"rateLimit": "Too many requests"}, "response": []}, {"errors": [], "response": ["ok"]}])

    def handler(request):
        return httpx.Response(200, json=next(payloads))

    assert run(make_api(handler).get_odds(1)) == ["ok"]


def test_cache_hit_skips_network_and_fresh_bypasses_it():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"errors": [], "response": [len(calls)]})

    api = make_api(handler)
    assert run(api.get_odds(1)) == [1] and run(api.get_odds(1)) == [1]
    assert len(calls) == 1
    assert run(api.get_odds(1, fresh=True)) == [2]
    assert run(api.get_odds(2)) == [3]  # 不同参数不共用缓存


def test_standings_are_flattened_across_groups():
    payload = {"errors": [], "response": [{"league": {"standings": [[{"team": {"id": 1}}], [{"team": {"id": 2}}]]}}]}
    api = make_api(lambda request: httpx.Response(200, json=payload))
    assert [r["team"]["id"] for r in run(api.get_standings(39, 2026))] == [1, 2]


def test_non_json_body_is_reported():
    with pytest.raises(APIError, match="JSON"):
        run(make_api(lambda request: httpx.Response(200, text="<html>")).get_odds(1))


def test_account_status_returns_object():
    body = {"errors": [], "response": {"subscription": {"plan": "Free"}, "requests": {"current": 3, "limit_day": 100}}}
    api = make_api(lambda request: httpx.Response(200, json=body))
    assert run(api.get_account_status())["requests"]["limit_day"] == 100
