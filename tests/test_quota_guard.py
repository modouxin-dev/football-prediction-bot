"""额度空转守卫：当日额度耗尽 / 429 时必须立刻停手，不得退避重试。

这些用例断言的是**真实发出的请求数**，不是日志文案——文案改了行为没改，
测试不该绿。

修复前的实测基线（见 PR 描述）：
    主源 429 +「request limit for the day」→ 真实请求 3 次，且熔断未启动
    主源 200 + errors.rateLimit(当日)      → 真实请求 3 次，且熔断未启动
    备用源 429（已判定整源冷却 60s）        → 真实请求 3 次
"""
import asyncio

import httpx
import pytest

import football_data
from api_client import APIError, FootballAPI
from football_data import FootballDataError, FootballDataAPI

# 生产环境实测返回的原文
LIMIT_DAY = "You have reached the request limit for the day."


def make_primary(handler, **kwargs):
    """主源客户端；handler 每次真实请求都会被调用。"""
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return handler(request, calls["n"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting))
    kwargs.setdefault("retry_delay", 0)
    return FootballAPI("K", client=client, base_url="https://t.test", **kwargs), calls


def make_fallback(handler, **kwargs):
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return handler(request, calls["n"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting))
    return FootballDataAPI("T", client=client, base_url="https://t.test", **kwargs), calls


def run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────── 主源：当日额度耗尽必须立刻停手


def test_primary_daily_quota_429_stops_after_one_request():
    """429 且文案写明按天结算：只发 1 次，不再退避重试。"""
    api, calls = make_primary(
        lambda r, n: httpx.Response(429, json={"errors": {"requests": LIMIT_DAY}}))

    with pytest.raises(APIError):
        run(api._request("/fixtures", {}))

    assert calls["n"] == 1, "额度已明确按天耗尽，重试必然失败，不应再发请求"


def test_primary_daily_quota_429_arms_the_breaker():
    """熔断必须被启动，否则后续调用点会各自重复烧请求。"""
    api, _ = make_primary(
        lambda r, n: httpx.Response(429, json={"errors": {"requests": LIMIT_DAY}}))

    with pytest.raises(APIError):
        run(api._request("/fixtures", {}))

    assert api.quota_exhausted is True, "无 remaining 头时熔断也必须启动"


def test_primary_rate_limit_error_payload_stops_after_one_request():
    """200 + errors.rateLimit 写明按天：同样只发 1 次并熔断。"""
    api, calls = make_primary(
        lambda r, n: httpx.Response(200, json={"errors": {"rateLimit": LIMIT_DAY}}))

    with pytest.raises(APIError):
        run(api._request("/fixtures", {}))

    assert calls["n"] == 1
    assert api.quota_exhausted is True


def test_primary_breaker_blocks_every_later_call():
    """熔断后连续多次调用：真实请求数不再增加（这是防空转的关键）。"""
    api, calls = make_primary(
        lambda r, n: httpx.Response(429, json={"errors": {"requests": LIMIT_DAY}}))

    for i in range(5):
        with pytest.raises(APIError):
            run(api._request("/fixtures", {"i": i}))

    assert calls["n"] == 1, f"熔断后 5 次调用应共发 1 次请求，实际 {calls['n']}"


# ─────────────────────────────────────────────── 主源：可恢复故障不得误伤


def test_primary_server_error_still_retries():
    """500 是临时故障，必须照旧重试——不能为了省额度砍掉正常重试。"""
    api, calls = make_primary(lambda r, n: httpx.Response(500, json={"errors": {}}),
                              retries=2)

    with pytest.raises(APIError):
        run(api._request("/fixtures", {}))

    assert calls["n"] == 3, "服务端临时故障应重试至上限"


def test_primary_transient_rate_limit_still_retries():
    """429 但文案是每分钟限流（不含按天措辞）：属可恢复，照旧重试。"""
    api, calls = make_primary(
        lambda r, n: httpx.Response(429, json={"errors": {"rateLimit": "Too many requests"}}),
        retries=2)

    with pytest.raises(APIError):
        run(api._request("/fixtures", {}))

    assert calls["n"] == 3, "临时限流不是当日额度耗尽，不应被熔断误伤"
    assert api.quota_exhausted is False


# ─────────────────────────────────────────────── 备用源：429 不得放大请求数


def test_fallback_429_stops_after_one_request():
    """已判定整源冷却，重试必然还是 429：只发 1 次。"""
    api, calls = make_fallback(
        lambda r, n: httpx.Response(429, json={"message": "API rate limit exceeded"}))

    with pytest.raises(FootballDataError):
        run(api._request("/matches", {}))

    assert calls["n"] == 1, "整源冷却已确认，不应再退避重试放大请求数"


def test_fallback_cooldown_blocks_all_later_requests():
    """一轮同步 10 个联赛：命中一次 429 后，其余 9 个应被冷却拦住。"""
    api, calls = make_fallback(
        lambda r, n: httpx.Response(429, json={"message": "API rate limit exceeded"}))

    for i in range(10):
        with pytest.raises(FootballDataError):
            run(api._request("/matches", {"i": i}))

    assert calls["n"] == 1, f"10 联赛一轮应共发 1 次请求，实际 {calls['n']}"


def test_fallback_non_429_still_retries():
    """网络类故障不是限流，必须照旧重试。"""
    def handler(r, n):
        raise httpx.ConnectError("boom")

    api, calls = make_fallback(handler)

    with pytest.raises(FootballDataError):
        run(api._request("/matches", {}))

    assert calls["n"] == football_data.MAX_RETRIES + 1, "网络错误应重试至上限"
