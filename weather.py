"""赛前天气（Open-Meteo）/ Pre-match weather via Open-Meteo.

为什么单独一个模块 / Why a separate module
-------------------------------------------
天气**不是** API-Football 提供的能力（它没有任何 weather 端点），
所以不能塞进 `api_client.py` —— 那里的每个请求都要带订阅 key、都要占
7500/天 的额度。Open-Meteo 免费、无需注册、也不占用我们的足球额度。

    Weather is **not** an API-Football capability (it has no weather endpoint
    at all), so it cannot live in `api_client.py` — every request there spends
    the paid 7500/day quota. Open-Meteo is free, needs no sign-up, and costs
    us nothing on the football plan.

取数链路 / Lookup chain
-----------------------
    fixture.venue.city  →  Geocoding API  →  (lat, lon)  →  Forecast API

球场经纬度拿不到：fixture 里只有 `venue: {id, name, city}`，没有坐标。
好在天气在城市尺度上足够用 —— 同一座城市的两座球场不会一边下雨一边暴晒。

    Venue coordinates are unavailable: a fixture only carries
    `venue: {id, name, city}`. City-level weather is fine — two stadiums in the
    same city do not differ by meaningful precipitation or wind.

诚实边界 / Honest limits
------------------------
* **效应未经验证，默认关闭。** 公开研究互相矛盾：英超 22 赛季分析称湿润月份
  +0.22 球/场，极端降雨 −0.8 球；但英冠 4,826 场分析显示降雨**无统计显著效应**。
  因此本模块**只搭管道、不改结果**，除非显式把 `WEATHER_STRENGTH` 调为正值。

  The effect is **unvalidated and off by default**. Published studies disagree
  (EPL wet months +0.22 goals/match and extreme rain −0.8, vs. a 4,826-match
  Championship analysis finding **no significant** rain effect). So this module
  builds the pipe but changes nothing unless `WEATHER_STRENGTH` is set > 0.

* 这是四类赛前信号里**唯一能真正回测的**：Open-Meteo 有 1940 年至今的归档，
  而伤停/阵容/评分都不存在于任何历史语料中。也就是说，「要不要开天气」这个
  问题是**可以**用数据回答的，不像另外三项只能靠线上 A/B 慢慢积累。
  启用前请务必先在自己库上验证，不要凭直觉开。

  This is the **only** one of the four pre-match signals that can genuinely be
  back-tested: Open-Meteo archives reach back to 1940, whereas injuries,
  lineups and ratings exist in no historical corpus. In other words the question
  "should we enable weather?" **can** be answered with data, unlike the other
  three which require slow live A/B accumulation. Validate on your own DB before
  enabling — never switch it on by intuition.

* 任何一步失败（城市为空、地理编码无结果、网络不通）都返回 None，
  **绝不**让预测失败。

  Any failure (empty city, no geocoding match, network down) returns None and
  **never** fails a prediction.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any
from errfmt import fmt_exc

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_TIMEOUT = 8.0

# 城市→坐标几乎不变，缓存 30 天；天气本身只缓存 30 分钟（临近开赛会更新）。
GEOCODE_TTL = 30 * 24 * 3600.0
FORECAST_TTL = 30 * 60.0

# Open-Meteo 免费额度是每天 1 万次，但我们一天只用几十次；
# 这里的限流只是防止异常循环把共享 IP 打爆。
MAX_GEOCACHE = 500


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


class _TTLCache:
    """极简 TTL + 容量上限缓存。天气是旁路数据，不值得引入额外依赖。"""

    def __init__(self, ttl: float, max_items: int) -> None:
        self._ttl = ttl
        self._max = max_items
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, now: float) -> Any:
        hit = self._data.get(key)
        if hit is None:
            return None
        stamp, value = hit
        if now - stamp > self._ttl:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any, now: float) -> None:
        if len(self._data) >= self._max:
            # 淘汰最老的一半，比逐个 LRU 简单且足够
            oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[: self._max // 2 or 1]
            for k, _ in oldest:
                self._data.pop(k, None)
        self._data[key] = (now, value)


class WeatherSample:
    """开赛时刻的天气快照。"""

    __slots__ = ("temp_c", "precip_mm", "wind_kmh", "humidity")

    def __init__(
        self,
        temp_c: float | None = None,
        precip_mm: float | None = None,
        wind_kmh: float | None = None,
        humidity: float | None = None,
    ) -> None:
        self.temp_c = temp_c
        self.precip_mm = precip_mm
        self.wind_kmh = wind_kmh
        self.humidity = humidity

    def as_dict(self) -> dict[str, float | None]:
        return {
            "temp_c": self.temp_c,
            "precip_mm": self.precip_mm,
            "wind_kmh": self.wind_kmh,
            "humidity": self.humidity,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"WeatherSample({self.as_dict()})"


class WeatherClient:
    """Open-Meteo 取数客户端。所有方法失败都返回 None。"""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        enabled: bool = True,
    ) -> None:
        self._timeout = timeout
        self.enabled = enabled
        self._geo = _TTLCache(GEOCODE_TTL, MAX_GEOCACHE)
        self._fc = _TTLCache(FORECAST_TTL, MAX_GEOCACHE)
        self._client: Any = None

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover - 关闭失败无需处理
                pass
            self._client = None

    def _now(self) -> float:
        return asyncio.get_event_loop().time()

    async def _http(self) -> Any:
        """惰性创建 httpx 客户端（与 api_client 保持同一依赖）。"""
        if self._client is None:
            try:
                import httpx
            except ImportError:  # pragma: no cover - httpx 是硬依赖
                log.info("httpx 未安装，天气信号不可用")
                return None
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def coords_for_city(self, city: str) -> tuple[float, float] | None:
        """城市名 → (纬度, 经度)。缓存 30 天。"""
        if not self.enabled or not city or not city.strip():
            return None
        name = city.strip()
        now = self._now()
        cached = self._geo.get(name, now)
        if cached is not None:
            return cached

        client = await self._http()
        if client is None:
            return None
        try:
            resp = await client.get(GEOCODE_URL, params={"name": name, "count": 1, "format": "json"})
            if resp.status_code >= 400:
                log.info("地理编码失败（city=%s）：HTTP %s", name, resp.status_code)
                return None
            results = (resp.json() or {}).get("results") or []
        except Exception as exc:
            log.info("地理编码异常（city=%s）：%s", name, fmt_exc(exc))
            return None

        if not results:
            # 负结果也缓存，避免同一座缺失城市被反复查
            self._geo.set(name, None, now)
            return None
        lat = _as_float(results[0].get("latitude"))
        lon = _as_float(results[0].get("longitude"))
        if lat is None or lon is None:
            self._geo.set(name, None, now)
            return None
        value = (lat, lon)
        self._geo.set(name, value, now)
        return value

    async def at_kickoff(
        self, lat: float, lon: float, when: datetime | None
    ) -> WeatherSample | None:
        """指定坐标与时刻的天气。when 为空则用当前时间。"""
        if not self.enabled:
            return None
        moment = when or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        day = moment.astimezone(timezone.utc).date().isoformat()
        hour = moment.astimezone(timezone.utc).hour

        now = self._now()
        key = (round(lat, 3), round(lon, 3), day)
        cached = self._fc.get(key, now)
        if cached is None:
            client = await self._http()
            if client is None:
                return None
            params = {
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,precipitation,wind_speed_10m,relative_humidity_2m",
                "start_date": day,
                "end_date": day,
                "timezone": "UTC",
            }
            try:
                resp = await client.get(FORECAST_URL, params=params)
                if resp.status_code >= 400:
                    log.info("天气获取失败（%s）：HTTP %s", key, resp.status_code)
                    return None
                cached = (resp.json() or {}).get("hourly") or {}
            except Exception as exc:
                log.info("天气异常（%s）：%s", key, fmt_exc(exc))
                return None
            self._fc.set(key, cached, now)

        times = cached.get("time") or []
        try:
            idx = times.index(f"{day}T{hour:02d}:00")
        except ValueError:
            # 用不到精确小时就取最近的一条，比直接放弃好
            if not times:
                return None
            idx = min(
                range(len(times)),
                key=lambda i: abs(_hour_of(times[i]) - hour),
            )

        def pick(field: str) -> float | None:
            series = cached.get(field) or []
            return _as_float(series[idx]) if idx < len(series) else None

        return WeatherSample(
            temp_c=pick("temperature_2m"),
            precip_mm=pick("precipitation"),
            wind_kmh=pick("wind_speed_10m"),
            humidity=pick("relative_humidity_2m"),
        )

    async def for_fixture(self, city: str | None, when: datetime | None) -> WeatherSample | None:
        """一步到位：城市 + 开赛时刻 → 天气快照。"""
        if not self.enabled or not city or not city.strip():
            return None
        coords = await self.coords_for_city(city)
        if coords is None:
            return None
        return await self.at_kickoff(coords[0], coords[1], when)


def _hour_of(stamp: str) -> int:
    """从 'YYYY-MM-DDT HH:MM' 里取小时，解析失败给 -99（会被 distance 推开）。"""
    try:
        return int(stamp.split("T")[1].split(":")[0])
    except (IndexError, ValueError):
        return -99
