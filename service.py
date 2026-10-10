"""业务层：把赛程 / 积分榜 / 赔率拼成预测，并保存最近的预测供按钮回调使用。"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from repository import PredictionRepository
from analyzer import (
    LeagueModel,
    MatchAnalyzer,
    TeamStrength,
    apply_market_adjustment,
    build_league_model,
    calculate_prediction_level,
    collect_1x2_odds,
    consensus_odds,
)
from api_client import APIError, FootballAPI
from data_source import DataSourceError
from config import Settings
from templates import LEAGUE_ORDER

log = logging.getLogger(__name__)

STORE_LIMIT = 100  # 内存里最多保留的预测条数（机器人重启后清空）
LOW_SAMPLE_GAMES = 5  # 主/客场已赛场次低于此值时提示样本不足
MODEL_VERSION = "poisson-v0.1"  # 展示给用户，便于判断结论来自哪套模型
FORM_MATCHES = 5  # 深度分析取近期几场
H2H_MATCHES = 10  # 历史交锋取几场
SEASON_FALLBACK_STEPS = 3  # 赛季不可用时，最多再向下降级几个赛季（不硬编码具体年份）
UPCOMING_DAYS = 7

# 未开赛状态：只认 NS 会漏掉 TBD（时间待定），赛程页显示了它预测却没有，
# 用户就会看到「赛程 4 场、预测 3 场」这种对不上的现象。
PRE_MATCH_STATUS = frozenset({"NS", "TBD"})

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


async def _no_injuries() -> list[dict]:
    """数据源未提供伤停接口时的空实现（保持 gather 结构不变）。"""
    return []


def injuries_stats(raw: list[dict], home_id: int, away_id: int) -> dict:
    """伤停名单按主客队分组。

    只做整理，不做判断：名单为空时上层显示「暂无伤停信息」，
    不伪造、也不据此调整模型（当前模型未使用伤停信号）。
    """
    out: dict = {"home": [], "away": []}
    for item in raw or []:
        player = item.get("player") or {}
        team = item.get("team") or {}
        name = player.get("name") or "?"
        reason = player.get("reason") or player.get("type") or ""
        entry = f"{name}（{reason}）" if reason else name
        tid = str(team.get("id"))
        if tid == str(home_id):
            out["home"].append(entry)
        elif tid == str(away_id):
            out["away"].append(entry)
    return out


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
        # 每个联赛各自解析出的赛季：多联赛下各联赛可用赛季未必相同，
        # 建模必须用「该场比赛所属联赛」的赛季，不能统一套主联赛的。
        self._season_by_league: dict[int, int] = {}
        self.using_upcoming: bool = False  # 当前赛程是否为「今日无比赛 → 扩展到未来」的结果
        # 配置联赛当天休战、改用国家队赛事兜底（国际比赛日）
        self.using_national_fallback: bool = False
        self.fixture_day_label: str = ""  # 赛程实际覆盖的日期范围，供标题显示
        self.last_note: str | None = None  # 最近一次操作的降级/空结果提示，由上层展示给用户
        self.truncated_count: int = 0  # 最近一次预测被上限砍掉的场数（0 表示未截断）
        self._store: OrderedDict[int, Prediction] = OrderedDict()
        # 预测落盘到机器人自身存储（SQLite），重启不丢，支撑命中率统计
        self.repo: PredictionRepository = PredictionRepository(getattr(settings, "db_path", None))
        # 历史库的 result 字段可能是 'pending'/NULL（旧版遗留），命中判定永远不相等，
        # 命中率会被压成 0。启动时自愈一次；幂等，正常库零改动。
        self.repo.backfill_results()
        # 概率温度校准：泊松模型系统性过度自信（高概率档实测虚高 7~11pt），
        # 会让「模型概率 − 1/赔率」被高估，把并非价值注的比赛误标成 Value Bet。
        # 温度用库内已结算样本拟合；样本不足时 fit_temperature 返回 1.0（不校准）。
        # 每次预测前刷新：随赛果积累逐步收敛，且始终只用过去的数据。
        self.temperature: float = 1.0
        self.refresh_calibration()
        # 赛程本地缓存：外部 API 只负责拉取，查询优先读本地库
        from sync import MatchSync

        self.sync: MatchSync = MatchSync(self, self.repo)
        # Elo 引擎：赛果回写后更新评分，预测时用评分修正 λ。
        # 必须在 self.sync 之后初始化——联赛标识取自 sync.competition。
        from elo import EloEngine

        self.elo: EloEngine = EloEngine(
            repo=self.repo,
            competition=getattr(self.sync, "competition", "") or str(settings.league_id),
        )
        # 每个联赛一套 Elo 引擎：评分按 competition 分区存储，
        # 西甲的赛果只能改西甲球队的评分，绝不能串到英超。
        self._elo_engines: dict[int, EloEngine] = {}
        self.elo_enabled: bool = True  # 排障时可关掉，退回纯泊松
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
            if kickoff and status in PRE_MATCH_STATUS and now <= kickoff <= end:
                result.append((kickoff, fx))
        result.sort(key=lambda item: item[0])
        return result

    def _league_id_of(self, fx: dict) -> int:
        """该场比赛真正所属的联赛 ID。

        多联赛下绝不能用主联赛 ID 代替：拿英超积分榜去给西甲比赛建模会算出错误的
        攻防强度。赛程里带联赛信息时以它为准，缺失时才回退到配置的主联赛。
        """
        raw = (fx.get("league") or {}).get("id")
        try:
            return int(raw) if raw is not None else int(self.settings.league_id)
        except (TypeError, ValueError):
            return int(self.settings.league_id)

    def _season_for(self, league_id: int) -> int:
        """该联赛解析出的赛季，未记录时回退到当前生效赛季。"""
        return self._season_by_league.get(int(league_id), self.season_in_use)

    def _elo_for(self, league_id: int) -> object:
        """取该联赛的 Elo 引擎（评分按联赛分区，避免跨联赛污染）。"""
        from elo import EloEngine

        lid = int(league_id)
        engine = self._elo_engines.get(lid)
        if engine is None:
            comp = ""
            try:
                comp = self.sync.competition_for(lid)
            except Exception:  # 同步层异常不得影响评分回写
                comp = str(lid)
            engine = EloEngine(repo=self.repo, competition=comp or str(lid))
            self._elo_engines[lid] = engine
        return engine

    async def _model_for(self, league_id: int, cache: dict[int, LeagueModel] | None = None) -> LeagueModel:
        """取该联赛的积分榜并建模。cache 用于同一批预测内复用，避免重复请求。"""
        lid = int(league_id)
        if cache is not None and lid in cache:
            return cache[lid]
        standings = await self.api.get_standings(lid, self._season_for(lid))
        prior = None
        try:
            from season_prior import known_team_ids, load_prior_strength

            prior = load_prior_strength(
                lid, self._season_for(lid), known=known_team_ids(self.repo))
        except Exception as exc:  # 先验是增强项，拿不到就退回联赛平均
            log.warning("跨赛季先验不可用（%s），退回联赛平均", exc)
            prior = None
        model = build_league_model(standings, prior_strength=prior)
        if cache is not None:
            cache[lid] = model
        return model

    @staticmethod
    def _day_window(day, tz) -> tuple[datetime, datetime]:
        """自然日 [00:00, 23:59:59] 在 UTC 下的区间。

        赛程页按自然日取数，预测若按滚动小时窗口取数，两者就不是同一批比赛——
        汇总标题会跳到别的日期（用户实测：赛程 10-04、汇总 10-05）。
        要对齐就必须用同一套窗口，这里把本地自然日换算成 UTC 区间。
        """
        start = tz.localize(datetime(day.year, day.month, day.day, 0, 0, 0))
        end = start + timedelta(days=1) - timedelta(seconds=1)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    async def build_predictions(
        self, *, lookahead_hours: int | None = None, limit: int | None = None,
        now: datetime | None = None, day=None
    ) -> list[Prediction]:
        """取未来 lookahead_hours 小时内尚未开赛的比赛，按开赛时间排序后生成预测。

        day 给定时改用「本地自然日」窗口（00:00~23:59:59），与赛程页口径一致，
        保证「今日赛程 N 场」和「今日预测汇总」是同一批比赛。
        """
        s = self.settings
        now = now or datetime.now(timezone.utc)
        if day is not None:
            start, end = self._day_window(day, s.timezone)
            now = start
        else:
            end = now + timedelta(hours=lookahead_hours or s.lookahead_hours)

        # 按候选赛季探测：SEASON 过期或套餐不支持当前赛季时，自动向下降级
        fixtures, season, note = await self._fetch_fixtures_multi(now.date(), end.date())
        upcoming = self._upcoming(fixtures, now, end)
        cap = limit or s.max_matches
        # 截断必须留痕：赛程页按自然日显示全部比赛，预测若静默砍掉一部分，
        # 用户会看到「赛程 4 场 / 预测 3 场」却无从判断是数据少了还是坏了。
        self.truncated_count = max(0, len(upcoming) - cap)
        upcoming = upcoming[:cap]
        if self.truncated_count:
            self.last_note = (
                f"ℹ️ 该时段共 {len(upcoming) + self.truncated_count} 场未开赛比赛，"
                f"本次只生成前 {len(upcoming)} 场的预测（上限 {cap} 场）。"
            )
        if not upcoming:
            # 降级成功但旧赛季没有「未来」的比赛：必须说清原因，不能报成「今天没比赛」
            self.last_note = note or (
                f"ℹ️ {season} 赛季在 {s.lookahead_hours} 小时窗口内没有未开赛的比赛。"
                if season != s.season
                else None
            )
            return []

        # 一场比赛只能用它所属联赛的积分榜建模；同一联赛的积分榜只请求一次。
        models: dict[int, LeagueModel] = {}
        predictions = []
        for kickoff, fx in upcoming:
            lid = self._league_id_of(fx)
            if lid not in models:
                models[lid] = await self._model_for(lid, models)
            prediction = await self._predict_one(models[lid], kickoff, fx)
            self._remember(prediction)
            predictions.append(prediction)
        return predictions

    async def _predict_one(self, model: LeagueModel, kickoff: datetime, fx: dict, fresh: bool = False) -> Prediction:
        teams = fx.get("teams") or {}
        home, away = teams.get("home") or {}, teams.get("away") or {}
        fixture_id = fx["fixture"]["id"]  # 保持数据源原类型；_store 内部统一用 str key 查找

        analysis = self.analyzer.predict_match(
            model, home["id"], away["id"], temperature=self.temperature,
        )
        try:
            odds_response = await self.api.get_odds(fixture_id, fresh=fresh)
        except (APIError, DataSourceError) as exc:  # 赔率缺失不应阻断整条预测
            log.warning("赔率获取失败（fixture=%s）：%s", fixture_id, exc)
            odds_response = []
        bookmakers = collect_1x2_odds(odds_response)
        odds = consensus_odds(bookmakers)
        # edge 由泊松口径计算：它是「模型 vs 市场」的差异，若先换成市场口径
        # 则 edge 恒为 -抽水，失去信息量。故两者的先后顺序不能颠倒。
        outcomes = self.analyzer.evaluate_outcomes(analysis, odds)
        # 展示概率换成市场口径（实测优于泊松：准确率 53.96% vs 50.99%，
        # log_loss 0.9698 vs 1.0053，McNemar p=0.0037，见 docs/MARKET_FUSION_EVAL.md）。
        # 无赔率时 apply_market_adjustment 原样返回，行为不变。
        analysis = apply_market_adjustment(analysis, odds)
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

    # 国家队 / 洲际赛事判定：靠 API 返回的真实字段，绝不猜联赛 ID。
    # 国际比赛日期间俱乐部联赛集体休战，只有这类赛事还有球，
    # 认不出它们就会整天显示「暂无比赛」。
    _NAT_COUNTRIES = frozenset({
        "world", "europe", "south america", "asia", "africa",
        "north central america", "oceania",
    })
    _NAT_KEYWORDS = (
        "world cup", "nations league", "qualification", "friendly",
        "copa america", "european championship", "asian cup", "africa cup",
        "confederations", "olympic", "gold cup", "euro",
    )

    @classmethod
    def _is_national(cls, fx: dict) -> bool:
        """该场是否国家队/洲际赛事（世预赛、欧国联、友谊赛等）。"""
        lg = fx.get("league") or {}
        country = str(lg.get("country") or "").strip().lower()
        if country and country in cls._NAT_COUNTRIES:
            return True
        name = str(lg.get("name") or "").strip().lower()
        return any(k in name for k in cls._NAT_KEYWORDS)

    async def _fetch_all_by_date(self, day) -> list[dict]:
        """某一天的全部赛程：一次请求拿全天，再按需筛选。

        主路径不再逐个联赛轮询——17 个联赛各带赛季降级与空结果重试，
        单次点击能放大到上百次请求，而且只要配置里的联赛当天休战
        （国际比赛日典型情况）就整体空窗。

        筛选策略：
        1. 优先保留 LEAGUE_IDS 配置的联赛；
        2. 配置联赛当天一场都没有时，回退到国家队 / 洲际赛事，
           保证「今日赛程」永远有内容可看。
        """
        try:
            fixtures = await self.api.get_fixtures_by_date(day)
        except Exception as exc:  # 按日拉取失败不致命，上层会走原有路径
            log.warning("按日拉取赛程失败（%s）：%s", day, type(exc).__name__)
            return []
        if not fixtures:
            return []
        s = self.settings
        wanted = {int(x) for x in (getattr(s, "league_ids", None) or (s.league_id,))}
        picked = [
            fx for fx in fixtures
            if int((fx.get("league") or {}).get("id") or 0) in wanted
        ]
        if picked:
            return picked
        # 配置联赛当天无球（国际比赛日）——回退国家队赛事，避免空白页
        national = [fx for fx in fixtures if self._is_national(fx)]
        if national:
            self.using_national_fallback = True
        return national

    async def _fetch_fixtures(self, date_from, date_to,
                              league_id: int | None = None) -> tuple[list[dict], int, str | None]:
        """按候选赛季依次请求赛程，遇到「赛季不可用」就自动降级。

        league_id 省略时用 settings.league_id。多联赛同步必须逐联赛调用：
        不能拿一个联赛的赛季可用性去决定另一个联赛的赛季。

        返回 (赛程, 实际使用的赛季, 降级提示)。Key 无效 / 限流这类错误直接抛出，
        不做无意义的重试与降级。
        """
        s = self.settings
        lid = s.league_id if league_id is None else int(league_id)
        first_error: APIError | None = None
        for season in self._season_candidates():
            try:
                fixtures = await self.api.get_fixtures(lid, season, date_from, date_to)
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

    async def _fetch_fixtures_multi(
        self, date_from, date_to
    ) -> tuple[list[dict], int, str | None]:
        """多联赛赛程：逐个联赛拉取后按开赛时间合并。

        每个联赛独立走自己的赛季降级逻辑，并把解析出的赛季记到 _season_by_league，
        供后续按联赛建模使用。单联赛失败不拖垮其他联赛；全部失败才抛出真实原因。

        单联赛配置（league_ids 只有一个）时，行为与直接调用 _fetch_fixtures 完全一致。
        """
        s = self.settings
        league_ids = tuple(getattr(s, "league_ids", None) or (s.league_id,))
        if len(league_ids) == 1:
            fixtures, season, note = await self._fetch_fixtures(date_from, date_to, league_id=league_ids[0])
            self._season_by_league[int(league_ids[0])] = season
            return fixtures, season, note

        results = await asyncio.gather(
            *(self._fetch_fixtures(date_from, date_to, league_id=lid) for lid in league_ids),
            return_exceptions=True,
        )
        merged: list[dict] = []
        first_error: BaseException | None = None
        notes: list[str] = []
        for lid, res in zip(league_ids, results):
            if isinstance(res, BaseException):
                log.warning("联赛 %s 赛程拉取失败：%s", lid, res)
                if first_error is None:
                    first_error = res
                continue
            fixtures, season, note = res
            self._season_by_league[int(lid)] = season
            if note:
                notes.append(note)
            merged.extend(fixtures)
        if not merged and first_error is not None:
            raise first_error
        merged.sort(key=lambda fx: parse_kickoff((fx.get("fixture") or {}).get("date")) or datetime.min.replace(tzinfo=timezone.utc))
        return merged, self.season_in_use, (notes[0] if notes else None)

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

    def _sort_fixtures_by_league(self, fixtures):
        """多联赛下按「联赛聚堆 + 联赛内按开赛时间」排序。

        赛程视图是按联赛打印分组标题的（只在联赛切换时打印一次）。若只按
        开赛时间排序，五大联赛的比赛会交替出现，同一个联赛标题被反复打印，
        分组等于失效——用户看不出哪场属于哪个联赛。这里让同联赛的比赛先
        聚成一堆，组内再按时间升序；未登记的联赛排最后，不会被丢弃。
        """
        if not fixtures:
            return fixtures

        def key(fx):
            raw = (fx.get("league") or {}).get("id")
            try:
                lid = int(raw)
            except (TypeError, ValueError):
                lid = 0
            try:
                order = LEAGUE_ORDER.index(lid)
            except ValueError:
                order = len(LEAGUE_ORDER)
            kickoff = parse_kickoff((fx.get("fixture") or {}).get("date"))
            return (order, lid, kickoff or datetime.min.replace(tzinfo=timezone.utc))

        return sorted(fixtures, key=key)

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
                result["fixtures"] = self._sort_fixtures_by_league(cached)
                result["from_cache"] = True
                result["season_range"] = self._season_range()
                if mode == MODE_NEXT:
                    cached = self._limit_to_next(cached, now, s.timezone, result)
                    result["fixtures"] = self._sort_fixtures_by_league(cached)
                    if not cached:
                        result["status"] = ST_WINDOW_EMPTY
                        result["note"] = "ℹ️ 本地缓存中没有未来的比赛，可尝试「刷新数据」。"
                        self.last_note = result["note"]
                        return result
                self.last_note = None
                return result

        try:
            fixtures, season, note = await self._fetch_fixtures_multi(date_from, date_to)
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
            result["fixtures"] = self._sort_fixtures_by_league(fixtures)
            if fb and getattr(fb, "last_shifted_date", None):
                result["status"] = ST_WINDOW_EMPTY
                result["note"] = f"ℹ️ {fb.last_note}"
            self.last_note = result["note"]
            return result

        # 今日无比赛时自动扩窗重查：直接抛空态会让用户以为机器人坏了，
        # 实际上只是今天没球（国际比赛日 / 休赛期）。这里回退到未来 N 天，
        # 并用 note 说清「今日无比赛」，避免把「今天没球」说成「数据源异常」。
        if mode == MODE_TODAY and not fixtures:
            span_from, span_to = today, today + timedelta(days=UPCOMING_DAYS)
            retry: list[dict] = []
            if self.local_first:
                try:
                    retry = self.repo.load_matches(
                        self.sync.competition,
                        span_from.isoformat(), span_to.isoformat(),
                    )
                except Exception:
                    retry = []
            if not retry:
                try:
                    retry, season, _ = await self._fetch_fixtures_multi(span_from, span_to)
                except APIError:
                    retry = []
            if retry:
                result["fixtures"] = self._sort_fixtures_by_league(retry)
                result["status"] = ST_OK
                result["mode"] = MODE_UPCOMING
                self.using_upcoming = True
                self.fixture_day_label = (
                    f"{span_from.isoformat()} ~ {span_to.isoformat()}"
                )
                result["day_label"] = self.fixture_day_label
                result["note"] = (
                    f"ℹ️ 今日（{today.isoformat()}）暂无比赛，"
                    f"已自动显示未来 {UPCOMING_DAYS} 天的赛程。"
                )
                self.last_note = result["note"]
                return result
            # 扩窗后依然没有：必须说清「未来 N 天也查过了」，
            # 否则用户看到的是「今天没比赛」，以为机器人只查了当天。
            result["status"] = ST_WINDOW_EMPTY
            result["day_label"] = f"{span_from.isoformat()} ~ {span_to.isoformat()}"
            result["note"] = (
                f"ℹ️ 今日及未来 {UPCOMING_DAYS} 天内均没有比赛"
                f"（{span_from.isoformat()} ~ {span_to.isoformat()}），"
                f"可尝试指定其它日期或「刷新数据」。"
            )
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
        self.using_national_fallback = False
        self.fixture_day_label = day.isoformat()
        season = s.season
        note = None

        # 主路径：一次请求拿当天全部比赛（含国家队赛事，省去逐联赛轮询）
        fixtures = await self._fetch_all_by_date(day)
        if not fixtures:
            fixtures, season, note = await self._fetch_fixtures_multi(day, day)
        if not fixtures:
            # 今日无比赛：自动扩展到未来 N 天，避免「今天没比赛」就给用户一片空白。
            # 注意区分：这里是「正常无数据」，仍不能把权限/故障错误伪装成空。
            end = day + timedelta(days=UPCOMING_DAYS)
            for offset in range(1, UPCOMING_DAYS + 1):
                d = day + timedelta(days=offset)
                fixtures = await self._fetch_all_by_date(d)
                if fixtures:
                    self.using_upcoming = True
                    self.fixture_day_label = f"{day.isoformat()} ~ {d.isoformat()}"
                    note = (
                        f"ℹ️ 今日（{day.isoformat()}）暂无比赛，"
                        f"已自动展示未来 {UPCOMING_DAYS} 天内的赛程。"
                    )
                    break
            if not fixtures:
                fixtures, season, note = await self._fetch_fixtures_multi(day, end)
                if fixtures:
                    self.using_upcoming = True
                    self.fixture_day_label = f"{day.isoformat()} ~ {end.isoformat()}"
                    note = (
                        f"ℹ️ 今日（{day.isoformat()}）暂无比赛，"
                        f"已自动展示未来 {UPCOMING_DAYS} 天内的赛程。"
                    )
        if fixtures:
            fixtures = sorted(
                fixtures,
                key=lambda fx: (parse_kickoff((fx.get("fixture") or {}).get("date")) or now),
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
        model = await self._model_for(self._league_id_of(fx))
        prediction = await self._predict_one(model, kickoff, fx)
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

        # 积分榜必需，且必须取该场比赛所属联赛的；失败直接抛真实原因
        standings = await self.api.get_standings(self._league_id_of(fx), season)
        model = build_league_model(standings)
        rows_by_team = {(r.get("team") or {}).get("id"): r for r in standings}

        # 可选数据并发取，单点失败不阻断整体，但保留真实原因
        # 伤停是可选数据：数据源不支持该方法时静默跳过，不能让整个分析失败
        injuries_call = getattr(self.api, "get_injuries", None)
        results = await asyncio.gather(
            self.api.get_team_form(home_id, season, FORM_MATCHES),
            self.api.get_team_form(away_id, season, FORM_MATCHES),
            self.api.get_h2h(home_id, away_id, H2H_MATCHES),
            injuries_call(fixture_id) if injuries_call else _no_injuries(),
            return_exceptions=True,
        )
        errors: dict[str, str | None] = {}
        for key, value in zip(("home_form", "away_form", "h2h", "injuries"), results):
            errors[key] = str(value) if isinstance(value, BaseException) else None
        home_raw = [] if isinstance(results[0], BaseException) else results[0]
        away_raw = [] if isinstance(results[1], BaseException) else results[1]
        h2h_raw = [] if isinstance(results[2], BaseException) else results[2]
        injuries_raw = [] if isinstance(results[3], BaseException) else results[3]

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
            "injuries": injuries_stats(injuries_raw, home_id, away_id),
            "model": {
                "home_strength": model.strength(home_id),
                "away_strength": model.strength(away_id),
                "analysis": self.analyzer.predict_match(
                model, home_id, away_id, temperature=self.temperature,
            ),
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

    def refresh_calibration(self) -> float:
        """用已结算样本重拟合概率温度，返回当前生效的 T。

        拟合不出来的情况（样本不足、数据异常）一律退回 1.0 —— 不校准只是
        维持现状，用错误的 T 反而会把概率推得更偏。
        """
        try:
            samples = self.repo.settled_samples()
        except Exception as exc:  # pragma: no cover - 库不可用时保持不校准
            log.warning("读取校准样本失败，保持不校准：%s", exc)
            self.temperature = 1.0
            return self.temperature
        from analyzer import fit_temperature

        self.temperature = fit_temperature(samples)
        if self.temperature != 1.0:
            log.info("概率温度校准已生效：T=%.4f（样本 %d 场）", self.temperature, len(samples))
        return self.temperature

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
                # Elo 闭环：赛果落盘后立刻更新评分。
                # 幂等由 elo_processed 保证——重启后重复触发不会让评分虚高。
                self._sync_elo(fx, int(home), int(away))
        if done:
            log.info("已回写 %d 场赛果", done)
            # 赛果多了，温度该重新拟合——否则校准一直停在启动时的样本上。
            self.refresh_calibration()
        return done

    def _sync_elo(self, fixture: dict, home_score: int, away_score: int) -> None:
        """把一场已结束比赛计入 Elo。任何异常都不影响结算主流程。"""
        if not getattr(self, "elo_enabled", False):
            return
        try:
            teams = (fixture.get("teams") or {})
            home_id = (teams.get("home") or {}).get("id")
            away_id = (teams.get("away") or {}).get("id")
            fx_id = (fixture.get("fixture") or {}).get("id")
            if home_id is None or away_id is None or fx_id is None:
                return
            self._elo_for(self._league_id_of(fixture)).apply_match(
                fx_id, home_id, away_id, home_score, away_score,
                season=self.settings.season)
        except Exception as exc:
            # Elo 出错绝不能拖垮赛果回写
            log.warning("Elo 更新失败（不影响结算）：%s", exc)

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
