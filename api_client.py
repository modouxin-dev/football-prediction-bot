"""API-Football 异步客户端：超时、重试、错误翻译、统一缓存。

支持两种订阅渠道（接口完全相同，只有地址和鉴权头不同）：
  - rapidapi ：RapidAPI 订阅，使用 RAPID_API_KEY
  - apisports：api-football.com 官方直连，使用 API_FOOTBALL_KEY
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any, Callable

import httpx

# 缓存后端：优先用 cache_manager 的 TTL + LRU 实现（有容量上限，不会无界增长），
# 取不到时退回简单字典。
#
# 注意：这里只导入「类」，不导入模块级的全局实例 api_cache。全局实例在进程内
# 单例，多个 FootballAPI 实例会共用同一份缓存，而缓存键只有 (path, params)，
# 不含 provider / base_url / 凭据 —— 不同渠道（rapidapi 与 apisports）、
# 不同账号的响应会互相串；测试里每个用例新建一个客户端，前一个用例写进去的
# 结果会被后一个用例命中，表现为「该报错的没报错、该发请求的没发请求」。
# 缓存改为按客户端实例独占即可解决，且生产只有一个实例（main.py:956），
# 跨调用复用与容量上限都保留。
try:
    from cache_manager import AsyncTTLCache
except ImportError:
    AsyncTTLCache = None


class SimpleCache:
    """cache_manager 不可用时的退路：无容量上限的字典缓存（向后兼容）。"""

    def __init__(self):
        self._cache = {}

    async def get(self, key):
        return self._cache.get(key)

    async def set(self, key, value, ttl=None):
        self._cache[key] = value


def _new_response_cache():
    """新建一个客户端实例独占的响应缓存。"""
    if AsyncTTLCache is not None:
        return AsyncTTLCache(max_size=500, default_ttl=3600)
    return SimpleCache()

log = logging.getLogger(__name__)

RAPID_HOST = "api-football-v1.p.rapidapi.com"
PROVIDERS: dict[str, tuple[str, Callable[[str], dict[str, str]]]] = {
    "rapidapi": (
        f"https://{RAPID_HOST}/v3",
        lambda key: {"X-RapidAPI-Key": key, "X-RapidAPI-Host": RAPID_HOST},
    ),
    "apisports": (
        "https://v3.football.api-sports.io",
        lambda key: {"x-apisports-key": key},
    ),
}

# 缓存时长（秒）
TTL_FIXTURES = 30 * 60
TTL_STANDINGS = 6 * 3600
TTL_ODDS = 15 * 60
TTL_H2H = 12 * 3600
TTL_FORM = 12 * 3600
TTL_STATUS = 5 * 60
TTL_SEASONS = 24 * 3600
TTL_SEASON_FIXTURES = 6 * 3600
TTL_INJURIES = 3 * 3600
TTL_STATS = 24 * 3600

HTTP_HINTS = {
    401: "API Key 无效或缺失",
    403: "无权访问（RapidAPI 上通常表示该 Key 未订阅 API-Football，或 Key 填错）",
    404: "接口地址不存在",
    429: "请求过于频繁，或今日额度已用尽",
}


class APIError(RuntimeError):
    """数据源返回错误（额度用尽、订阅无效、参数错误等）。"""


SHOT_ON_TARGET_TYPES = ("Shots on Goal", "Shots on Target")


def _stat_value(statistics: list[dict], types: tuple[str, ...]) -> int | None:
    """在单队的 statistics 数组里取出计数值。"""
    for item in statistics or []:
        if not isinstance(item, dict):
            continue
        if (item.get("type") or "").strip() not in types:
            continue
        v = item.get("value")
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            try:
                return int(v.strip())
            except ValueError:
                return None
    return None


def stat_types_of(response: list[dict]) -> list[str]:
    """响应里出现的统计项名称（用于探测：确认套餐下到底有哪些字段）。"""
    out: list[str] = []
    for item in response or []:
        if not isinstance(item, dict):
            continue
        for st in item.get("statistics") or []:
            name = (st.get("type") or "").strip()
            if name and name not in out:
                out.append(name)
    return out


def parse_shots_on_target(response: list[dict], *,
                          home_team_id=None, away_team_id=None) -> dict[str, int] | None:
    """从 /fixtures/statistics 响应解析主客队射正数。"""
    entries: list[tuple[str | None, int | None]] = []
    for item in response or []:
        if not isinstance(item, dict):
            continue
        team = item.get("team") or {}
        tid = team.get("id")
        entries.append((str(tid) if tid is not None else None,
                        _stat_value(item.get("statistics") or [], SHOT_ON_TARGET_TYPES)))
    if len(entries) < 2:
        return None

    home = away = None
    if home_team_id is not None and away_team_id is not None:
        hid, aid = str(home_team_id), str(away_team_id)
        for tid, sot in entries:
            if tid == hid:
                home = sot
            elif tid == aid:
                away = sot
    else:
        home, away = entries[0][1], entries[1][1]

    if home is None or away is None:
        return None
    return {"home": int(home), "away": int(away)}


def flatten_standings(response: list[dict]) -> list[dict]:
    """把 /standings 的响应展开成球队积分行列表（兼容分组赛制）。"""
    rows: list[dict] = []
    for item in response or []:
        for group in (item.get("league") or {}).get("standings") or []:
            rows.extend(group)
    return rows


class FootballAPI:
    def __init__(
        self,
        api_key: str,
        *,
        provider: str = "rapidapi",
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        retries: int = 2,
        retry_delay: float = 1.5,
        base_url: str | None = None,
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"未知的数据源渠道：{provider}")
        default_base, make_headers = PROVIDERS[provider]
        self.provider = provider
        self._headers = make_headers(api_key)
        self._base_url = (base_url or default_base).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._retries = retries
        self._retry_delay = retry_delay
        # 响应缓存按实例独占（见文件头说明）；仍走 cache_manager 的 TTL+LRU
        # 实现，容量上限与过期淘汰都保留。
        self._cache = _new_response_cache()
        self.quota_remaining: str | None = None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # ---- 底层请求 ---------------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any], ttl: float = 0.0, fresh: bool = False) -> Any:
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))

        # 检查缓存
        if ttl and not fresh:
            cached = await self._cache.get(key)
            if cached is not None:
                log.debug(f"缓存命中: {path}")
                return cached

        # 调用 API
        data = await self._request(path, params)

        # 存入缓存
        if ttl:
            await self._cache.set(key, data, ttl)
            log.debug(f"缓存存储: {path} (TTL: {ttl}s)")

        return data

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        url = f"{self._base_url}/{path}"
        last_error = "未知错误"

        for attempt in range(self._retries + 1):
            retry_wait = self._retry_delay * (attempt + 1)
            try:
                resp = await self._client.get(url, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{type(exc).__name__}"
                log.warning("请求 %s 失败（第 %d 次）：%s", path, attempt + 1, type(exc).__name__)
            else:
                self._track_quota(resp)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = self._describe_http(resp)
                    log.warning("请求 %s 失败（第 %d 次）：%s", path, attempt + 1, last_error)
                elif resp.status_code >= 400:
                    raise APIError(self._describe_http(resp))
                else:
                    payload = self._json(resp)
                    errors = payload.get("errors")
                    if errors:
                        if isinstance(errors, dict) and "rateLimit" in errors:
                            last_error = self._format_errors(errors)
                            retry_wait = max(retry_wait, 6.0) if self._retry_delay else 0
                        else:
                            raise APIError(self._format_errors(errors))
                    elif "response" not in payload and payload.get("message"):
                        raise APIError(str(payload["message"]))
                    else:
                        return payload.get("response", [])
            if attempt < self._retries:
                await asyncio.sleep(retry_wait)

        raise APIError(last_error)

    def _track_quota(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            self.quota_remaining = remaining

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            payload = resp.json()
        except ValueError:
            raise APIError("数据源返回的内容不是有效的 JSON") from None
        if not isinstance(payload, dict):
            raise APIError("数据源返回的 JSON 结构异常")
        return payload

    @staticmethod
    def _describe_http(resp: httpx.Response) -> str:
        code = resp.status_code
        hint = HTTP_HINTS.get(code) or (("数据源服务端错误" if code >= 500 else ""))
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("message"):
                detail = str(body["message"])[:200]
        except ValueError:
            detail = (resp.text or "")[:120]
        text = f"HTTP {code}" + (f" {hint}" if hint else "")
        return text + (f"（响应：{detail}）" if detail else "")

    @staticmethod
    def _format_errors(errors: Any) -> str:
        if isinstance(errors, dict):
            text = "; ".join(f"{k}: {v}" for k, v in errors.items())
            if "plan" in errors:
                text += "（当前订阅套餐不支持这个请求，请在 API-Football 控制台确认套餐与可用赛季）"
            return text
        return str(errors)

    # ---- 业务接口 ---------------------------------------------------------
    async def get_fixtures(self, league_id: int, season: int, date_from: date, date_to: date) -> list[dict]:
        """指定日期范围（含）内的赛程。"""
        params = {"league": league_id, "season": season, "from": date_from.isoformat(), "to": date_to.isoformat()}
        return await self._get("fixtures", params, ttl=TTL_FIXTURES)

    async def get_fixtures_by_date(self, day: date) -> list[dict]:
        """某一天的全部赛程（不按联赛过滤）。"""
        params = {"date": day.isoformat()}
        return await self._get("fixtures", params, ttl=TTL_FIXTURES)

    async def get_fixtures_by_season(self, league_id: int, season: int) -> list[dict]:
        """整季赛程（只按赛季拉取，不带日期范围）。"""
        params = {"league": league_id, "season": season}
        return await self._get("fixtures", params, ttl=TTL_SEASON_FIXTURES)

    async def get_injuries(self, fixture_id: int) -> list[dict]:
        """指定比赛的伤停名单。"""
        try:
            return await self._get("injuries", {"fixture": fixture_id}, ttl=TTL_INJURIES)
        except APIError as exc:
            log.info("伤停不可用（fixture=%s）：%s", fixture_id, exc)
            return []

    async def get_standings(self, league_id: int, season: int) -> list[dict]:
        """积分榜（含主客场进球/失球）。"""
        response = await self._get("standings", {"league": league_id, "season": season}, ttl=TTL_STANDINGS)
        return flatten_standings(response)

    async def get_odds(self, fixture_id: int, fresh: bool = False) -> list[dict]:
        return await self._get("odds", {"fixture": fixture_id}, ttl=TTL_ODDS, fresh=fresh)

    async def get_h2h(self, home_id: int, away_id: int, last: int = 5) -> list[dict]:
        params = {"h2h": f"{home_id}-{away_id}", "last": last}
        return await self._get("fixtures/headtohead", params, ttl=TTL_H2H)

    async def get_team_form(self, team_id: int, season: int, last: int = 5) -> list[dict]:
        """某支球队最近 last 场已结束的比赛。"""
        params = {"team": team_id, "season": season, "last": last}
        matches = await self._get("fixtures", params, ttl=TTL_FORM)
        done = {"FT", "AET", "PEN"}
        return [m for m in matches or [] if ((m.get("fixture") or {}).get("status") or {}).get("short") in done]

    async def get_fixture_statistics(self, fixture_id: int) -> list[dict]:
        """单场技术统计（/fixtures/statistics）。"""
        return await self._get("fixtures/statistics", {"fixture": fixture_id}, ttl=TTL_STATS)

    async def get_available_seasons(self) -> list[int]:
        """当前账号可访问的赛季列表。"""
        response = await self._get("leagues/seasons", {}, ttl=TTL_SEASONS)
        seasons: set[int] = set()
        for item in response or []:
            try:
                seasons.add(int(item))
            except (TypeError, ValueError):
                continue
        return sorted(seasons)

    async def get_account_status(self) -> dict:
        """账户与额度（用于 /status 诊断）。"""
        response = await self._get("status", {}, ttl=TTL_STATUS)
        return response if isinstance(response, dict) else {}

