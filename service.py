"""业务层：把赛程 / 积分榜 / 赔率拼成预测，并保存最近的预测供按钮回调使用。"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from repository import PredictionRepository
from analyzer import (
    LeagueModel,
    MatchAnalyzer,
    TeamStrength,
    build_league_model,
    calculate_prediction_level,
    collect_1x2_odds,
    consensus_odds,
)
from api_client import APIError, FootballAPI
from data_source import DataSourceError
from config import Settings

log = logging.getLogger(__name__)

STORE_LIMIT = 100  # 内存里最多保留的预测条数（机器人重启后清空）
LOW_SAMPLE_GAMES = 5  # 主/客场已赛场次低于此值时提示样本不足
MODEL_VERSION = "poisson-v0.1"  # 展示给用户，便于判断结论来自哪套模型
FORM_MATCHES = 5  # 深度分析取近期几场
H2H_MATCHES = 10  # 历史交锋取几场
SEASON_FALLBACK_STEPS = 3  # 赛季不可用时，最多再向下降级几个赛季（不硬编码具体年份）
UPCOMING_DAYS = 7

# 赛程查询模式 / Fixture query modes
MODE_TODAY = "today"        # 今日
MODE_UPCOMING = "upcoming"  # 未来 N 天
MODE_NEXT = "next"          # 下一场（不早于今天的第一场）
MODE_DATE = "date"          # 指定日期

# 查询结果状态 / Query result status
ST_OK = "ok"                  # 窗口内有比赛
ST_WINDOW_EMPTY = "window_empty"  # 接口有数据，但请求窗口内没有比赛
ST_NO_DATA = "no_data"        # 接口无数据或故障  # 今日无比赛时，自动向前查找的天数（避免"今天没比赛"就显示空白）

_FINISHED = {"FT", "AET", "PEN"}  # 已完场的状态缩写


def is_season_error(exc: BaseException) -> bool:
    """判断是否为「该赛季不可用」（套餐权限 / 赛季未开放），而不是 Key 无效或限流。"""
    text = str(exc).lower()
    if not text:
        return False
    return ("do not have access to this season" in text) or ("season" in text and "plan" in text)


def _is_finished(m: dict) -> bool:
    """只统计已完场的比赛；状态字段缺失时按已完场处理（兼容精简数据）。"""
    short = ((m.get("fixture") or {}).get("status") or {}).get("short")
    return True if not short else short in _FINISHED


def _side(m: dict, team_id: int) -> tuple[int | None, int | None, bool]:
    """返回 (本队进球, 对手进球, 是否主场)；数据不全时返回 (None, None, ...)。"""
    teams, goals = m.get("teams") or {}, m.get("goals") or {}
    hg, ag = goals.get("home"), goals.get("away")
    if hg is None or ag is None:
        return None, None, False
    is_home = (teams.get("home") or {}).get("id") == team_id
    return (hg if is_home else ag), (ag if is_home else hg), is_home


def form_stats(matches: list[dict], team_id: int) -> dict:
    """近期战绩统计：胜平负、进失球、主客场表现。数据不足时 played=0，由上层显示“暂无”。"""
    rows, w = [], {"win": 0, "draw": 0, "lose": 0}
    home, away = {"win": 0, "draw": 0, "lose": 0}, {"win": 0, "draw": 0, "lose": 0}
    gf = ga = 0
    for m in matches or []:
        if not _is_finished(m):  # 未开赛/进行中不算进“近期状态”（状态缺失时按已完场处理）
            continue
        our, their, is_home = _side(m, team_id)
        if our is None:
            continue
        gf, ga = gf + our, ga + their
        key = "win" if our > their else "draw" if our == their else "lose"
        w[key] += 1
        (home if is_home else away)[key] += 1
        teams = m.get("teams") or {}
        rows.append(
            {
                "result": key,
                "score": f"{our}-{their}",
                "is_home": is_home,
                "opponent": ((teams.get("away") if is_home else teams.get("home")) or {}).get("name") or "?",
                "date": (m.get("fixture") or {}).get("date"),
            }
        )
    played = w["win"] + w["draw"] + w["lose"]
    return {
        "played": played,
        "win": w["win"],
        "draw": w["draw"],
        "lose": w["lose"],
        "goals_for": gf,
        "goals_against": ga,
        "avg_for": gf / played if played else None,
        "avg_against": ga / played if played else None,
        "home": home,
        "away": away,
        "matches": rows,
    }


def h2h_stats(matches: list[dict], home_id: int) -> dict:
    """历史交锋统计（以主队视角）。"""
    finished = []
    win = draw = loss = gf = ga = 0
    for m in matches or []:
        if not _is_finished(m):
            continue
        our, their, _ = _side(m, home_id)
        if our is None:
            continue
        gf, ga = gf + our, ga + their
        if our > their:
            win += 1
        elif our == their:
            draw += 1
        else:
            loss += 1
        finished.append(m)
    played = win + draw + loss
    return {
        "played": played,
        "win": win,
        "draw": draw,
        "lose": loss,
        "goals_for": gf,
        "goals_against": ga,
        "matches": finished,
    }


@dataclass
class Prediction:
    fixture_id: int
    league: str
    round_label: str
    venue: str
    home: str
    away: str
    home_id: int
    away_id: int
    kickoff: datetime
    analysis: dict
    home_strength: TeamStrength
    away_strength: TeamStrength
    model: LeagueModel
    fixture: dict
    bookmakers: list[dict] = field(default_factory=list)
    odds: dict | None = None
    outcomes: dict = field(default_factory=dict)
    best: tuple[str, dict] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "API-Football"
    # 输入数据快照：预测可追溯，事后能回答「这个结论是基于什么算出来的」
    inputs: dict = field(default_factory=dict)
    season: int = 0  # 实际使用的赛季（降级后可能不同于配置的赛季），展示给用户  # 实际使用的数据源（主源或备用源），必须如实展示

    @property
    def low_sample(self) -> bool:
        return min(self.home_strength.games_home, self.away_strength.games_away) < LOW_SAMPLE_GAMES

    @property
    def has_team_data(self) -> bool:
        """积分榜里是否真的有这两支球队（False 表示只能按联赛平均估算）。"""
        return bool(self.model.teams) and self.home_id in self.model.teams and self.away_id in self.model.teams

    @property
    def data_completeness(self) -> str:
        if not self.model.teams:
            return "无数据"
        if not self.has_team_data:
            return "无数据"
        return "部分" if self.low_sample else "完整"

    @property
    def level(self) -> dict:
        """信心等级：只由概率计算，不允许手工指定。"""
        return calculate_prediction_level({
            "home_win": self.analysis["win_prob"],
            "draw": self.analysis["draw_prob"],
            "away_win": self.analysis["loss_prob"],
        })

    @property
    def model_version(self) -> str:
        """模型版本：结论可追溯，不同版本的结果不能直接比较。"""
        return self.inputs.get("model_version") or MODEL_VERSION

    @property
    def insufficient(self) -> bool:
        """数据不足：积分榜缺失或不含这两队 → 只能按联赛平均估算，结论不可信。

        此时必须明确提示用户，绝不能给出确定性的结论。
        """
        return not self.has_team_data

    @property
    def prob_sum(self) -> float:
        return self.analysis["win_prob"] + self.analysis["draw_prob"] + self.analysis["loss_prob"]


def parse_kickoff(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def round_label(raw: str | None) -> str:
    """'Regular Season - 6' → '第 6 轮'；杯赛等其它轮次原样返回。"""
    raw = (raw or "").strip()
    m = re.search(r"regular season\s*-\s*(\d+)", raw, re.IGNORECASE)
    return f"第 {m.group(1)} 轮" if m else raw


class PredictionService:
    def __init__(self, settings: Settings, api: FootballAPI, analyzer: MatchAnalyzer | None = None) -> None:
        self.settings = settings
        self.api = api
        self.analyzer = analyzer or MatchAnalyzer()
        self.season_in_use: int = settings.season
        self.using_upcoming: bool = False  # 当前赛程是否为「今日无比赛 → 扩展到未来」的结果
        self.fixture_day_label: str = ""  # 赛程实际覆盖的日期范围，供标题显示
        self.last_note: str | None = None  # 最近一次操作的降级/空结果提示，由上层展示给用户
        self._store: OrderedDict[int, Prediction] = OrderedDict()
        # 预测落盘到机器人自身存储（SQLite），重启不丢，支撑命中率统计
        self.repo: PredictionRepository = PredictionRepository(getattr(settings, "db_path", None))
        # 赛程本地缓存：外部 API 只负责拉取，查询优先读本地库
        from sync import MatchSync

        self.sync: MatchSync = MatchSync(self, self.repo)
        # 本地优先开关：默认开启；关掉则退回每次回源（排障用）
        self.local_first: bool = True

    @property
    def source_label(self) -> str:
        """当前实际使用的数据源名称（主源 API-Football / 备用源 football-data.org）。"""
        return getattr(self.api, "source_label", "API-Football")

    # ---- 预测 ---------------------------------------------------------------
    @staticmethod
    def _upcoming(fixtures: list[dict], now: datetime, end: datetime) -> list[tuple[datetime, dict]]:
        result = []
        for fx in fixtures:
            info = fx.get("fixture") or {}
            kickoff = parse_kickoff(info.get("date"))
            status = (info.get("status") or {}).get("short")
            if kickoff and status == "NS" and now <= kickoff <= end:
                result.append((kickoff, fx))
        result.sort(key=lambda item: item[0])
        return result

    async def build_predictions(
        self, *, lookahead_hours: int | None = None, limit: int | None = None, now: datetime | None = None
    ) -> list[Prediction]:
        """取未来 lookahead_hours 小时内尚未开赛的比赛，按开赛时间排序后生成预测。"""
        s = self.settings
        now = now or datetime.now(timezone.utc)
        end = now + timedelta(hours=lookahead_hours or s.lookahead_hours)

        # 按候选赛季探测：SEASON 过期或套餐不支持当前赛季时，自动向下降级
        fixtures, season, note = await self._fetch_fixtures(now.date(), end.date())
        upcoming = self._upcoming(fixtures, now, end)
        upcoming = upcoming[: limit or s.max_matches]
        if not upcoming:
            # 降级成功但旧赛季没有「未来」的比赛：必须说清原因，不能报成「今天没比赛」
            self.last_note = note or (
                f"ℹ️ {season} 赛季在 {s.lookahead_hours} 小时窗口内没有未开赛的比赛。"
                if season != s.season
                else None
            )
            return []

        model = build_league_model(await self.api.get_standings(s.league_id, self.season_in_use))
        predictions = []
        for kickoff, fx in upcoming:
            prediction = await self._predict_one(model, kickoff, fx)
            self._remember(prediction)
            predictions.append(prediction)
        return predictions

    async def _predict_one(self, model: LeagueModel, kickoff: datetime, fx: dict, fresh: bool = False) -> Prediction:
        teams = fx.get("teams") or {}
        home, away = teams.get("home") or {}, teams.get("away") or {}
        fixture_id = fx["fixture"]["id"]  # 保持数据源原类型；_store 内部统一用 str key 查找

        analysis = self.analyzer.predict_match(model, home["id"], away["id"])
        try:
            odds_response = await self.api.get_odds(fixture_id, fresh=fresh)
        except (APIError, DataSourceError) as exc:  # 赔率缺失不应阻断整条预测
            log.warning("赔率获取失败（fixture=%s）：%s", fixture_id, exc)
            odds_response = []
        bookmakers = collect_1x2_odds(odds_response)
        odds = consensus_odds(bookmakers)
        outcomes = self.analyzer.evaluate_outcomes(analysis, odds)
        best = max(outcomes.items(), key=lambda kv: kv[1]["edge"]) if outcomes else None
        league = fx.get("league") or {}

        return Prediction(
            fixture_id=fixture_id,
            league=league.get("name") or f"联赛 {self.settings.league_id}",
            round_label=round_label(league.get("round")),
            venue=((fx["fixture"].get("venue") or {}).get("name") or ""),
            home=home.get("name", "?"),
            away=away.get("name", "?"),
            home_id=home["id"],
            away_id=away["id"],
            kickoff=kickoff,
            analysis=analysis,
            home_strength=model.strength(home["id"]),
            away_strength=model.strength(away["id"]),
            model=model,
            inputs={
                "model_version": MODEL_VERSION,
                "source": self.source_label,
                "season": self.season_in_use,
                "league_id": self.settings.league_id,
                "standings_rows": len(getattr(model, "teams", {}) or {}),
                "has_home_data": home["id"] in (getattr(model, "teams", {}) or {}),
                "has_away_data": away["id"] in (getattr(model, "teams", {}) or {}),
                "odds_count": len(bookmakers),
                "kickoff": kickoff.isoformat() if kickoff else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            fixture=fx,
            bookmakers=bookmakers,
            odds=odds,
            source=self.source_label,
            outcomes=outcomes,
            best=best,
            season=self.season_in_use,
        )

    # ---- 赛季探测与降级 ---------------------------------------------------------
    async def resolve_season(self) -> tuple[int, bool]:
        """确定实际使用的赛季，返回 (赛季, 是否发生了降级)。

        先查账号可用赛季列表，再按「目标赛季 → 小于目标的最近可用赛季」排序；
        列表不可用（接口报错/为空）时回退为逐年向下降级。
        不硬编码任何年份，也不伪造赛季数据。
        """
        s = self.settings
        requested = s.season
        if not s.allow_season_fallback or s.season_mode == "fixed":
            return requested, False

        available: list[int] = []
        try:
            available = sorted({int(x) for x in (await self.api.get_available_seasons() or [])})
        except Exception as exc:  # 列表接口失败不阻断，退回逐年降级
            log.warning("获取可用赛季列表失败（%s），改用逐年降级", type(exc).__name__)

        if available:
            ordered: list[int] = []
            if requested in available:
                ordered.append(requested)
            # 小于目标赛季的最近几个可用赛季，由近及远
            below = [x for x in sorted(available, reverse=True) if x < requested]
            ordered.extend(below[:SEASON_FALLBACK_STEPS])
            if not ordered:  # 列表里没有也不小于目标的赛季：至少试一次目标赛季
                ordered = [requested]
            return ordered[0], ordered[0] != requested

        # 列表不可用：按日期推算 + 逐年降级
        tail = s.expected_season if s.expected_season != requested else requested
        return tail, tail != requested

    def _season_candidates(self) -> list[int]:
        """实际尝试赛季的顺序（用于逐个请求验证，因为列表可能含无权访问的赛季）。"""
        s = self.settings
        requested = s.season
        if not s.allow_season_fallback or s.season_mode == "fixed":
            return [requested]
        base = [requested]
        if s.expected_season != requested:
            base.append(s.expected_season)
        tail = base[-1]
        base.extend(tail - i for i in range(1, SEASON_FALLBACK_STEPS + 1))
        seen, out = set(), []
        for season in base:
            if season not in seen:
                seen.add(season)
                out.append(season)
        return out

    async def _fetch_fixtures(self, date_from, date_to) -> tuple[list[dict], int, str | None]:
        """按候选赛季依次请求赛程，遇到「赛季不可用」就自动降级。

        返回 (赛程, 实际使用的赛季, 降级提示)。Key 无效 / 限流这类错误直接抛出，
        不做无意义的重试与降级。
        """
        s = self.settings
        first_error: APIError | None = None
        for season in self._season_candidates():
            try:
                fixtures = await self.api.get_fixtures(s.league_id, season, date_from, date_to)
            except APIError as exc:
                if not is_season_error(exc):
                    raise  # 非赛季问题（Key/限流/网络）不降级，直接暴露真实原因
                if first_error is None:
                    first_error = exc
                log.warning("赛季 %s 不可用（%s），尝试向下降级", season, exc)
                continue
            if fixtures:
                self.season_in_use = season
                note = None
                if season != s.season:
                    note = (
                        f"⚠️ 已自动降级到 {season} 赛季（配置的 {s.season} 赛季不可用）。"
                        f"该赛季为历史数据，无法提供未来赛程预测。"
                    )
                return list(fixtures), season, note
        if first_error is not None:
            raise first_error
        return [], s.season, None

    def _season_range(self) -> tuple | None:
        """当前数据源记录的赛季日期范围（最早 / 最晚比赛日），用于排查窗口命中情况。"""
        fb = getattr(self.api, "fallback", None)
        return getattr(fb, "season_range", None) if fb else None

    def _limit_to_next(self, fixtures: list[dict], now: datetime, tz, result: dict) -> list[dict]:
        """MODE_NEXT：只保留不早于当前时刻的第一场，并把标题日期改成该场日期。"""
        upcoming = [fx for fx in fixtures
                    if (parse_kickoff((fx.get("fixture") or {}).get("date")) or now) >= now]
        upcoming.sort(key=lambda fx: parse_kickoff((fx.get("fixture") or {}).get("date")) or now)
        picked = upcoming[:1]
        if picked:
            k = parse_kickoff((picked[0].get("fixture") or {}).get("date"))
            if k:
                self.fixture_day_label = k.astimezone(tz).date().isoformat()
                result["day_label"] = self.fixture_day_label
        return picked

    async def query_fixtures(self, mode: str = MODE_TODAY, target: date | None = None,
                             now: datetime | None = None) -> dict:
        """统一赛程查询入口：今日 / 未来 N 天 / 下一场 / 指定日期。

        明确区分三种状态，避免把「接口无数据」和「窗口内没比赛」混为一谈：
        - ok          请求窗口内有比赛
        - window_empty 接口有数据（返回整季赛程），但请求窗口内没有
        - no_data     接口无数据或故障（保留真实原因）
        """
        s = self.settings
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(s.timezone).date()

        if mode == MODE_DATE:
            day = target or today
            date_from = date_to = day
        elif mode == MODE_NEXT:
            date_from, date_to = today, today + timedelta(days=365)
        elif mode == MODE_UPCOMING:
            date_from, date_to = today, today + timedelta(days=UPCOMING_DAYS)
        else:
            date_from = date_to = today

        self.using_upcoming = mode in (MODE_UPCOMING, MODE_NEXT)
        self.fixture_day_label = (
            f"{date_from.isoformat()} ~ {date_to.isoformat()}" if date_from != date_to
            else date_from.isoformat()
        )

        result = {
            "mode": mode, "status": ST_OK, "fixtures": [],
            "season": self.season_in_use, "day_label": self.fixture_day_label,
            "season_range": None, "note": None,
        }

        # 本地优先：先读 SQLite，命中就不再打外部接口
        # （外部 API 只负责拉取并落盘，Telegram / 预测只读本地库）
        if self.local_first:
            try:
                cached = self.repo.load_matches(
                    self.sync.competition, date_from.isoformat(), date_to.isoformat()
                )
            except Exception:
                cached = []
            if cached:
                result["fixtures"] = cached
                result["from_cache"] = True
                result["season_range"] = self._season_range()
                if mode == MODE_NEXT:
                    cached = self._limit_to_next(cached, now, s.timezone, result)
                    result["fixtures"] = cached
                    if not cached:
                        result["status"] = ST_WINDOW_EMPTY
                        result["note"] = "ℹ️ 本地缓存中没有未来的比赛，可尝试「刷新数据」。"
                        self.last_note = result["note"]
                        return result
                self.last_note = None
                return result

        try:
            fixtures, season, note = await self._fetch_fixtures(date_from, date_to)
        except APIError as exc:
            # 接口故障 / 权限问题：保留真实原因与原始异常（供上层翻译成用户可读文案）
            result.update(status=ST_NO_DATA, note=str(exc), error=exc)
            self.last_note = str(exc)
            return result

        # 拉取成功即落盘，后续同窗口查询直接读本地，不再重复请求
        if fixtures:
            try:
                self.repo.save_matches(
                    self.sync.competition, fixtures,
                    source=self.source_label, http_status="200", message=f"query:{mode}",
                )
            except Exception as exc:  # 落盘失败不影响本次展示
                log.warning("赛程落盘失败：%s", exc)

        result["season"] = season
        result["season_range"] = self._season_range()

        # 过滤前先记下接口实际返回了多少场：
        # 「下一场」过滤掉已开赛的比赛后为空 ≠ 数据源无数据，
        # 否则会把「今天两场都踢完了」误报成「数据源未返回赛程」。
        raw_count = len(fixtures or [])

        if mode == MODE_NEXT and fixtures:
            # 只看不早于当前时刻的第一场
            upcoming = [fx for fx in fixtures
                        if (parse_kickoff((fx.get("fixture") or {}).get("date")) or now) >= now]
            upcoming.sort(key=lambda fx: parse_kickoff((fx.get("fixture") or {}).get("date")) or now)
            fixtures = upcoming[:1]
            if fixtures:
                k = parse_kickoff((fixtures[0].get("fixture") or {}).get("date"))
                self.fixture_day_label = k.astimezone(s.timezone).date().isoformat()
                result["day_label"] = self.fixture_day_label

        fb = getattr(self.api, "fallback", None)
        if fixtures:
            result["fixtures"] = fixtures
            if fb and getattr(fb, "last_shifted_date", None):
                result["status"] = ST_WINDOW_EMPTY
                result["note"] = f"ℹ️ {fb.last_note}"
            self.last_note = result["note"]
            return result

        # 无比赛：区分「接口没数据」还是「窗口内没比赛」
        if raw_count and mode == MODE_NEXT:
            # 接口确实返回了比赛，只是都已开赛 —— 属于窗口内没比赛，不是故障
            result["status"] = ST_WINDOW_EMPTY
            result["note"] = (
                f"ℹ️ 已获取 {raw_count} 场赛程，但没有晚于当前时刻的比赛，"
                f"可尝试「未来 7 天」或指定日期。"
            )
            self.last_note = result["note"]
            return result

        # 能走到这里说明请求已经成功（HTTP 200），只是窗口内 0 场。
        # 真正的故障（HTTP 错误 / 超时 / 解析失败）已在上面 except 返回 no_data，
        # 所以此处一律是「当前时间范围内暂无比赛」，不能说成「数据源无数据」。
        result["status"] = ST_WINDOW_EMPTY
        span = result["season_range"]
        if span:
            detail = f"该赛季数据范围 {span[0]} ~ {span[1]}"
            result["note"] = (
                f"ℹ️ {date_from} ~ {date_to} 内没有比赛。{detail}，"
                f"可尝试「下一场」或指定其它日期。"
            )
        else:
            result["note"] = (
                f"ℹ️ 当前时间范围内暂无比赛（{date_from} ~ {date_to}），"
                f"可尝试「下一场」或指定其它日期。"
            )
        self.last_note = result["note"]
        if result["note"]:
            log.warning("赛程查询[%s]：%s", mode, result["note"])
        return result

    async def get_today_fixtures(self, now: datetime | None = None) -> list[dict]:
        """取「今天」（按 TIMEZONE，默认 Asia/Shanghai）的全部赛程。

        与 build_predictions 不同：这里不限制未来窗口，已开赛 / 已完场的比赛也要列出。
        取不到时抛出 APIError，让上层展示真实原因（套餐 / 赛季 / 网络），
        绝不能把权限错误伪装成「今天没有比赛」。
        """
        s = self.settings
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(s.timezone).date()
        self.using_upcoming = False
        self.fixture_day_label = day.isoformat()

        fixtures, season, note = await self._fetch_fixtures(day, day)
        if not fixtures:
            # 今日无比赛：自动扩展到未来 N 天，避免「今天没比赛」就给用户一片空白。
            # 注意区分：这里是「正常无数据」，仍不能把权限/故障错误伪装成空。
            end = day + timedelta(days=UPCOMING_DAYS)
            fixtures, season, note = await self._fetch_fixtures(day, end)
            if fixtures:
                self.using_upcoming = True
                self.fixture_day_label = f"{day.isoformat()} ~ {end.isoformat()}"
                fixtures = sorted(
                    fixtures,
                    key=lambda fx: (parse_kickoff((fx.get("fixture") or {}).get("date")) or now),
                )
                note = (
                    f"ℹ️ 今日（{day.isoformat()}）暂无比赛，"
                    f"已自动展示未来 {UPCOMING_DAYS} 天内的赛程。"
                )
        # 备用源把数据「回退到最近比赛日」时，把真实日期带出来，别只说「暂无比赛」
        fb = getattr(self.api, "fallback", None)
        if fixtures and fb and getattr(fb, "last_shifted_date", None):
            shifted = fb.last_shifted_date
            self.fixture_day_label = shifted.isoformat()
            self.using_upcoming = False
            note = f"ℹ️ {fb.last_note}"

        if not fixtures and getattr(self.api, "using_fallback", False):
            # 备用源正常响应但今日无比赛：如实说明，不伪装成主源的权限错误，
            # 也不把「今天没比赛」说成数据源故障。
            primary_err = self.api.last_error("api-football") if hasattr(self.api, "last_error") else None
            note = (
                f"ℹ️ 主数据源当前赛季不可用，已切换到备用数据源 {self.source_label}；近期暂无比赛。"
                if primary_err
                else f"ℹ️ 当前使用备用数据源 {self.source_label}；近期暂无比赛。"
            )
        self.last_note = note
        if note:
            log.warning("今日赛程：%s", note)
        return fixtures

    # ---- 单场比赛预测 ----------------------------------------------------------
    async def predict_fixture(self, fixture_id: int, fixtures: list[dict] | None = None) -> Prediction:
        """为指定的一场比赛生成预测。

        fixtures 为今日赛程缓存；不传则重新拉取一次。找不到该场比赛抛 KeyError，
        API 失败抛 APIError（由上层展示真实原因）。
        """
        if fixtures is None:
            fixtures = await self.get_today_fixtures()
        fx = next((f for f in fixtures if str((f.get("fixture") or {}).get("id")) == str(fixture_id)), None)
        if fx is None:
            raise KeyError(fixture_id)
        # 无比赛数据时禁止生成预测：缺开赛时间或队伍信息的比赛，预测无从谈起
        kickoff = parse_kickoff((fx.get("fixture") or {}).get("date"))
        if kickoff is None:
            raise APIError("该场比赛缺少开赛时间，无法预测")
        teams = fx.get("teams") or {}
        if not (teams.get("home") or {}).get("id") or not (teams.get("away") or {}).get("id"):
            raise APIError("该场比赛缺少参赛队伍信息，无法预测")
        standings = await self.api.get_standings(self.settings.league_id, self.season_in_use)
        prediction = await self._predict_one(build_league_model(standings), kickoff, fx)
        self._remember(prediction)
        return prediction

    async def analyze_fixture(self, fixture_id: int, fixtures: list[dict] | None = None) -> dict:
        """深度分析报告：近期状态 + 联赛排名 + 历史交锋 + 模型因素。

        积分榜是必需数据，取不到就抛 APIError，由上层显示真实原因；
        近期状态与历史交锋属于可选数据，失败时降级为「暂无可靠数据」并附上真实原因，
        绝不把权限错误伪装成「没有比赛 / 没有数据」。
        """
        if fixtures is None:
            fixtures = await self.get_today_fixtures()
        fx = next((f for f in fixtures if str((f.get("fixture") or {}).get("id")) == str(fixture_id)), None)
        if fx is None:
            raise KeyError(fixture_id)
        kickoff = parse_kickoff((fx.get("fixture") or {}).get("date"))
        if kickoff is None:
            raise APIError("该场比赛缺少开赛时间，无法分析")
        s = self.settings
        teams = fx.get("teams") or {}
        home_id = (teams.get("home") or {}).get("id")
        away_id = (teams.get("away") or {}).get("id")
        season = self.season_in_use

        standings = await self.api.get_standings(s.league_id, season)  # 必需：失败直接抛真实原因
        model = build_league_model(standings)
        rows_by_team = {(r.get("team") or {}).get("id"): r for r in standings}

        # 可选数据并发取，单点失败不阻断整体，但保留真实原因
        results = await asyncio.gather(
            self.api.get_team_form(home_id, season, FORM_MATCHES),
            self.api.get_team_form(away_id, season, FORM_MATCHES),
            self.api.get_h2h(home_id, away_id, H2H_MATCHES),
            return_exceptions=True,
        )
        errors: dict[str, str | None] = {}
        for key, value in zip(("home_form", "away_form", "h2h"), results):
            errors[key] = str(value) if isinstance(value, BaseException) else None
        home_raw = [] if isinstance(results[0], BaseException) else results[0]
        away_raw = [] if isinstance(results[1], BaseException) else results[1]
        h2h_raw = [] if isinstance(results[2], BaseException) else results[2]

        return {
            "fixture_id": fixture_id,
            "league": (fx.get("league") or {}).get("name") or f"联赛 {s.league_id}",
            "home": (teams.get("home") or {}).get("name") or "?",
            "away": (teams.get("away") or {}).get("name") or "?",
            "home_id": home_id,
            "away_id": away_id,
            "kickoff": kickoff,
            "home_form": form_stats(home_raw, home_id),
            "away_form": form_stats(away_raw, away_id),
            "home_row": rows_by_team.get(home_id),
            "away_row": rows_by_team.get(away_id),
            "h2h": h2h_stats(h2h_raw, home_id),
            "model": {
                "home_strength": model.strength(home_id),
                "away_strength": model.strength(away_id),
                "analysis": self.analyzer.predict_match(model, home_id, away_id),
                "avg_home_goals": model.avg_home_goals,
                "avg_away_goals": model.avg_away_goals,
            },
            "errors": errors,
            "source": self.source_label,
            "has_team_data": bool(model.teams) and home_id in model.teams and away_id in model.teams,
            "created_at": datetime.now(timezone.utc),
        }

    async def get_standings_page(self, limit: int = 20) -> list[dict]:
        """积分榜（联赛排名），取不到时抛 APIError 显示真实原因。"""
        return await self.api.get_standings(self.settings.league_id, self.season_in_use)

    # ---- 存取（按钮回调用） -----------------------------------------------------
    def settle_result(self, fixture_id, home_score: int | None, away_score: int | None) -> bool:
        """回写真实赛果，供命中率统计使用。"""
        return self.repo.settle(fixture_id, home_score, away_score)

    async def sync_results(self, fixtures: list[dict] | None = None) -> int:
        """把已完场比赛的真实比分回写到数据库。返回本次结算条数。

        只处理库里已有预测、但还没回填比分的比赛；取不到赛程时不报错。
        """
        pending = self.repo.pending()
        if not pending:
            return 0
        if fixtures is None:
            try:
                fixtures = await self.get_today_fixtures()
            except Exception as exc:  # 取不到赛程只是暂时无法结算，不影响机器人
                log.warning("同步赛果时获取赛程失败：%s", exc)
                return 0
        by_id = {str((fx.get("fixture") or {}).get("id")): fx for fx in fixtures}
        done = 0
        for row in pending:
            fx = by_id.get(str(row["fixture_id"]))
            if not fx:
                continue
            short = ((fx.get("fixture") or {}).get("status") or {}).get("short")
            if short not in _FINISHED:
                continue
            goals = fx.get("goals") or {}
            home, away = goals.get("home"), goals.get("away")
            if home is None or away is None:
                continue
            if self.repo.settle(row["fixture_id"], int(home), int(away)):
                done += 1
        if done:
            log.info("已回写 %d 场赛果", done)
        return done

    def stats(self) -> dict:
        """命中率统计（含各信心等级）。"""
        return self.repo.stats()

    def _remember(self, prediction: Prediction) -> None:
        key = str(prediction.fixture_id)  # 主源数字 ID 与备用源 'fd-' ID 统一为字符串键
        self._store[key] = prediction
        self._store.move_to_end(key)
        self.repo.save(prediction)  # 落盘：机器人重启后仍可统计命中率
        while len(self._store) > STORE_LIMIT:
            self._store.popitem(last=False)

    def get(self, fixture_id) -> Prediction:
        """取不到（机器人重启过或已被淘汰）时抛 KeyError。"""
        return self._store[str(fixture_id)]

    async def refresh(self, fixture_id) -> Prediction:
        """重新拉取最新赔率并重算价值偏差（球队强度沿用当时的模型）。"""
        old = self.get(fixture_id)
        new = await self._predict_one(old.model, old.kickoff, old.fixture, fresh=True)
        self._remember(new)
        return new

    async def get_h2h(self, prediction: Prediction, last: int = 5) -> list[dict]:
        return await self.api.get_h2h(prediction.home_id, prediction.away_id, last)

    @property
    def cached_predictions(self) -> int:
        return len(self._store)
