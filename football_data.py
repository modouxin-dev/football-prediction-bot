"""备用数据源：football-data.org（https://www.football-data.org/documentation/quickstart）

定位：API-Football 为主源，本模块为备用源。当主源出现 401/403/429、返回错误、
空数据或赛季不可用时接管「赛程、赛果、积分榜」。

关键设计：
- 返回的**结构与 API-Football 完全一致**（协议转换），因此 analyzer / bot_handler /
  图表等上层代码无需关心数据来源，也不用修改。
- 比赛 ID 统一加 "fd-" 前缀，避免与 API-Football 的数字 ID 混用导致按钮点错比赛。
- Token 只从环境变量读取，绝不打印、不进日志、不进异常信息。
- 免费层限制：无赔率、无历史交锋、无球员统计，对应方法返回空值（不伪造）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"

# 缓存时长（秒）：免费层 10 次/分钟，必须缓存
TTL_MATCHES = 10 * 60  # 今日赛程
TTL_STANDINGS = 30 * 60  # 联赛排名
TTL_SEASONS = 24 * 3600  # 赛季列表

MAX_RETRIES = 2  # 429 最多重试 2 次，指数退避

# API-Football 联赛 ID → football-data.org 的 competition code
# 免费层仅覆盖其中 12 个竞赛（英超/西甲/德甲/意甲/法甲/欧冠/荷甲/葡超/英冠/巴甲/世界杯/欧洲杯）
LEAGUE_ID_TO_CODE: dict[int, str] = {
    39: "PL",  # 英超
    140: "PD",  # 西甲
    78: "BL1",  # 德甲
    135: "SA",  # 意甲
    61: "FL1",  # 法甲
    2: "CL",  # 欧冠
    88: "DED",  # 荷甲
    94: "PPL",  # 葡超
    40: "ELC",  # 英冠
    71: "BSA",  # 巴甲
    1: "WC",  # 世界杯
    4: "EC",  # 欧洲杯
}

# competition code → football-data.org 的竞赛 id（全局 /v4/matches 端点过滤用）
CODE_TO_ID: dict[str, int] = {
    "PL": 2021, "PD": 2014, "BL1": 2002, "SA": 2019, "FL1": 2015, "CL": 2001,
    "DED": 2003, "PPL": 2017, "ELC": 2016, "BSA": 2013, "WC": 2000, "EC": 2018,
}

# football-data.org 状态 → API-Football 的 status.short
STATUS_MAP: dict[str, str] = {
    "SCHEDULED": "NS",
    "TIMED": "NS",
    "IN_PLAY": "LIVE",
    "PAUSED": "HT",
    "FINISHED": "FT",
    "POSTPONED": "PST",
    "SUSPENDED": "SUSP",
    "CANCELLED": "CANC",
    "CANCELED": "CANC",
    "AWARDED": "FT",
}


class FootballDataError(RuntimeError):
    """备用数据源错误。消息里不会出现 Token。"""


def strip_prefix(value) -> str:
    """'fd-123' → '123'；已经是纯数字则原样返回。"""
    text = str(value)
    return text[3:] if text.startswith("fd-") else text


def _code_for(league_id: int) -> str:
    """联赛 ID 转 competition code；未知联赛直接报错，不猜。"""
    code = LEAGUE_ID_TO_CODE.get(int(league_id))
    if not code:
        raise FootballDataError(f"备用数据源不支持该联赛（ID={league_id}），仅支持：" + ", ".join(map(str, sorted(LEAGUE_ID_TO_CODE))))
    return code


class FootballDataAPI:
    """football-data.org 客户端，输出与 FootballAPI 相同的数据结构。"""

    def __init__(
        self,
        api_token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        base_url: str | None = None,
    ) -> None:
        self.token = api_token
        self._headers = {"X-Auth-Token": api_token}
        self._base_url = (base_url or BASE_URL).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self.last_note: str | None = None           # 数据被回退展示时的说明 / note when shifted
        self.last_shifted_date: date | None = None  # 实际展示的比赛日 / actually shown matchday
        self.season_range: tuple[date, date] | None = None  # 赛季最早/最晚比赛日 / season span
        self.source = "football-data"

    @property
    def source_label(self) -> str:
        return "football-data.org"

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # ---- 底层请求（429 指数退避） ---------------------------------------------
    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_error = "未知错误"

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await self._client.get(url, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{type(exc).__name__}"
                log.warning("备用源请求 %s 失败（第 %d 次）", path, attempt + 1)
            else:
                if resp.status_code == 429:
                    # 免费层 10 次/分钟：429 不无限重试，退避后最多再试 2 次
                    last_error = "HTTP 429 请求过于频繁（免费层 10 次/分钟）"
                    log.warning("备用源 429 限流（第 %d 次）", attempt + 1)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(2**attempt)
                        continue
                    raise FootballDataError(last_error)
                if resp.status_code == 401:
                    raise FootballDataError("HTTP 401 Token 无效")
                if resp.status_code == 403:
                    # 免费层对未订阅竞赛也返回 403（而非 404），必须读 body 说明
                    raise FootballDataError(self._describe_403(resp))
                if resp.status_code >= 400:
                    raise FootballDataError(f"HTTP {resp.status_code}（备用数据源错误）")
                payload = self._json(resp)
                if payload.get("message"):
                    raise FootballDataError(str(payload["message"])[:200])
                return payload
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2**attempt)

        raise FootballDataError(last_error)

    @staticmethod
    def _describe_403(resp: httpx.Response) -> str:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("message"):
                detail = str(body["message"])[:200]
        except ValueError:
            detail = (resp.text or "")[:120]
        return f"HTTP 403 无权访问该资源（免费层仅覆盖 12 个竞赛）" + (f"（{detail}）" if detail else "")

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            payload = resp.json()
        except ValueError:
            raise FootballDataError("备用数据源返回的内容不是有效的 JSON") from None
        if not isinstance(payload, dict):
            raise FootballDataError("备用数据源返回的 JSON 结构异常")
        return payload

    async def _get(self, path: str, params: dict[str, Any], ttl: float = 0.0) -> Any:
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        now = time.monotonic()
        hit = self._cache.get(key)
        if ttl and hit and hit[0] > now:
            return hit[1]
        data = await self._request(path, params)
        if ttl:
            self._cache[key] = (now + ttl, data)
        return data

    # ---- 协议转换 -------------------------------------------------------------
    @staticmethod
    def _to_fixture(match: dict, league_id: int, season: int) -> dict:
        """football-data.org 的 match → API-Football 的 fixture 结构。"""
        home = match.get("homeTeam") or {}
        away = match.get("awayTeam") or {}
        score = ((match.get("score") or {}).get("fullTime")) or {}
        hg, ag = score.get("home"), score.get("away")
        competition = match.get("competition") or {}
        return {
            "fixture": {
                "id": f"fd-{match.get('id')}",  # 加前缀避免与主源 ID 冲突
                "date": match.get("utcDate"),
                "status": {"short": STATUS_MAP.get(match.get("status") or "", match.get("status") or "")},
                "venue": {"name": match.get("venue") or ""},
            },
            "league": {
                "id": league_id,
                "name": competition.get("name") or f"联赛 {league_id}",
                "season": season,
                "round": match.get("matchday") and f"Regular Season - {match['matchday']}" or "",
            },
            "teams": {
                "home": {"id": f"fd-{home.get('id')}", "name": home.get("name") or home.get("shortName") or "?"},
                "away": {"id": f"fd-{away.get('id')}", "name": away.get("name") or away.get("shortName") or "?"},
            },
            "goals": {"home": hg, "away": ag},
        }

    @staticmethod
    def _to_standings_rows(payload: dict) -> list[dict]:
        """standings（TOTAL/HOME/AWAY 三种 type）→ API-Football 的 flatten 行列表。"""
        by_team: dict[str, dict] = {}
        for block in payload.get("standings") or []:
            block_type = (block.get("type") or "TOTAL").upper()
            for row in block.get("table") or []:
                team = row.get("team") or {}
                team_id = f"fd-{team.get('id')}"
                entry = by_team.setdefault(
                    team_id,
                    {
                        "rank": row.get("position"),
                        "team": {"id": team_id, "name": team.get("name") or team.get("shortName") or "?"},
                        "points": row.get("points"),
                        "all": {},
                        "home": {},
                        "away": {},
                    },
                )
                goals = {
                    "for": row.get("goalsFor") or 0,
                    "against": row.get("goalsAgainst") or 0,
                }
                bucket = {
                    "played": row.get("playedGames") or 0,
                    "win": row.get("won") or 0,
                    "draw": row.get("draw") or 0,
                    "lose": row.get("lost") or 0,
                    "goals": goals,
                }
                if block_type == "HOME":
                    entry["home"] = bucket
                elif block_type == "AWAY":
                    entry["away"] = bucket
                else:
                    entry["all"] = bucket
        # 免费层可能只有 TOTAL：用总计的一半近似主客场，保证模型仍能算（精度低于主源）
        for entry in by_team.values():
            total = entry.get("all") or {}
            if not entry["home"]:
                entry["home"] = _half(total)
            if not entry["away"]:
                entry["away"] = _half(total)
            if not entry["all"]:
                entry["all"] = _combine(entry["home"], entry["away"])
        return list(by_team.values())

    # ---- 对外接口（与 FootballAPI 同名，便于统一调度） ----------------------------
    @staticmethod
    def _match_date(m: dict) -> date | None:
        """取出比赛的 UTC 日期（无有效时间时返回 None）。"""
        raw = m.get("utcDate") or ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            return None

    def _record_season_range(self, matches: list[dict]) -> None:
        """记录该赛季最早与最晚的比赛日，便于排查「窗口内为何 0 场」。

        Records the earliest/latest matchday of the season — helps diagnose
        why a requested window returned nothing.
        """
        days = sorted({d for d in (self._match_date(m) for m in matches) if d})
        if days:
            self.season_range = (days[0], days[-1])

    def _nearest_matchday(self, matches: list[dict], target: date) -> date | None:
        """从整季赛程里找出离 target 最近的一个比赛日（优先取不早于 target 的）。"""
        days = sorted({d for d in (self._match_date(m) for m in matches) if d})
        if not days:
            return None
        after = [d for d in days if d >= target]
        return after[0] if after else days[-1]  # 有未来场次取最近一场，否则取最后一场

    @staticmethod
    def _in_range(m: dict, date_from: date, date_to: date) -> bool:
        """本地按开赛日期过滤（用于「不带日期参数」拉取全量赛程后的筛选）。"""
        raw = m.get("utcDate") or ""
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        return date_from <= dt.date() <= date_to
    async def get_fixtures(self, league_id: int, season: int, date_from: date, date_to: date) -> list[dict]:
        code = _code_for(league_id)
        params = {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()}
        payload = await self._get(f"competitions/{code}/matches", params, ttl=TTL_MATCHES)
        matches = payload.get("matches") or []

        if not matches:
            # 免费层对部分竞赛的 dateFrom/dateTo 组合会返回空列表，
            # 退回全局赛程端点 /v4/matches 再按竞赛过滤，避免误判为「没有比赛」。
            log.info("备用源联赛端点返回空，改用全局端点 /v4/matches 重试（竞赛 %s）", code)
            payload = await self._get("matches", params, ttl=TTL_MATCHES)
            all_matches = payload.get("matches") or []
            wanted_id = CODE_TO_ID.get(code)
            matches = [
                m for m in all_matches
                if ((m.get("competition") or {}).get("code")) == code
                or (wanted_id and ((m.get("competition") or {}).get("id")) == wanted_id)
            ]
            log.info("全局端点共 %d 场，过滤后 %d 场", len(all_matches), len(matches))

        if not matches:
            # 第三级兜底：部分免费层账号的 dateFrom/dateTo 过滤会返回空，
            # 改为拉取该竞赛整季赛程（不带日期参数），再在本地按日期筛选。
            log.info("备用源带日期过滤仍为空，改用不带日期参数拉取整季赛程（竞赛 %s）", code)
            payload = await self._get(f"competitions/{code}/matches", {}, ttl=TTL_MATCHES)
            season_matches = payload.get("matches") or []
            self._record_season_range(season_matches)
            matches = [m for m in season_matches if self._in_range(m, date_from, date_to)]
            log.info("整季赛程共 %d 场，按 %s ~ %s 本地过滤后 %d 场",
                     len(season_matches), date_from, date_to, len(matches))

            if not matches and season_matches:
                # 窗口内确实没有比赛（多为赛季尚未开始或已结束）。
                # 不伪造数据，而是定位这批真实数据里「离请求窗口最近的一个比赛日」，
                # 由上层明确标注日期后展示，总比一句「暂无赛程」更有用。
                nearest = self._nearest_matchday(season_matches, date_from)
                if nearest is not None:
                    matches = [
                        m for m in season_matches
                        if self._match_date(m) == nearest
                    ]
                    self.last_note = (
                        f"请求区间 {date_from} ~ {date_to} 内该联赛没有比赛，"
                        f"以下展示数据源中最近的比赛日 {nearest}（真实数据，非预测）"
                    )
                    self.last_shifted_date = nearest
                    log.info("窗口内无比赛，回退展示最近比赛日 %s（%d 场）",
                             nearest, len(matches))

        if not matches:
            return []
        return [self._to_fixture(m, league_id, season) for m in matches]

    async def probe(self, league_id: int) -> dict:
        """连通性诊断：真实请求一次备用源，返回状态/条数/原始片段，供 /status 展示。

        不抛异常（除网络层外的问题也一律转成文本），便于管理员自查账号与套餐。
        """
        code = _code_for(league_id)
        result: dict[str, Any] = {"code": code}
        try:
            payload = await self._request(f"competitions/{code}/matches", {})
        except Exception as exc:  # 诊断不能影响主流程
            result.update(ok=False, detail=f"{type(exc).__name__}: {exc}")
            return result
        matches = (payload or {}).get("matches") or []
        self._record_season_range(matches)
        result.update(
            ok=bool(matches),
            count=len(matches),
            competition=((payload or {}).get("competition") or {}).get("name", "-"),
            season_range=self.season_range,
        )
        if not matches:
            # 返回空时把原始响应片段带出来，便于判断是账号限制还是真的没数据
            result["raw"] = str(payload)[:200]
        return result

    async def get_standings(self, league_id: int, season: int) -> list[dict]:
        code = _code_for(league_id)
        payload = await self._get(f"competitions/{code}/standings", {"season": season}, ttl=TTL_STANDINGS)
        return self._to_standings_rows(payload)

    async def get_team_form(self, team_id: int | str, season: int, last: int = 5) -> list[dict]:
        """某队最近 last 场已完场。免费层无专用端点，从联赛赛程里过滤。"""
        league_id = _league_of_team(team_id)
        code = _code_for(league_id)
        payload = await self._get(f"competitions/{code}/matches", {"season": season, "status": "FINISHED"}, ttl=TTL_MATCHES)
        matches = payload.get("matches") or []
        target = str(team_id)
        mine = []
        for m in matches:
            home, away = m.get("homeTeam") or {}, m.get("awayTeam") or {}
            if f"fd-{home.get('id')}" == target or f"fd-{away.get('id')}" == target:
                mine.append(self._to_fixture(m, league_id, season))
        mine.sort(key=lambda fx: (fx.get("fixture") or {}).get("date") or "", reverse=True)
        return mine[:last]

    async def get_h2h(self, home_id: int | str, away_id: int | str, last: int = 5) -> list[dict]:
        """免费层无历史交锋端点：返回空列表（上层会显示「暂无可靠数据」，不伪造）。"""
        return []

    async def get_odds(self, fixture_id: int | str, fresh: bool = False) -> list[dict]:
        """免费层无赔率：返回空列表（上层会提示「暂无赔率」）。"""
        return []

    async def get_available_seasons(self) -> list[int]:
        """当前账号可访问的赛季（从 competitions 列表的 currentSeason 推导）。"""
        payload = await self._get("competitions", {}, ttl=TTL_SEASONS)
        seasons: set[int] = set()
        for comp in payload.get("competitions") or []:
            current = (comp.get("currentSeason") or {}).get("startDate")
            if current:
                try:
                    seasons.add(int(str(current)[:4]))
                except ValueError:
                    continue
        return sorted(seasons)

    async def get_account_status(self) -> dict:
        """免费层无账户额度端点，返回最小结构供 /status 显示。"""
        return {"subscription": {"plan": "Free", "active": True}, "requests": {}}


def _half(total: dict) -> dict:
    """把 TOTAL 数据折半作为主/客场的近似（免费层只有 TOTAL 时的兜底）。"""
    goals = total.get("goals") or {}
    return {
        "played": (total.get("played") or 0) // 2,
        "win": (total.get("win") or 0) // 2,
        "draw": (total.get("draw") or 0) // 2,
        "lose": (total.get("lose") or 0) // 2,
        "goals": {"for": (goals.get("for") or 0) / 2, "against": (goals.get("against") or 0) / 2},
    }


def _combine(home: dict, away: dict) -> dict:
    h_goals, a_goals = home.get("goals") or {}, away.get("goals") or {}
    return {
        "played": (home.get("played") or 0) + (away.get("played") or 0),
        "win": (home.get("win") or 0) + (away.get("win") or 0),
        "draw": (home.get("draw") or 0) + (away.get("draw") or 0),
        "lose": (home.get("lose") or 0) + (away.get("lose") or 0),
        "goals": {
            "for": (h_goals.get("for") or 0) + (a_goals.get("for") or 0),
            "against": (h_goals.get("against") or 0) + (a_goals.get("against") or 0),
        },
    }


def _league_of_team(team_id) -> int:
    """备用源的 team_id 形如 'fd-<id>'，无法反推联赛；按配置的默认联赛处理。

    说明：football-data.org 的 team id 不含联赛信息，这里返回 39（英超）作为默认，
    由调用方保证传入的 team 属于当前配置的联赛。
    """
    return 39
