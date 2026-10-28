"""预算闸门：服务端已声明当日额度耗尽后，不得再发出请求。

背景（真实缺陷）：
    api_client 会读取响应头 x-ratelimit-requests-remaining，但**只存不用**——
    它从未参与「要不要发这个请求」的判断。于是额度只能靠撞 429 /
    "request limit for the day" 才知道没了，此时已用计数已被推过套餐上限
    （线上 /status 实测 7558 / 7500）。

写法与 tests/test_api_client.py 保持一致：**同步测试函数 + asyncio.run()**。
本项目 pytest.ini 未开启 asyncio_mode=auto，不采用 @pytest.mark.asyncio。

本组用例断言的是**真实发出的请求数**，不是异常文案。
"""
import asyncio

import httpx
import pytest

from api_client import APIError, FootballAPI


def make_api(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("retry_delay", 0)
    return FootballAPI("SECRET-KEY", client=client, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_no_request_after_quota_zero():
    """服务端声明剩余 0 后，后续请求一次都不该发出。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200, json={"errors": [], "response": []},
            headers={"x-ratelimit-requests-remaining": "0"},
        )

    api = make_api(handler)
    run(api._request("fixtures", {}))      # 第 1 次：真实发出，拿到 remaining=0
    assert calls["n"] == 1

    with pytest.raises(APIError):
        run(api._request("fixtures", {}))
    with pytest.raises(APIError):
        run(api._request("fixtures", {}))

    assert calls["n"] == 1, f"额度耗尽后仍发出 {calls['n']} 次请求"


def test_429_does_not_retry_when_quota_exhausted():
    """429 把剩余刷成 0 后，本轮剩余重试必须放弃（1 次而非多次）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            429,
            json={"message": "You have reached the request limit for the day"},
            headers={"x-ratelimit-requests-remaining": "0"},
        )

    api = make_api(handler, retries=3)
    with pytest.raises(APIError):
        run(api._request("fixtures", {}))

    assert calls["n"] == 1, f"额度耗尽后仍重试，实际发出 {calls['n']} 次"


def test_positive_remaining_not_blocked():
    """剩余额度为正时不得误伤。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200, json={"errors": [], "response": []},
            headers={"x-ratelimit-requests-remaining": "500"},
        )

    api = make_api(handler, retries=0)
    run(api._request("fixtures", {}))
    run(api._request("fixtures", {}))
    run(api._request("fixtures", {}))

    assert calls["n"] == 3, f"有额度却被拦，实际只发出 {calls['n']} 次"


def test_missing_header_never_blocks():
    """服务端不返回该响应头时，闸门必须保持关闭（不能因缺头就拒绝服务）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"errors": [], "response": []})

    api = make_api(handler, retries=0)
    run(api._request("fixtures", {}))
    run(api._request("fixtures", {}))

    assert calls["n"] == 2
