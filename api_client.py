"""API-Football 异步客户端：超时、重试、错误翻译、统一缓存。

支持两种订阅渠道（接口完全相同，只有地址和鉴权头不同）：
  - rapidapi ：RapidAPI 订阅，使用 RAPID_API_KEY
  - apisports：api-football.com 官方直连，使用 API_FOOTBALL_KEY
"""
from __future__ import annotations
from errfmt import fmt_exc

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Callable

import httpx
import os

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
    AsyncTTLCache = None  # type: ignore[assignment,misc]  # 依赖缺失时的降级


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
# 进行中比赛的比分分钟级变化，缓存必须短；否则「实时」只是看起来实时
TTL_LIVE = 60
TTL_SEASONS = 24 * 3600
TTL_SEASON_FIXTURES = 6 * 3600
TTL_INJURIES = 3 * 3600
TTL_LINEUPS = 10 * 60  # 阵容临近开赛才公布，缓存要短
TTL_STATS = 24 * 3600
# 球队名单几乎不变（一个转会窗才动一次），赛季统计一天更新一次足够
TTL_SQUAD = 24 * 3600
TTL_PLAYER_STATS = 12 * 3600
# /players 每页 20 人，一支一线队 20–30 人，故最多翻 3 页即止。
# 设上限是为了防止接口异常时陷入死循环把额度烧光。
PLAYER_STATS_MAX_PAGES = 3

HTTP_HINTS = {
    401: "API Key 无效或缺失",
    403: "无权访问（RapidAPI 上通常表示该 Key 未订阅 API-Football，或 Key 填错）",
    404: "接口地址不存在",
    429: "请求过于频繁，或今日额度已用尽",
}


class APIError(RuntimeError):
    """数据源返回错误（额度用尽、订阅无效、参数错误等）。"""


# 「当日额度耗尽」的判据特征。额度是按天结算的硬上限，一旦打光，
# 当天内任何重试都必然失败——重试只是白烧调用方的时间窗。
# 判据必须**精确**：只认"按天结算"的措辞，不含 "day" 的一律视为
# 可恢复的临时限流（每分钟/每秒），照旧退避重试。
# 实测生产返回的原文："You have reached the request limit for the day."
def _quota_day() -> str:
    """按额度结算时区取当日日期，供熔断的按天自动解除比对。

    与 repository._quota_day() 保持同一口径（QUOTA_TZ > TIMEZONE > Asia/Shanghai）。
    这里不直接 import repository —— 它是数据库层，会让底层 HTTP 客户端背上 ORM 依赖。
    """
    tzname = os.environ.get("QUOTA_TZ") or os.environ.get("TIMEZONE") or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:  # 容器缺 tzdata 时退化为固定 +8
        tz = timezone(__import__("datetime").timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d")


_DAILY_QUOTA_MARKERS = ("request limit for the day", "reached the request limit")


def _is_daily_quota_exhausted(*parts: object) -> bool:
    """判断错误是否属于「当日额度耗尽」这类当天不可恢复的失败。

    只在**明确出现按天结算措辞**时返回 True。未知措辞一律返回 False：
    误判成"额度耗尽"会让整套系统当天拒发所有请求，比多试两次更糟。
    """
    blob = " ".join(str(p) for p in parts if p).lower()
    return "day" in blob and any(m in blob for m in _DAILY_QUOTA_MARKERS)


SHOT_ON_TARGET_TYPES = ("Shots on Goal", "Shots on Target")


def _stat_value(statistics: list[dict[str, Any]], types: tuple[str, ...]) -> int | None:
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


def stat_types_of(response: list[dict[str, Any]]) -> list[str]:
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


def parse_shots_on_target(response: list[dict[str, Any]], *,
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


def flatten_standings(response: list[dict[str, Any]]) -> list[dict]:
    """把 /standings 的响应展开成球队积分行列表（兼容分组赛制）。"""
    rows: list[dict[str, Any]] = []
    for item in response or []:
        for group in (item.get("league") or {}).get("standings") or []:
            rows.extend(group)
    return rows


def _as_date(day) -> date:
    """把 date / datetime / ISO 字符串统一成 date。

    为什么存在：``.isoformat()`` 只在 date 上有，字符串传进来会抛
    AttributeError；datetime 的 isoformat 带时间部分，拼进查询参数
    会让数据源按「某时刻」而不是「某天」理解。两类都是静默错误的来源。

    Args:
        day: date / datetime / "YYYY-MM-DD"

    Returns:
        归一化后的 date

    Raises:
        ValueError: 类型或格式非法——**显式抛出**让调用方看见，
            而不是被内层 except 吞成「今天没有比赛」。
    """
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    if isinstance(day, str):
        return date.fromisoformat(day.strip())
    raise ValueError(f"无法识别的日期类型：{type(day).__name__}（{day!r}）")


class FootballAPI:

    # 额度熔断的归属日（None = 未熔断）。
    # 设为**类级**默认值：部分测试用 FootballAPI.__new__() 构造实例以避开
    # 网络初始化，这类实例不走 __init__，若只在 __init__ 里赋值就会
    # AttributeError。类级默认让任何构造路径都具备该属性。
    _quota_exhausted_day: str | None = None

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
        quota_floor: int = 0,
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
        # 额度熔断阈值：剩余次数低于/等于此值时主动停手。
        # 此前 quota_remaining 只被记录、从未被读取——有计数、有展示，
        # 唯独没有熔断。额度打光后会一直发无效请求，直到各调用点各自失败，
        # 期间整套系统处于无人知晓的停摆状态。
        self.quota_floor = int(quota_floor) if quota_floor else 0
        self._quota_left: int | None = None
        # 熔断所属额度日；None = 未熔断。跨日后据此自动解除。
        self._quota_exhausted_day: str | None = None
        # 实时端点最后一次失败原因（None = 最近一次成功或从未调用）。
        # 用于区分「没有进行中的比赛」与「套餐不支持 live」。
        self.last_live_error: str | None = None

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
        # ── 额度熔断 ────────────────────────────────────────────────
        # 已知额度不足时直接拒发，不再消耗最后一次机会。
        # 额度是按天结算的硬上限，打光后所有数据接口同时失效；
        # 主动熔断能让失败发生在「可预期、可定位」的位置，
        # 而不是散落在几十个调用点里各自超时。
        if self.quota_exhausted:
            raise APIError(
                f"数据源额度已耗尽（剩余 {self.quota_remaining}），已主动停止请求。"
                "额度每日重置，请稍后重试。")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        url = f"{self._base_url}/{path}"
        last_error = "未知错误"

        for attempt in range(self._retries + 1):
            retry_wait = self._retry_delay * (attempt + 1)
            try:
                resp = await self._client.get(url, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{fmt_exc(exc)}"
                log.warning("请求 %s 失败（第 %d 次）：%s", path, attempt + 1, fmt_exc(exc))
            else:
                self._track_quota(resp)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = self._describe_http(resp)
                    log.warning("请求 %s 失败（第 %d 次）：%s", path, attempt + 1, last_error)
                    # 429 有两种截然不同的含义：临时限流（可恢复）与
                    # 当日额度耗尽（当天不可恢复）。此前二者一视同仁地退避
                    # 重试 3 次，于是额度打光后每个调用点仍各烧 3 次无效请求，
                    # 而熔断开关始终未启动 —— 系统处于无人知晓的空转。
                    if resp.status_code == 429 and _is_daily_quota_exhausted(
                            last_error, self._safe_body_text(resp)):
                        self._mark_quota_exhausted(last_error)
                        break
                elif resp.status_code >= 400:
                    raise APIError(self._describe_http(resp))
                else:
                    payload = self._json(resp)
                    errors = payload.get("errors")
                    if errors:
                        if isinstance(errors, dict) and "rateLimit" in errors:
                            last_error = self._format_errors(errors)
                            # 同上：rateLimit 里写明"按天"才是真的没救，
                            # 其余（每分钟限流）照旧退避重试。
                            if _is_daily_quota_exhausted(last_error):
                                self._mark_quota_exhausted(last_error)
                                break
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

    def _mark_quota_exhausted(self, reason: str) -> None:
        """确认当日额度已耗尽：置熔断，后续请求在入口即被拒发。

        quota_exhausted 只在 _quota_left 已知时为真，所以必须显式写 0，
        否则接口不返回 remaining 头时熔断永远不启动。
        """
        self._quota_left = 0
        self.quota_remaining = "0"
        self._quota_exhausted_day = _quota_day()
        log.error("主源当日额度已耗尽，已熔断至次日（%s）重置：%s",
                  self._quota_exhausted_day, str(reason)[:120])

    @staticmethod
    def _safe_body_text(resp: httpx.Response) -> str:
        """安全读取响应体文本用于额度判别；读不到不影响主流程。"""
        try:
            return (resp.text or "")[:500]
        except Exception:  # noqa: BLE001
            return ""

    def _track_quota(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            self.quota_remaining = remaining
            try:
                self._quota_left = int(str(remaining).strip())
            except (TypeError, ValueError):
                # 头部不是整数（部分网关返回带引号的字符串），保持未知
                self._quota_left = None
                return
            # 维护熔断归属日：额度恢复时立即清除；耗尽时记下当天，
            # 供 quota_exhausted 跨天自动解除比对。否则「响应头返回 0」
            # 这条路径同样会形成永不解除的死锁。
            if self._quota_left > int(getattr(self, "quota_floor", 0) or 0):
                self._quota_exhausted_day = None
            elif self._quota_exhausted_day is None:
                self._quota_exhausted_day = _quota_day()

    @property
    def quota_left(self) -> int | None:
        """数据源上报的剩余请求次数；未知返回 None（不猜）。"""
        return self._quota_left

    @property
    def quota_exhausted(self) -> bool:
        """额度是否已被判定为不足。

        只在**明确知道**剩余次数时才熔断；未知（None）一律不触发——
        否则一次头部解析失败就会让整套系统拒发所有请求，
        那比额度打光本身更糟。
        """
        # 跨天自动解除：额度按日结算，熔断只应在触发当日有效。
        # 此前熔断是单向锁存——唯一能改写 _quota_left 的 _track_quota 只在
        # 真实发出请求后执行，而熔断后请求在入口即被拒，永远走不到更新，
        # 于是主源被永久停用：/status 长期显示「备用源生效中」，
        # 而数据源侧额度其实仍有剩余。这里按额度日比对解锁。
        if self._quota_exhausted_day is not None:
            today = _quota_day()
            if self._quota_exhausted_day != today:
                log.info("额度已跨日（%s -> %s），自动解除主源熔断",
                         self._quota_exhausted_day, today)
                self._quota_exhausted_day = None
                self._quota_left = None
                self.quota_remaining = None
                return False
            return True
        left = self._quota_left
        if left is None:
            return False
        return left <= int(getattr(self, "quota_floor", 0) or 0)

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
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
    async def get_fixtures(self, league_id: int, season: int, date_from, date_to) -> list[dict[str, Any]]:
        """指定日期范围（含）内的赛程。

        两个端点同样走 ``_as_date`` 归一化——理由与 ``get_fixtures_by_date``
        相同：``str`` 没有 ``.isoformat()``，而调用方传字符串是常态
        （配置、CLI、数据库取出的都是字符串）。
        """
        params = {
            "league": league_id, "season": season,
            "from": _as_date(date_from).isoformat(),
            "to": _as_date(date_to).isoformat(),
        }
        return await self._get("fixtures", params, ttl=TTL_FIXTURES)

    async def get_fixtures_by_date(self, day) -> list[dict[str, Any]]:
        """某一天的全部赛程（不按联赛过滤）。

        ⚠️ ``day`` 可以是 ``date``、``datetime`` 或 ISO 字符串
        （``"2026-10-26"``），**在入口统一归一化**。

        为什么必须在这里做而不是靠调用方自觉：这个坑历史上踩了两次——
        - 结算回填（PR #112）：``stale_pending_days()`` 返回字符串，
          ``"2026-10-26".isoformat()`` 抛 AttributeError，异常被内层
          ``except Exception`` 吞成空列表，表现为「成功取数 17 天、
          补回 0 场」——全程无报错却毫无进展。
        - ``/oddsprobe``：同样的字符串，同样静默失败。

        只在调用方改，下一个调用点还会再犯。归一化收到入口，
        这一类 bug 整体消失。
        """
        d = _as_date(day)
        params = {"date": d.isoformat()}
        return await self._get("fixtures", params, ttl=TTL_FIXTURES)

    async def get_fixtures_by_season(self, league_id: int, season: int) -> list[dict[str, Any]]:
        """整季赛程（只按赛季拉取，不带日期范围）。"""
        params = {"league": league_id, "season": season}
        return await self._get("fixtures", params, ttl=TTL_SEASON_FIXTURES)

    async def get_live_fixtures(self) -> list[dict[str, Any]]:
        """正在进行的比赛（/fixtures?live=all）。

        ⚠️ 套餐门槛：`live=all` 属于付费能力，免费层会直接拒绝。
        因此这里与 get_injuries / get_lineups 一样按**可选数据**处理——
        取不到一律返回空列表，绝不能让看板整页报错。

        TTL 刻意设短（TTL_LIVE）：进行中比赛的比分分钟级变化，
        沿用 TTL_FIXTURES（30 分钟）等于把实时数据伪装成实时。

        空列表有三种含义，上层必须能区分，否则用户会把「套餐不支持」
        误读成「现在没有比赛」——两者处置方式完全不同（前者要换套餐，
        后者只需等待）。因此把失败原因留在 ``last_live_error`` 上：
          1. 空列表 + last_live_error=None → 此刻确实没有进行中的比赛
          2. 空列表 + 含 "套餐/订阅/permission" → 套餐不支持
          3. 空列表 + 其他原因 → 限流或临时故障
        """
        try:
            data = await self._get("fixtures", {"live": "all"}, ttl=TTL_LIVE)
        except APIError as exc:
            self.last_live_error = str(exc)
            log.info("实时赛况不可用（live=all）：%s", exc)
            return []
        else:
            self.last_live_error = None
            return data

    async def get_injuries(self, fixture_id: int) -> list[dict[str, Any]]:
        """指定比赛的伤停名单。"""
        try:
            return await self._get("injuries", {"fixture": fixture_id}, ttl=TTL_INJURIES)
        except APIError as exc:
            log.info("伤停不可用（fixture=%s）：%s", fixture_id, exc)
            return []

    async def get_lineups(self, fixture_id: int) -> list[dict[str, Any]]:
        """指定比赛的首发阵容（/fixtures/lineups）。

        官方说明：阵容通常只在开赛前 20–40 分钟才公布，部分赛事甚至更晚。
        因此这是**可选数据**，取不到（未公布/套餐不含/限流）一律返回空，
        绝不能让预测失败。
        """
        try:
            return await self._get("fixtures/lineups", {"fixture": fixture_id},
                                   ttl=TTL_LINEUPS)
        except APIError as exc:
            log.info("阵容不可用（fixture=%s）：%s", fixture_id, exc)
            return []

    async def get_squad(self, team_id: int) -> list[dict[str, Any]]:
        """球队当前名单（/players/squads），**含位置**。

        它的价值在于补上伤停端点缺失的字段：`/injuries` 只给缺阵类型、
        不给位置，因此无法判断「缺的是前锋还是边后卫」。拿到名单后可按
        player.id 把缺阵者映射回位置，做位置加权（见 `context.py`）。

        取不到一律返回空 —— 位置加权只是增强，缺了应退化为按人数计，
        绝不能让预测失败。
        """
        try:
            return await self._get("players/squads", {"team": team_id}, ttl=TTL_SQUAD)
        except APIError as exc:
            log.info("球队名单不可用（team=%s）：%s", team_id, exc)
            return []

    async def get_team_player_stats(self, team_id: int, season: int) -> list[dict[str, Any]]:
        """某队全队本赛季的球员统计（/players），含 `games.rating`。

        评分是**赛季累计平均**，只含已结束的比赛，因此赛前可用、无前视偏差。
        （`/fixtures/players` 的单场 rating 是赛后数据，**不可**用于赛前。）

        接口按 20 人分页，一线队通常 2 页即可取完，最多翻
        `PLAYER_STATS_MAX_PAGES` 页。取不到返回空。
        """
        out: list[dict[str, Any]] = []
        for page in range(1, PLAYER_STATS_MAX_PAGES + 1):
            try:
                chunk = await self._get(
                    "players", {"team": team_id, "season": season, "page": page},
                    ttl=TTL_PLAYER_STATS,
                )
            except APIError as exc:
                log.info("球员统计不可用（team=%s 第%d页）：%s", team_id, page, exc)
                break
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < 20:      # 不足一页即最后一页
                break
        return out

    async def get_standings(self, league_id: int, season: int) -> list[dict[str, Any]]:
        """积分榜（含主客场进球/失球）。"""
        response = await self._get("standings", {"league": league_id, "season": season}, ttl=TTL_STANDINGS)
        return flatten_standings(response)

    async def get_odds(self, fixture_id: int, fresh: bool = False) -> list[dict[str, Any]]:
        return await self._get("odds", {"fixture": fixture_id}, ttl=TTL_ODDS, fresh=fresh)

    async def get_h2h(self, home_id: int, away_id: int, last: int = 5) -> list[dict[str, Any]]:
        params = {"h2h": f"{home_id}-{away_id}", "last": last}
        return await self._get("fixtures/headtohead", params, ttl=TTL_H2H)

    async def get_team_form(self, team_id: int, season: int, last: int = 5) -> list[dict[str, Any]]:
        """某支球队最近 last 场已结束的比赛。"""
        params = {"team": team_id, "season": season, "last": last}
        matches = await self._get("fixtures", params, ttl=TTL_FORM)
        done = {"FT", "AET", "PEN"}
        return [m for m in matches or [] if ((m.get("fixture") or {}).get("status") or {}).get("short") in done]

    async def get_fixture_statistics(self, fixture_id: int) -> list[dict[str, Any]]:
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

    async def get_account_status(self) -> dict[str, Any]:
        """账户与额度（用于 /status 诊断）。"""
        response = await self._get("status", {}, ttl=TTL_STATUS)
        return response if isinstance(response, dict) else {}
