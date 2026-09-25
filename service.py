"""业务层：把赛程 / 积分榜 / 赔率拼成预测，并保存最近的预测供按钮回调使用。"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from analyzer import LeagueModel, MatchAnalyzer, TeamStrength, build_league_model, collect_1x2_odds, consensus_odds
from api_client import APIError, FootballAPI
from config import Settings

log = logging.getLogger(__name__)

STORE_LIMIT = 100  # 内存里最多保留的预测条数（机器人重启后清空）
LOW_SAMPLE_GAMES = 5  # 主/客场已赛场次低于此值时提示样本不足
MODEL_VERSION = "poisson-v0.1"  # 展示给用户，便于判断结论来自哪套模型


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
        self._store: OrderedDict[int, Prediction] = OrderedDict()

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

        # SEASON 变量过期（例如仍是 2025）时，自动改用按日期推算出的赛季再试一次
        seasons = [s.season] + ([s.expected_season] if s.expected_season != s.season else [])
        upcoming: list[tuple[datetime, dict]] = []
        for season in seasons:
            fixtures = await self.api.get_fixtures(s.league_id, season, now.date(), end.date())
            upcoming = self._upcoming(fixtures, now, end)
            if upcoming:
                if season != s.season:
                    log.warning("SEASON=%s 下没有赛程，已改用 %s 赛季，请更新 SEASON 变量", s.season, season)
                self.season_in_use = season
                break
        upcoming = upcoming[: limit or s.max_matches]
        if not upcoming:
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
        fixture_id = fx["fixture"]["id"]

        analysis = self.analyzer.predict_match(model, home["id"], away["id"])
        try:
            odds_response = await self.api.get_odds(fixture_id, fresh=fresh)
        except APIError as exc:  # 赔率缺失不应阻断整条预测
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
            fixture=fx,
            bookmakers=bookmakers,
            odds=odds,
            outcomes=outcomes,
            best=best,
        )

    # ---- 今日赛程 ---------------------------------------------------------------
    def _season_candidates(self) -> list[int]:
        """尝试赛季的顺序：配置的 SEASON 优先，其次按日期推算出的赛季。"""
        s = self.settings
        return [s.season] + ([s.expected_season] if s.expected_season != s.season else [])

    async def get_today_fixtures(self, now: datetime | None = None) -> list[dict]:
        """取「今天」（按 TIMEZONE，默认 Asia/Shanghai）的全部赛程。

        与 build_predictions 不同：这里不限制未来窗口，已开赛 / 已完场的比赛也要列出。
        取不到时抛出 APIError，让上层展示真实原因（套餐 / 赛季 / 网络），
        绝不能把权限错误伪装成「今天没有比赛」。
        """
        s = self.settings
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(s.timezone).date()
        first_error: APIError | None = None
        for season in self._season_candidates():
            try:
                fixtures = await self.api.get_fixtures(s.league_id, season, day, day)
            except APIError as exc:
                if first_error is None:
                    first_error = exc
                continue
            if fixtures:
                if season != s.season:
                    log.warning("今日赛程：SEASON=%s 无数据，已改用 %s 赛季", s.season, season)
                self.season_in_use = season
                return list(fixtures)
        if first_error is not None:
            raise first_error
        return []

    # ---- 单场比赛预测 ----------------------------------------------------------
    async def predict_fixture(self, fixture_id: int, fixtures: list[dict] | None = None) -> Prediction:
        """为指定的一场比赛生成预测。

        fixtures 为今日赛程缓存；不传则重新拉取一次。找不到该场比赛抛 KeyError，
        API 失败抛 APIError（由上层展示真实原因）。
        """
        if fixtures is None:
            fixtures = await self.get_today_fixtures()
        fx = next((f for f in fixtures if (f.get("fixture") or {}).get("id") == fixture_id), None)
        if fx is None:
            raise KeyError(fixture_id)
        kickoff = parse_kickoff((fx.get("fixture") or {}).get("date"))
        if kickoff is None:
            raise APIError("该场比赛缺少开赛时间，无法预测")
        standings = await self.api.get_standings(self.settings.league_id, self.season_in_use)
        prediction = await self._predict_one(build_league_model(standings), kickoff, fx)
        self._remember(prediction)
        return prediction

    # ---- 存取（按钮回调用） -----------------------------------------------------
    def _remember(self, prediction: Prediction) -> None:
        self._store[prediction.fixture_id] = prediction
        self._store.move_to_end(prediction.fixture_id)
        while len(self._store) > STORE_LIMIT:
            self._store.popitem(last=False)

    def get(self, fixture_id: int) -> Prediction:
        """取不到（机器人重启过或已被淘汰）时抛 KeyError。"""
        return self._store[fixture_id]

    async def refresh(self, fixture_id: int) -> Prediction:
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
