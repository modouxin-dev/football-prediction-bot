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


# ---- 同类风险点：所有接受日期的公开接口都必须归一化 -------------------------
# 只修一个接口等于没修——下一个调用点还会再犯。这里把三个入口都锁住。


class _FakeFootballDataAPI:
    """备用源客户端的 fake：只重写网络层，记录查询参数。"""

    def __init__(self):
        self.seen: list[dict] = []

    async def _get(self, path, params=None, ttl=0.0, fresh=False):
        self.seen.append(dict(params or {}))
        return {"matches": []}


def _fd_client():
    """构造一个不发真实请求的备用源客户端。

    走**真实构造函数**，只替换最底层的 ``_request``（网络）。
    这样 ``_get`` 的缓存逻辑、``_code_for`` 的联赛校验都保持真实——
    测的是真实路径，不是替身路径。
    """
    from football_data import FootballDataAPI

    c = FootballDataAPI("fake-token-for-test")
    seen: list[dict] = []

    async def _fake_request(path, params=None):
        seen.append(dict(params or {}))
        return {"matches": []}

    # 赋给实例属性：调用点 ``self._request(path, params)`` 不会自动绑 self
    c._request = _fake_request
    c.seen = seen
    return c


def test_football_data_as_date_accepts_string():
    """备用源同样要能吃字符串——两处行为必须一致。"""
    from football_data import _as_date

    assert _as_date("2026-10-26") == date(2026, 10, 26)
    assert _as_date(datetime(2026, 10, 26, 23, 59)) == date(2026, 10, 26)


def test_football_data_get_fixtures_accepts_string():
    from football_data import FootballDataAPI

    c = _fd_client()
    asyncio.run(FootballDataAPI.get_fixtures(
        c, 39, 2026, "2026-10-26", "2026-10-28"))
    # 取 **第一次** 请求：fake 返回空 matches 时，get_fixtures 会回退到
    # 全局端点 /v4/matches 再发一次，seen[-1] 会是那次重试的参数。
    # 要验证的是「归一化后的日期有没有正确传出去」，故看首请求。
    assert c.seen[0]["dateFrom"] == "2026-10-26"
    assert c.seen[0]["dateTo"] == "2026-10-28"


def test_football_data_get_fixtures_rejects_bad_type():
    from football_data import FootballDataAPI

    c = _fd_client()
    with pytest.raises(ValueError):
        asyncio.run(FootballDataAPI.get_fixtures(
            c, 39, 2026, 20261026, "2026-10-28"))
    assert c.seen == [], "不该发出请求"


def test_api_client_get_fixtures_accepts_string(client):
    """主源带日期范围的接口同样要能吃字符串。"""
    asyncio.run(client.get_fixtures(39, 2026, "2026-10-26", "2026-10-28"))
    assert client.seen[-1]["from"] == "2026-10-26"
    assert client.seen[-1]["to"] == "2026-10-28"


def test_api_client_get_fixtures_datetime_has_no_time_part(client):
    asyncio.run(client.get_fixtures(
        39, 2026, datetime(2026, 10, 26, 23, 30), datetime(2026, 10, 28, 1, 2)))
    assert client.seen[-1]["from"] == "2026-10-26"
    assert client.seen[-1]["to"] == "2026-10-28"


def test_api_client_get_fixtures_rejects_bad_type(client):
    with pytest.raises(ValueError):
        asyncio.run(client.get_fixtures(39, 2026, None, "2026-10-28"))
    assert client.seen == [], "不该发出请求"
