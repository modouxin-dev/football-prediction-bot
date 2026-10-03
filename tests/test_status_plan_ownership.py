"""/status 套餐显示必须归属主源账号（全部 Mock）。

背景（线上真实误导）：
主源冷却期间 get_account_status() 走备用源，返回的是备用源的 Free 档，
于是 /status 显示「套餐 Free（有效）」，让人误以为付费订阅失效。
套餐是主源账号的属性，与当前生效哪个源无关。
"""
import asyncio

import pytest

from commands.admin import _account_status_preferring_primary

PRO_ACCOUNT = {
    "subscription": {"plan": "Pro", "active": True},
    "requests": {"current": 10, "limit_day": 7500},
}
FREE_ACCOUNT = {
    "subscription": {"plan": "Free", "active": True},
    "requests": {"current": 3, "limit_day": 100},
}


def run(coro):
    return asyncio.run(coro)


class Stub:
    """桩：返回预设账号，或按配置抛错。"""

    def __init__(self, account=None, error=None):
        self.account = account
        self.error = error
        self.calls = 0

    async def get_account_status(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.account


class RouterStub:
    """模拟 DataSourceRouter：带 primary 属性，自身走常规路由。"""

    def __init__(self, primary, own=None):
        self.primary = primary
        self.own = own if own is not None else FREE_ACCOUNT
        self.calls = 0

    async def get_account_status(self):
        self.calls += 1
        return self.own


def test_plan_comes_from_primary_not_fallback():
    """核心：主源是 Pro 时，即便常规路由会返回 Free，也必须显示 Pro。"""
    primary = Stub(PRO_ACCOUNT)
    router = RouterStub(primary, own=FREE_ACCOUNT)

    result = run(_account_status_preferring_primary(router))

    assert result == PRO_ACCOUNT
    assert result["subscription"]["plan"] == "Pro"
    assert router.calls == 0, "不应回落到常规路由（那会读到备用源的 Free）"


def test_falls_back_when_primary_raises():
    """主源读不到时回落到常规路由，不能让状态页崩掉。"""
    primary = Stub(error=RuntimeError("主源不可用"))
    router = RouterStub(primary, own=FREE_ACCOUNT)

    result = run(_account_status_preferring_primary(router))

    assert result == FREE_ACCOUNT
    assert router.calls == 1


def test_falls_back_when_primary_empty():
    """主源返回空 dict 视为没读到，同样回落到常规路由。"""
    primary = Stub(account={})
    router = RouterStub(primary, own=FREE_ACCOUNT)

    result = run(_account_status_preferring_primary(router))

    assert result == FREE_ACCOUNT
    assert router.calls == 1


def test_plain_api_without_primary():
    """没有 primary 属性（非路由器）时，行为与改动前完全一致。"""
    plain = Stub(PRO_ACCOUNT)
    assert run(_account_status_preferring_primary(plain)) == PRO_ACCOUNT
