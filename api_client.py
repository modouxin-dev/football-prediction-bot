"""API-Football 异步客户端：超时、重试、错误翻译、内存缓存。

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

# 缓存时长（秒）：尽量节省免费套餐很紧张的请求额度
TTL_FIXTURES = 30 * 60
TTL_STANDINGS = 6 * 3600
TTL_ODDS = 15 * 60
TTL_H2H = 12 * 3600

HTTP_HINTS = {
    401: "API Key 无效或缺失",
    403: "无权访问（RapidAPI 上通常表示该 Key 未订阅 API-Football，或 Key 填错）",
    404: "接口地址不存在",
    429: "请求过于频繁，或今日额度已用尽",
}


class APIError(RuntimeError):
    """数据源返回错误（额度用尽、订阅无效、参数错误等）。"""


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
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self.quota_remaining: str | None = None  # 最近一次响应里的日额度剩余

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # ---- 底层请求 ---------------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any], ttl: float = 0.0, fresh: bool = False) -> Any:
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        now = time.monotonic()
        hit = self._cache.get(key)
        if ttl and not fresh and hit and hit[0] > now:
            return hit[1]
        data = await self._request(path, params)
        if ttl:
            self._cache[key] = (now + ttl, data)
            self._prune(now)
        return data

    def _prune(self, now: float) -> None:
        if len(self._cache) > 200:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}

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
        hint = HTTP_HINTS.get(code) or ("数据源服务端错误" if code >= 500 else "")
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

    async def get_standings(self, league_id: int, season: int) -> list[dict]:
        """积分榜（含主客场进球/失球），一次请求即可算出全联赛球队强度。"""
        response = await self._get("standings", {"league": league_id, "season": season}, ttl=TTL_STANDINGS)
        return flatten_standings(response)

    async def get_odds(self, fixture_id: int, fresh: bool = False) -> list[dict]:
        return await self._get("odds", {"fixture": fixture_id}, ttl=TTL_ODDS, fresh=fresh)

    async def get_h2h(self, home_id: int, away_id: int, last: int = 5) -> list[dict]:
        params = {"h2h": f"{home_id}-{away_id}", "last": last}
        return await self._get("fixtures/headtohead", params, ttl=TTL_H2H)

    async def get_account_status(self) -> dict:
        """账户与额度（用于 /status 诊断，不缓存）。"""
        response = await self._request("status", {})
        return response if isinstance(response, dict) else {}
