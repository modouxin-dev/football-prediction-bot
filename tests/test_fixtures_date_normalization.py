"""按日取赛程的日期归一化。

存在理由
--------
``get_fixtures_by_date`` 内部调 ``day.isoformat()``，而 ``str`` 没有这个方法。
传字符串进去会抛 AttributeError，而异常被内层的 ``except Exception``
吞成空列表——表现为「成功取数 N 天、补回 0 场」：全程无报错却毫无进展。

这个坑踩了两次（结算回填 PR #112、/oddsprobe），所以归一化收到接口入口，
并在此锁定：**任何调用方传 str / date / datetime 都必须能正常工作**。
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from api_client import FootballAPI, _as_date


class _FakeClient(FootballAPI):
    """只重写网络层，记录实际发出去的查询参数。"""

    def __init__(self):
        # 绕过 __init__ 的网络初始化
        self.seen: list[dict] = []

    async def _get(self, path, params, ttl=0.0, fresh=False):
        self.seen.append(dict(params))
        return []


@pytest.fixture
def client():
    return _FakeClient()


def test_as_date_accepts_str():
    assert _as_date("2026-10-26") == date(2026, 10, 26)


def test_as_date_accepts_date_and_datetime():
    """datetime 必须收敛成 date——否则 isoformat 会带时间部分。"""
    assert _as_date(date(2026, 10, 26)) == date(2026, 10, 26)
    assert _as_date(datetime(2026, 10, 26, 23, 59, 59)) == date(2026, 10, 26)


def test_as_date_rejects_garbage():
    """非法输入显式抛错，不能静默成「今天没比赛」。"""
    with pytest.raises(ValueError):
        _as_date(20261026)
    with pytest.raises(ValueError):
        _as_date("不是日期")
    with pytest.raises(ValueError):
        _as_date(None)


def test_get_fixtures_by_date_accepts_string(client):
    """字符串必须能直接用——这是踩过两次的那个坑。"""
    asyncio.run(client.get_fixtures_by_date("2026-10-26"))
    assert client.seen[-1]["date"] == "2026-10-26"


def test_get_fixtures_by_date_accepts_date_object(client):
    asyncio.run(client.get_fixtures_by_date(date(2026, 10, 26)))
    assert client.seen[-1]["date"] == "2026-10-26"


def test_get_fixtures_by_date_datetime_has_no_time_part(client):
    """datetime 不能把时间部分带进查询参数。"""
    asyncio.run(client.get_fixtures_by_date(datetime(2026, 10, 26, 23, 30)))
    assert client.seen[-1]["date"] == "2026-10-26"


def test_get_fixtures_by_date_rejects_bad_type(client):
    """非法类型直接抛，不被吞成空列表。"""
    with pytest.raises(ValueError):
        asyncio.run(client.get_fixtures_by_date(20261026))
    assert client.seen == [], "不该发出请求"
