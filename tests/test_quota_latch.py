"""额度熔断必须能跨日自动解除（回归测试）。

背景 / Why
----------
熔断曾是一个**单向锁存**：唯一能改写 `_quota_left` 的 `_track_quota`
只在 `_request` 真实发出请求后才执行，而熔断后请求在 `_request` 入口
即被拒发 —— 于是永远走不到更新，主源被永久停用。

表现：/status 长期显示「备用源生效中」，而数据源侧额度其实仍有剩余，
只能靠重启进程恢复。

本测试用 monkeypatch 控制「当前额度日」，断言跨日后熔断自动解除。
注意：这里断言的是**行为**（能否再次发起请求），不是源码文本。
"""
from __future__ import annotations

import pytest

import api_client
from api_client import FootballAPI


class _Resp:
    """最小响应替身，只提供 _track_quota 需要的 headers。"""

    def __init__(self, remaining: str) -> None:
        self.headers = {"x-ratelimit-requests-remaining": remaining}


@pytest.fixture
def today(monkeypatch):
    """把「当前额度日」变成可变量，便于模拟跨天。"""
    box = {"d": "2026-10-29"}
    monkeypatch.setattr(api_client, "_quota_day", lambda: box["d"])
    return box


def test_熔断当天保持生效(today):
    c = FootballAPI(api_key="k")
    c._mark_quota_exhausted("request limit for the day")
    assert c.quota_exhausted is True, "触发当日必须保持熔断"


def test_跨日后自动解除(today):
    c = FootballAPI(api_key="k")
    c._mark_quota_exhausted("request limit for the day")
    assert c.quota_exhausted is True
    today["d"] = "2026-10-30"          # 跨天
    assert c.quota_exhausted is False, "跨天后应自动解除，不应永久停用主源"
    assert c._quota_left is None, "解除后应回到『未知』态，由下次响应头重新判定"


def test_响应头返回0形成的隐蔽死锁也能跨日解除(today):
    """不走 _mark_quota_exhausted、仅由响应头把余额判成 0 的路径。"""
    c = FootballAPI(api_key="k")
    c._track_quota(_Resp("0"))
    assert c.quota_exhausted is True
    today["d"] = "2026-10-30"
    assert c.quota_exhausted is False, "响应头归零同样必须能跨日解除"


def test_额度恢复时立即清除熔断(today):
    c = FootballAPI(api_key="k")
    c._track_quota(_Resp("0"))
    assert c.quota_exhausted is True
    c._track_quota(_Resp("5000"))
    assert c.quota_exhausted is False, "数据源侧额度恢复后不应继续熔断"


def test_未熔断实例不受影响(today):
    """防误伤守卫：从未熔断的实例不得被误判为耗尽。"""
    assert FootballAPI(api_key="k").quota_exhausted is False
