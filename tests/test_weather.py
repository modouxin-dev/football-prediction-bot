"""天气旁路（Open-Meteo）的单元测试。

设计原则 / Design principle
---------------------------
天气是**旁路信号**：任何失败都必须静默返回 None，绝不能影响预测。
因此这里大量使用假客户端，而不是真的去打网络（Open-Meteo 免费，但测试
不该依赖外网，也不该把共享 IP 的额度用在 CI 上）。

    Weather is a **side channel**: every failure must return None silently and
    never affect a prediction. So these tests use fake clients rather than the
    real network (Open-Meteo is free, but CI must not depend on external
    connectivity or spend a shared IP's quota).
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import pytest

import weather as weather_mod
from weather import (FORECAST_TTL, GEOCODE_TTL, WeatherClient,
                     WeatherSample, _TTLCache, _hour_of)


class _FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload if self._payload is not None else {}


class _FakeClient:
    """按 URL 关键字返回预设响应。"""

    def __init__(self, geo=None, fc=None, status=200):
        self.geo = geo
        self.fc = fc
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, **_):
        self.calls.append((url, dict(params or {})))
        if "geocoding" in url:
            return _FakeResp(self.geo, self.status)
        return _FakeResp(self.fc, self.status)

    async def aclose(self):
        pass


def run(coro):
    """在**独立**的新事件循环里跑协程。

    不能用 get_event_loop()：其他测试（pytest-asyncio 相关）可能已关闭或
    占用了默认循环，会让本文件单独跑通过、全量跑却失败。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client(geo=None, fc=None, status=200, enabled=True) -> WeatherClient:
    c = WeatherClient(enabled=enabled)
    c._client = _FakeClient(geo, fc, status)
    return c


# ---- 缓存 ------------------------------------------------------------------

def test_ttl_cache_expires_and_evicts():
    now = 1000.0
    c = _TTLCache(10.0, 4)
    c.set("a", 1, now)
    assert c.get("a", now) == 1
    assert c.get("a", now + 11) is None          # 过期
    for i in range(10):
        c.set(f"k{i}", i, now + i)               # 超过容量上限
    assert len(c._data) <= 4


def test_negative_geocode_result_is_cached_too():
    """查不到的城市也必须缓存，否则会被反复查询（浪费共享额度）。"""
    now = 0.0
    c = _TTLCache(GEOCODE_TTL, 10)
    c.set("Nowhere", None, now)
    assert c.get("Nowhere", now + 60) is None
    assert "Nowhere" in c._data                  # 负结果确实落了缓存


# ---- 地理编码 --------------------------------------------------------------

def test_coords_for_city_success():
    c = _client(geo={"results": [{"latitude": 53.48, "longitude": -2.24}]})
    assert run(c.coords_for_city("Manchester")) == (53.48, -2.24)


def test_coords_for_city_empty_and_http_error():
    assert run(
        _client(geo={}).coords_for_city("Manchester")) is None
    assert run(
        _client(geo={}, status=500).coords_for_city("Manchester")) is None


def test_coords_for_city_blank_input():
    c = _client(geo={"results": [{"latitude": 1.0, "longitude": 2.0}]})
    assert run(c.coords_for_city("")) is None
    assert run(c.coords_for_city("   ")) is None


def test_coords_for_city_disabled_client_never_calls():
    c = _client(geo={"results": [{"latitude": 1.0, "longitude": 2.0}]}, enabled=False)
    assert run(c.coords_for_city("Manchester")) is None
    assert c._client.calls == []                 # 完全没有发请求


def test_coords_for_city_bad_payload():
    """经纬度不是数字时必须当作失败，不能返回 (None, None)。"""
    c = _client(geo={"results": [{"latitude": "x", "longitude": None}]})
    assert run(c.coords_for_city("X")) is None


# ---- 天气取数 --------------------------------------------------------------

def _hourly(day="2026-10-14"):
    return {
        "time": [f"{day}T{h:02d}:00" for h in range(24)],
        "temperature_2m": [10.0 + i for i in range(24)],
        "precipitation": [0.0] * 24,
        "wind_speed_10m": [5.0] * 24,
        "relative_humidity_2m": [60.0] * 24,
    }


def test_at_kickoff_picks_matching_hour():
    c = _client(fc={"hourly": _hourly()})
    when = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    got = run(c.at_kickoff(53.48, -2.24, when))
    assert got is not None
    assert math.isclose(got.temp_c, 25.0)        # 10 + 15


def test_at_kickoff_naive_datetime_treated_as_utc():
    c = _client(fc={"hourly": _hourly()})
    when = datetime(2026, 10, 14, 3, 0)          # 无时区
    got = run(c.at_kickoff(53.48, -2.24, when))
    assert math.isclose(got.temp_c, 13.0)


def test_at_kickoff_missing_hour_falls_back_to_nearest():
    partial = _hourly()
    partial["time"] = [f"2026-10-14T{h:02d}:00" for h in (0, 12)]
    partial["temperature_2m"] = [4.0, 16.0]
    c = _client(fc={"hourly": partial})
    when = datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)
    got = run(c.at_kickoff(53.48, -2.24, when))
    assert got is not None                       # 取最近的一条，而不是放弃


def test_at_kickoff_http_error_returns_none():
    c = _client(fc={}, status=500)
    got = run(
        c.at_kickoff(53.48, -2.24, datetime(2026, 10, 14, 15, tzinfo=timezone.utc)))
    assert got is None


def test_for_fixture_end_to_end():
    c = _client(geo={"results": [{"latitude": 53.48, "longitude": -2.24}]},
                fc={"hourly": _hourly()})
    got = run(
        c.for_fixture("Manchester", datetime(2026, 10, 14, 15, tzinfo=timezone.utc)))
    assert isinstance(got, WeatherSample)
    assert math.isclose(got.temp_c, 25.0)


def test_for_fixture_no_city_returns_none():
    c = _client(geo={"results": [{"latitude": 1.0, "longitude": 1.0}]})
    assert run(c.for_fixture(None, None)) is None
    assert run(c.for_fixture("  ", None)) is None


def test_sample_as_dict_and_repr():
    s = WeatherSample(12.0, 0.5, 20.0, 70.0)
    assert s.as_dict()["temp_c"] == 12.0
    assert "WeatherSample" in repr(s)


def test_hour_of_parsing():
    assert _hour_of("2026-10-14T15:00") == 15
    assert _hour_of("garbage") == -99            # 会被距离比较推开，不抛错


def test_forecast_ttl_is_short_but_geocode_long():
    """天气临近开赛会变，缓存必须短；城市坐标几乎不变，缓存可以很长。"""
    assert FORECAST_TTL < GEOCODE_TTL
