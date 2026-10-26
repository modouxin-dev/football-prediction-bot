"""业务层：把赛程 / 积分榜 / 赔率拼成预测，并保存最近的预测供按钮回调使用。"""
from __future__ import annotations
from errfmt import fmt_exc

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查时导入，避免运行时循环依赖
    from elo import EloEngine

import asyncio
import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from analyzer import (LeagueModel, MatchAnalyzer, TeamStrength,
                      apply_market_adjustment, build_league_model,
                      calculate_prediction_level, collect_1x2_odds,
                      consensus_odds, implied_probabilities)
from analyzer import (asian_handicap_edge, collect_asian_handicap_odds,
                      consensus_asian_handicap, score_matrix_from_analysis)
from prematch import side_lineup, squad_factors
from ensemble import FALLBACK_PROBS, EnsemblePredictor
from api_client import APIError, FootballAPI
from config import Settings
from context import (context_factors, merge_factors, player_ratings,
                     squad_positions, weighted_absence_load)
from data_source import DataSourceError
from formatkit import _team_key
from repository import PredictionRepository
from signal_capture import capture as capture_signal_ab, split_factors
from signal_ab import CHANGE_APPLIED, SIGNAL_LINEUP, SIGNAL_RATING
from templates import LEAGUE_ORDER
from weather import WeatherClient

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

# 赛果回溯天数：结算任务只取「今天」的赛程，隔夜比赛会永远匹配不上比分。
# 这里限定最多回补最近几天——再往前要么没有预测流水，要么补了也白烧额度。
SETTLE_BACKFILL_DAYS = 3


def is_season_error(exc: BaseException) -> bool:
    """判断是否为「该赛季不可用」（套餐权限 / 赛季未开放），而不是 Key 无效或限流。"""
    text = str(exc).lower()
    if not text:
        return False
    return ("do not have access to this season" in text) or ("season" in text and "plan" in text)


def _is_finished(m: dict[str, Any]) -> bool:
    """只统计已完场的比赛；状态字段缺失时按已完场处理（兼容精简数据）。"""
    short = ((m.get("fixture") or {}).get("status") or {}).get("short")
    return True if not short else short in _FINISHED


def _side(m: dict[str, Any], team_id: int) -> tuple[int | None, int | None, bool]:
    """返回 (本队进球, 对手进球, 是否主场)；数据不全时返回 (None, None, ...)。"""
    teams, goals = m.get("teams") or {}, m.get("goals") or {}
    hg, ag = goals.get("home"), goals.get("away")
    if hg is None or ag is None:
        return None, None, False
    is_home = (teams.get("home") or {}).get("id") == team_id
    return (hg if is_home else ag), (ag if is_home else hg), is_home


# 阵容只在开赛前约 20–40 分钟才公布（官方说明），过早请求必然为空。
# 因此只有进入这个窗口才去取，否则白烧额度——自动预测每轮数百场，不能每场都试。
PREMATCH_LINEUP_WINDOW = 45 * 60  # 秒

# 阵容数据的两条生效路径（见 PredictionService._lineup_already_applied）。
# 都源自 get_lineups，去重时任一留下 applied 行即视为"这场已用上阵容"。
LINEUP_SIGNAL_TYPES = (SIGNAL_LINEUP, SIGNAL_RATING)

# 赛前刷新一轮最多重算多少场。取 24 是因为一个时段的比赛通常不超过这个数
# （五大联赛同一时刻开踢的场次峰值约 10–15），留一倍余量；
# 超出的按开赛时间排序后自然落在窗口外，下一轮再处理，不会丢失。
PREMATCH_REFRESH_CAP = 24

# 球员评分只在「今晚谁上场」确定后才有意义，故与阵容共用同一窗口。
# 天气提前太久不准（预报会改），6 小时内才取。
CONTEXT_WEATHER_WINDOW = 6 * 3600  # 秒


async def _no_injuries() -> list[dict[str, Any]]:
    """数据源未提供伤停接口时的空实现（保持 gather 结构不变）。"""
    return []



async def _safe_api_call(coro_factory) -> list[dict[str, Any]]:
    """可选端点：方法不存在、抛错、无数据，一律返回空，不阻断预测。"""
    if coro_factory is None:
        return []
    try:
        data = await coro_factory()
    except Exception as exc:  # noqa: BLE001 - 可选信号失败绝不能中断预测
        log.info("赛前信号不可用：%s", exc)
        return []
    return data or []


def injuries_stats(raw: list[dict[str, Any]], home_id: int, away_id: int) -> dict:
    """伤停名单按主客队分组。

    只做整理，不做判断：名单为空时上层显示「暂无伤停信息」，
    不伪造、也不据此调整模型（当前模型未使用伤停信号）。
    """
    out: dict[str, Any] = {"home": [], "away": []}
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


def form_stats(matches: list[dict[str, Any]], team_id: int) -> dict:
    """近期战绩统计：胜平负、进失球、主客场表现。数据不足时 played=0，由上层显示“暂无”。"""
    rows, w = [], {"win": 0, "draw": 0, "lose": 0}
    home, away = {"win": 0, "draw": 0, "lose": 0}, {"win": 0, "draw": 0, "lose": 0}
    gf = ga = 0
    for m in matches or []:
        if not _is_finished(m):  # 未开赛/进行中不算进“近期状态”（状态缺失时按已完场处理）
            continue
        our, their, is_home = _side(m, team_id)
        if our is None or their is None:
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


def h2h_stats(matches: list[dict[str, Any]], home_id: int) -> dict:
    """历史交锋统计（以主队视角）。"""
    finished = []
    win = draw = loss = gf = ga = 0
    for m in matches or []:
        if not _is_finished(m):
            continue
        our, their, _ = _side(m, home_id)
        if our is None or their is None:
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


def _model_probs_of(analysis: dict) -> list[float] | None:
    """从 analysis 取模型原始（未融合市场）概率 (主胜, 平, 客胜)。

    apply_market_adjustment 会把融合前的概率留在 poisson_probs；
    没有该键（未融合 / 老版本）时回退当前三项概率。
    任一概率缺失或和 ≤ 0 时返回 None，由调用方按「无样本」处理。
    """
    if not isinstance(analysis, dict):
        return None
    src = analysis.get("poisson_probs")
    keys: tuple[str, ...]
    if isinstance(src, dict):
        keys = ("home", "draw", "away")
        vals = [src.get(k) for k in keys]
    else:
        keys = ("win_prob", "draw_prob", "loss_prob")
        vals = [analysis.get(k) for k in keys]
    out: list[float] = []
    for v in vals:
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f) or f < 0:
            return None
        out.append(f)
    return out if sum(out) > 0 else None


@dataclass
class Prediction:
    # 主源 API-Football 是数字 ID；备用源 football-data.org 一律加 "fd-" 前缀
    # （football_data.py:217/229/230），所以这三类 ID 实际都可能是字符串。
    # 此前注解写死 int，与 _predict_one 里「保持数据源原类型」的注释自相矛盾，
    # 会误导维护者写出对 str ID 不成立的代码。
    fixture_id: int | str
    league: str
    round_label: str
    venue: str
    home: str
    away: str
    home_id: int | str
    away_id: int | str
    kickoff: datetime
    analysis: dict[str, Any]
    home_strength: TeamStrength
    away_strength: TeamStrength
    model: LeagueModel
    fixture: dict[str, Any]
    bookmakers: list[dict[str, Any]] = field(default_factory=list)
    odds: dict[str, Any] | None = None
    #: 亚洲盘推荐（全场，bet id=4）。None 表示未开盘/取不到——
    #: 绝不用胜平负折算冒充，那是另一件事。
    asian: dict[str, Any] | None = None
    outcomes: dict[str, Any] = field(default_factory=dict)
    best: tuple[str, dict[str, Any]] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "API-Football"
    # 输入数据快照：预测可追溯，事后能回答「这个结论是基于什么算出来的」
    inputs: dict[str, Any] = field(default_factory=dict)
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
    def level(self) -> dict[str, Any]:
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
        # 多模型集成器：三路 (统计模型 / 市场 / 联赛基线)，缺失自动重归一化。
        # ENSEMBLE_LEARN=1 时权重随实测表现动态调整；默认固定权重，行为确定。
        self.ensemble = EnsemblePredictor(
            models=("model", "market", "prior"),
            eta=getattr(settings, "ensemble_eta", 200.0),
            learn=bool(getattr(settings, "ensemble_learn", False)),
        )
        self.season_in_use: int = settings.season
        # 天气客户端惰性创建：它不占足球额度，但默认 strength=0（不改结果），
        # 因此任何失败都必须是静默的。
        self._weather: WeatherClient | None = None
        # 每个联赛各自解析出的赛季：多联赛下各联赛可用赛季未必相同，
        # 建模必须用「该场比赛所属联赛」的赛季，不能统一套主联赛的。
        self._season_by_league: dict[int, int] = {}
        self.using_upcoming: bool = False  # 当前赛程是否为「今日无比赛 → 扩展到未来」的结果
        # 配置联赛当天休战、改用国家队赛事兜底（国际比赛日）
        self.using_national_fallback: bool = False
        self.fixture_day_label: str = ""  # 赛程实际覆盖的日期范围，供标题显示
        self.last_note: str | None = None  # 最近一次操作的降级/空结果提示，由上层展示给用户
        self.truncated_count: int = 0  # 最近一次预测被上限砍掉的场数（0 表示未截断）
        # 键统一为 str：主源是数字 ID、备用源 football-data.org 是 'fd-123'
        # （见 football_data.py:217），两边都必须能用同一个 key 查到。
        # 此前注解写成 OrderedDict[int, ...] 与实际不符，会误导维护者去传 int。
        self._store: OrderedDict[str, Prediction] = OrderedDict()
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
    def _upcoming(fixtures: list[dict[str, Any]], now: datetime, end: datetime) -> list[tuple[datetime, dict]]:
        result = []
        for fx in fixtures:
            info = fx.get("fixture") or {}
            kickoff = parse_kickoff(info.get("date"))
            status = (info.get("status") or {}).get("short")
            if kickoff and status in PRE_MATCH_STATUS and now <= kickoff <= end:
                result.append((kickoff, fx))
        result.sort(key=lambda item: item[0])
        return result

    def _league_id_of(self, fx: dict[str, Any]) -> int:
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

    def _elo_for(self, league_id: int) -> "EloEngine":
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

    @staticmethod
    def confidence_of(pred: Prediction) -> float:
        """把握度：模型给出的最高结果概率（0~1）。

        用它排序而不是任何人工规则——它就是模型自己认为「最可能发生的那个结果」
        的概率，是模型真实置信程度的直接读数。
        """
        a = getattr(pred, "analysis", None) or {}
        vals = []
        for key in ("win_prob", "draw_prob", "loss_prob"):
            try:
                vals.append(float(a.get(key) or 0.0))
            except (TypeError, ValueError):
                vals.append(0.0)
        return max(vals) if vals else 0.0

    def select_top_predictions(
        self, predictions: list[Prediction], *,
        min_n: int | None = None, max_n: int | None = None,
        enabled: bool | None = None, min_confidence: float | None = None,
    ) -> list[Prediction]:
        """只保留最有把握的 N 场（默认 3~7 场）。

        为什么砍场次：一天 30 场全推等于没推，重点被淹没；而且低把握场次会把
        整体命中率往下拉。「少而精」是唯一能同时提高可读性和命中率的手段。

        下限 / 上限语义：
            - 可用场次 >= 上限：只推上限场（宁缺毋滥）；
            - 下限 <= 可用 < 上限：全部推出，不再凑数；
            - 可用 < 下限：今天确实没那么多比赛，全推，不虚构。

        质量线（TOPN_MIN_CONFIDENCE > 0 时生效）：连门槛都没够到的场次直接不推。
        默认 0（不启用），保证「今天有球就有推送」的既有行为不被意外改变。
        """
        s = self.settings
        pool = list(predictions or [])
        if not (getattr(s, "topn_enabled", True) if enabled is None else enabled):
            return pool
        lo = int(getattr(s, "topn_min", 3) if min_n is None else min_n)
        hi = int(getattr(s, "topn_max", 7) if max_n is None else max_n)
        if hi < lo:
            lo, hi = hi, lo
        floor = float(getattr(s, "topn_min_confidence", 0.0) if min_confidence is None else min_confidence)
        if floor > 0:
            pool = [p for p in pool if self.confidence_of(p) >= floor]
        ranked = sorted(pool, key=lambda p: (-self.confidence_of(p), p.kickoff))
        keep = ranked[:hi]
        # 截断必须留痕：砍掉多少、按什么标准砍，都要让用户看得见
        dropped = len(ranked) - len(keep)
        if dropped > 0:
            tail = f"；把握度门槛 {floor:.0%}" if floor > 0 else ""
            self.last_note = (
                f"ℹ️ 今日共 {len(ranked)} 场候选，只推送把握度最高的 "
                f"{len(keep)} 场（上限 {hi} 场）{tail}。"
            )
        elif floor > 0 and not keep:
            self.last_note = (
                f"ℹ️ 今日 {len(predictions or [])} 场候选均未达到把握度门槛 "
                f"{floor:.0%}，本次不推送。"
            )
        return keep

    async def auto_predict_store(
        self, league_ids: tuple[int, ...], days: int = 2, cap: int = 60
    ) -> dict[str, Any]:
        """无人值守批量预测：对指定联赛的未来赛程逐场预测并落盘。

        为什么要有这个方法：
            `build_predictions` 是「用户点了才算」，只覆盖当前选中的那一个联赛。
            于是看板会出现「欧冠 9 场全部未预测」——因为机器人从没为欧冠算过。
            赛程页展示的是**库里所有联赛**，预测若只覆盖单联赛，两边永远对不上。
            这里按联赛列表批量算，让「有赛程」和「有预测」是同一批比赛。

        ⚠️ 幂等：predictions 以 fixture_id 为主键且 ON CONFLICT DO UPDATE，
            重复跑只会刷新概率，不会产生重复行，也不会污染命中率统计。

        ⚠️ 只算 PRE_MATCH 的比赛：已开赛的比赛再补预测毫无意义，
            且会让「赛前预测」变成「事后诸葛亮」，直接毁掉复盘结论。

        返回 {saved, skipped_season, errors, per_league}。
        """
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=max(1, int(days)))
        stats: dict[str, Any] = {
            "saved": 0, "errors": [], "per_league": {}, "window_days": days,
        }

        # 第一阶段：先把各联赛窗口内未开赛的赛程收集齐。
        pending: list[tuple[int, list[tuple[Any, dict[str, Any]]], Any]] = []
        for lid in dict.fromkeys(int(x) for x in league_ids):
            try:
                fixtures, season, note = await self._fetch_fixtures(
                    now.date(), end.date(), league_id=lid)
            except Exception as exc:
                # 单联赛失败不能拖垮其他联赛；原因必须留痕，否则无从排查
                stats["errors"].append(f"联赛 {lid} 赛程拉取失败：{exc}")
                log.warning("自动预测：联赛 %s 赛程拉取失败：%s", lid, exc)
                continue
            upcoming = self._upcoming(fixtures, now, end)
            if not upcoming:
                stats["per_league"][lid] = {"saved": 0, "note": note or "窗口内无未开赛比赛"}
                continue
            pending.append((lid, upcoming, note))

        # 第二阶段：轮转发牌。
        # 配额若按联赛顺序「先到先得」，cap 打满后直接结束，
        # 排在后面的联赛（意甲/西甲）一场都轮不到，看板上它们就恒显示
        # 「尚未生成预测」——明明同步了赛程却没有预测。轮转保证配额不足时
        # 每个联赛都分到一些，而不是前面吃满、后面饿死。
        models: dict[int, LeagueModel] = {}
        cursor: dict[int, int] = {lid: 0 for lid, _, _ in pending}
        saved_per: dict[int, int] = {lid: 0 for lid, _, _ in pending}
        total = 0
        while total < cap:
            progressed = False
            for lid, upcoming, _note in pending:
                if total >= cap:
                    break
                i = cursor[lid]
                if i >= len(upcoming):
                    continue
                if lid not in models:
                    try:
                        models[lid] = await self._model_for(lid, models)
                    except Exception as exc:
                        # 建模失败则放弃该联赛，但原因必须进 errors，否则无从排查
                        stats["errors"].append(f"联赛 {lid} 建模失败：{exc}")
                        log.warning("自动预测：联赛 %s 建模失败：%s", lid, exc)
                        cursor[lid] = len(upcoming)
                        continue
                kickoff, fx = upcoming[i]
                cursor[lid] = i + 1
                try:
                    prediction = await self._predict_one(models[lid], kickoff, fx)
                except Exception as exc:
                    log.warning("自动预测：fixture %s 失败：%s",
                                (fx.get("fixture") or {}).get("id"), exc)
                    continue
                self._remember(prediction)
                saved_per[lid] += 1
                total += 1
                progressed = True
            if not progressed:
                break

        for lid, _upcoming, note in pending:
            stats["per_league"][lid] = {"saved": saved_per[lid], "note": note}
        stats["saved"] = total
        return stats

    async def prematch_refresh(
        self,
        window_seconds: float = PREMATCH_LINEUP_WINDOW,
        cap: int = PREMATCH_REFRESH_CAP,
    ) -> dict[str, Any]:
        """赛前刷新：只对**即将开赛**的比赛重算预测，让阵容信号真正生效。

        为什么必须有这个任务（它不是"再跑一次自动预测"就够了）：
            `auto_predict_store` 每 6 小时一轮，而首发阵容按官方说明要
            开赛前 20–40 分钟才公布。两个节奏对不上时——例如比赛 19:45 开踢，
            自动预测落在 14:00 与 20:00——19:45 那场在 14:00 拿不到阵容，
            20:00 又已经开赛被排除。**阵容信号在线上一次都不会触发**，
            代码写了、测试过了，生产里却是死代码。（这正是本项目反复出现
            的"接了线但没人调用"那一类问题。）

        因此这里按「距离开赛还有多久」挑比赛，只在官方公布窗口内重算，
        拿到阵容后立刻把新概率落盘（predictions 以 fixture_id 为主键，
        覆盖而非新增）。

        ⚠️ 幂等与额度：拿到阵容的比赛会在 signal_ab 留下 applied 记录，
            下一轮据此跳过；没拿到（官方还没公布）的才会重试。
            因此一轮真实请求数 ≈ 窗口内尚未拿到阵容的场次数，不是全量。

        ⚠️ 已开赛的比赛绝不重算：那会让「赛前预测」变成事后诸葛亮，
            直接毁掉复盘与回测的结论。

        返回 {checked, refreshed, skipped, errors}。
        """
        stats: dict[str, Any] = {
            "checked": 0, "refreshed": 0, "skipped": 0, "errors": [],
            # 本轮实际重算过的比赛明细，供赛前二次推送按"距开赛多久 +
            # 是否拿到阵容"决定推送时机。放在这里而不是让调用方重查一遍，
            # 是为了避免两处算出不同的 lead（时间会流逝）。
            "entries": [],
        }
        settings = getattr(self, "settings", None)
        if not getattr(settings, "prematch_enabled", False):
            stats["skipped_reason"] = "赛前信号总开关关闭（PREMATCH_ENABLED=0）"
            return stats
        now = datetime.now(timezone.utc)
        try:
            window = float(window_seconds)
        except (TypeError, ValueError):
            window = float(PREMATCH_LINEUP_WINDOW)
        end = now + timedelta(seconds=window)
        # 只按日期粗筛（utc_date 时区后缀不统一），精确窗口在下面用解析后的
        # 开赛时间判断，避免字符串比较把跨时区的比赛漏掉。
        rows = self.repo.upcoming_matches(
            now.date().isoformat(), end.date().isoformat(), limit=max(int(cap) * 5, 50))
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for fx in rows:
            kickoff = parse_kickoff((fx.get("fixture") or {}).get("date"))
            if kickoff is None:
                continue
            # 严格 now < kickoff <= end：已开赛的排除，窗口外的留给自动预测
            if not (now < kickoff <= end):
                continue
            if _is_finished(fx):
                continue
            candidates.append((kickoff, fx))
        candidates.sort(key=lambda item: item[0])

        models: dict[int, Any] = {}
        for kickoff, fx in candidates[: max(int(cap), 1)]:
            fid = (fx.get("fixture") or {}).get("id")
            stats["checked"] += 1
            # 已经拿到并施加过阵容的比赛不再重复请求，省额度、少一次无谓失败
            if self._lineup_already_applied(fid):
                stats["skipped"] += 1
                continue
            lid = self._league_id_of(fx)
            if lid not in models:
                try:
                    models[lid] = await self._model_for(lid, models)
                except Exception as exc:  # 单联赛建模失败不拖垮其他比赛
                    stats["errors"].append(f"联赛 {lid} 建模失败：{exc}")
                    log.warning("赛前刷新：联赛 %s 建模失败：%s", lid, exc)
                    continue
            try:
                prediction = await self._predict_one(models[lid], kickoff, fx)
            except Exception as exc:
                stats["errors"].append(f"fixture {fid} 预测失败：{exc}")
                log.warning("赛前刷新：fixture %s 失败：%s", fid, exc)
                continue
            self._remember(prediction)
            stats["refreshed"] += 1
            stats["entries"].append({
                "fixture_id": fid,
                "kickoff": kickoff,
                "lead_seconds": (kickoff - now).total_seconds(),
                # 阵容是否真的拿到了：与去重判断共用同一份真值（signal_ab
                # 的 applied 行），避免两处口径不一致导致"说拿到了其实没有"。
                "lineup": bool(self._lineup_already_applied(fid)),
                "prediction": prediction,
            })
        return stats

    def _lineup_already_applied(self, fixture_id: Any) -> bool:
        """该场是否已经拿到过首发阵容并施加过（用于避免每轮重复请求）。

        用 signal_ab 的 applied 行当标记，而不是另设一张表：
            它本来就是"这一场、这一类信号施加过"的权威记录，
            再建一张表只会有两份可能不一致的真值。
        查不到（或表不存在）一律返回 False —— 宁可多请求一次，
        也不能因为标记读不出来而永远不再取阵容。

        ⚠️ 阵容数据有**两条**生效路径，只认 SIGNAL_LINEUP 会漏掉另一条：
            · SIGNAL_LINEUP —— 阵型攻守倾向（受 PREMATCH_TILT 控制）
            · SIGNAL_RATING —— 首发 11 人的赛季评分均值（受 RATING_STRENGTH 控制）
          两者的数据源头都是 get_lineups。默认 PREMATCH_TILT=0（倾向未启用）
          而 RATING_STRENGTH=0.5（评分启用），此时阵容只走评分路径，
          若只认 SIGNAL_LINEUP，去重会失效 —— 同一场比赛每 15 分钟被
          重复请求一次阵容，白烧额度。故两条路径任一留下 applied 行即视为
          "这场已经用上阵容"。
        """
        if fixture_id is None:
            return False
        try:
            rows = self.repo.signal_ab_for_fixture(fixture_id)
        except Exception as exc:  # 读标记失败不能当成"已施加"
            log.warning("读取阵容标记失败（fixture=%s）：%s", fixture_id, exc)
            return False
        for row in rows:
            try:
                if row["signal_type"] in LINEUP_SIGNAL_TYPES \
                        and row["change_type"] == CHANGE_APPLIED:
                    return True
            except (KeyError, IndexError, TypeError):
                continue
        return False

    def _signal_ab_snapshot(self) -> dict[str, Any]:
        """当前关键配置快照，随每条对照记录一起存。

        半年后要回答"为什么那段时间对照结果长那样"，光看代码不够 ——
        代码里只有今天的默认值，而当时的强度、融合权重、温度可能完全不同。
        没有这个快照，历史对照数据就废了一半。
        """
        s = self.settings
        keys = ("prematch_enabled", "prematch_strength", "prematch_tilt",
                "rating_strength", "weather_strength", "market_weight",
                "temperature")
        return {k: getattr(s, k, None) for k in keys}

    async def _prematch_factors(
        self,
        fixture_id: Any,
        kickoff: datetime | None,
        home_id: Any,
        away_id: Any,
        city: str | None = None,
        season: int | None = None,
        split_out: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """合成全部赛前信号（伤停 / 首发阵容 / 球员评估 / 天气）修正因子。

        返回 None 表示「不修正」，此时模型行为与接入前**逐位一致**：
        未启用、拿不到任何信号、或修正量本身为零，都属于这种情况。

        `split_out` 传入一个空 dict 时，会把**四类信号各自独立**的因子填进去，
        供 `signal_capture.capture` 计算三版对照预测。它是旁路输出：
        填充分裂因子的失败只告警，不影响返回的 merged 与实际预测。
        """
        settings = getattr(self, "settings", None)
        if not getattr(settings, "prematch_enabled", False):
            return None
        now = datetime.now(timezone.utc)
        lead = None
        if isinstance(kickoff, datetime):
            lead = (kickoff - now).total_seconds()
        # 阵容只在临近开赛才公布，没进窗口就不问，省额度也少一次无谓失败。
        want_lineups = lead is not None and lead <= PREMATCH_LINEUP_WINDOW

        injuries_call = getattr(self.api, "get_injuries", None)
        lineups_call = getattr(self.api, "get_lineups", None) if want_lineups else None

        tasks = [_safe_api_call(lambda: injuries_call(fixture_id))
                 if injuries_call else _no_injuries()]
        if lineups_call:
            tasks.append(_safe_api_call(lambda: lineups_call(fixture_id)))
        results = await asyncio.gather(*tasks)
        injuries = results[0] if results else []
        lineups = results[1] if len(results) > 1 else []

        # 球员评估与天气：即使没有伤停/阵容也可能有贡献（评分靠首发，
        # 天气只看城市），所以单独判断而不是跟着上面的早退。
        rating_strength = getattr(settings, "rating_strength", 0.0)
        weather_strength = getattr(settings, "weather_strength", 0.0)
        loads, start_xi, ratings, weather = {}, {}, {}, None
        if (rating_strength > 0 or weather_strength > 0) and season is not None:
            loads, start_xi, ratings, weather = await self._context_signals(
                kickoff, home_id, away_id, lineups, injuries, city, season
            )
        elif season is not None:
            # 强度为 0 时只需位置加权（它属于伤停范畴，受 prematch 强度控制）
            loads, _, _, _ = await self._context_signals(
                kickoff, home_id, away_id, lineups, injuries, city, season
            )

        if not injuries and not lineups and not start_xi and weather is None:
            return None

        factors = squad_factors(
            home_id, away_id,
            injuries=injuries, lineups=lineups,
            strength=getattr(settings, "prematch_strength", 0.5),
            tilt_weight=getattr(settings, "prematch_tilt", 0.0),
            loads=loads or None,
        )
        context = context_factors(
            home_id, away_id,
            start_xi=start_xi or None,
            ratings=ratings or None,
            weather_sample=weather,
            rating_strength=rating_strength,
            weather_strength=weather_strength,
        )
        merged = merge_factors(factors, context)
        if merged is None:
            return None
        # 旁路：把四类信号各自独立的因子交给采集器算对照。失败只告警 ——
        # 对照是为了以后能变好，不能因为它挂了影响本次预测。
        if split_out is not None:
            try:
                split_out.update(split_factors(
                    home_id, away_id,
                    injuries=injuries, lineups=lineups, loads=loads or None,
                    start_xi=start_xi or None, ratings=ratings or None,
                    weather_sample=weather,
                    prematch_strength=getattr(settings, "prematch_strength", 0.5),
                    prematch_tilt=getattr(settings, "prematch_tilt", 0.0),
                    rating_strength=rating_strength,
                    weather_strength=weather_strength,
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("分裂赛前信号因子失败（fixture=%s）：%s", fixture_id, exc)
        # 修正量全为零（例如只有存疑伤停且强度为 0）时同样视为不修正
        if all(abs(merged[side][k] - 1.0) < 1e-12
               for side in ("home", "away") for k in ("attack", "concede")):
            if abs(merged.get("weather", 1.0) - 1.0) < 1e-12:
                return None
        return merged

    def _weather_client(self) -> WeatherClient:
        """惰性创建天气客户端（只有 weather_strength>0 才会真正发请求）。"""
        if self._weather is None:
            self._weather = WeatherClient(
                enabled=getattr(self.settings, "weather_strength", 0.0) > 0
            )
        return self._weather

    async def _context_signals(
        self,
        kickoff: datetime | None,
        home_id: Any,
        away_id: Any,
        lineups: list[dict[str, Any]] | None,
        injuries: list[dict[str, Any]] | None,
        city: str | None,
        season: int,
    ) -> tuple[dict[str, float], dict[str, list[dict[str, Any]]], dict[str, Any], Any]:
        """收集球员评估所需的三类旁路数据。失败一律返回空，不阻断预测。

        返回 (加权缺阵负荷, 各队首发, 各队评分表, 天气快照)。
        """
        settings = self.settings
        now = datetime.now(timezone.utc)
        lead = None
        if isinstance(kickoff, datetime):
            lead = (kickoff - now).total_seconds()

        squad_call = getattr(self.api, "get_squad", None)
        stats_call = getattr(self.api, "get_team_player_stats", None)

        # 位置映射：伤停端点没有位置，靠球队名单补上
        positions: dict[str, dict[str, str]] = {}
        if squad_call:
            for key, tid in (("home", str(home_id)), ("away", str(away_id))):
                try:
                    raw = await _safe_api_call(lambda tid=tid: squad_call(tid))
                except Exception:  # noqa: BLE001
                    raw = []
                positions[key] = squad_positions(raw)

        # 评分：只有确定首发后才有意义，与阵容同一窗口。
        # 键必须用**球队 id**（不是 "home"/"away"）：context_factors 按 id 查表，
        # 两边键不一致会让评分静默失效（曾真实发生过）。
        want_xi = bool(lineups)
        ratings: dict[str, dict[str, tuple[float, float]]] = {}
        start_xi: dict[str, list[dict[str, Any]]] = {}
        if want_xi and stats_call:
            for key, tid in (("home", str(home_id)), ("away", str(away_id))):
                side = side_lineup(lineups, tid)
                xi = side.get("startXI") or []
                start_xi[str(tid)] = xi
                if not xi:
                    continue
                try:
                    raw = await _safe_api_call(lambda tid=tid: stats_call(tid, season))
                except Exception:  # noqa: BLE001
                    raw = []
                ratings[str(tid)] = player_ratings(raw)

        # 加权缺阵负荷（位置信息拿不到时退化为按人数）
        loads: dict[str, float] = {}
        for key, tid in (("home", home_id), ("away", away_id)):
            loads[key] = weighted_absence_load(
                injuries, tid, positions.get(key) or {}
            )

        # 天气：默认关闭（strength=0），此时连 DNS 都不会查
        sample = None
        if getattr(settings, "weather_strength", 0.0) > 0 and city:
            if lead is None or lead <= CONTEXT_WEATHER_WINDOW:
                try:
                    sample = await self._weather_client().for_fixture(city, kickoff)
                except Exception as exc:  # noqa: BLE001
                    log.info("天气不可用（city=%s）：%s", city, exc)
                    sample = None
        return loads, start_xi, ratings, sample

    @staticmethod
    def _build_trace(*, analysis: dict, prematch: dict | None,
                     model_probs: tuple[float, float, float],
                     final_probs: tuple[float, float, float],
                     market_probs: tuple[float, float, float] | None,
                     weights: dict, used: list, fallback: bool,
                     temperature: float) -> dict:
        """组装单场预测的「决策链证据」。

        为什么是阶段式而非只记特征：
        特征只回答「用了什么」，阶段才回答「每一步把概率推了多少」。
        复盘真正要定位的是后者——比如市场权重 0.75 把模型给的主胜 45%
        拉到 52%，最终判客胜，这个锅在集成环节而不在泊松。

        三个阶段严格按实际执行顺序排列，probs 为该阶段**结束后**的概率，
        故最后一项必然等于落库的展示概率（自洽性由测试保证）。

        Args:
            analysis: predict_match 的输出（已被市场改写，但 features /
                pre_temperature_probs 是改写前留的快照）
            prematch: 赛前信号（伤停/首发），None 表示未启用
            model_probs: 模型路概率（温度校准后、集成前）
            final_probs: 集成后概率，即展示概率
            market_probs: 市场去水后的隐含概率，无赔率时为 None
            weights: 集成器实际使用的各路权重
            used: 实际参与集成的路（缺的路不会出现在这里）
            fallback: 是否回落到经验值
            temperature: 本次使用的温度值

        Returns:
            可直接 JSON 序列化的 dict。任何字段取不到时记为 None，
            **不填默认值**——None 表示"未知"，填 0 会被读成"影响为零"。
        """
        pre_t = analysis.get("pre_temperature_probs")
        # ⚠️ 阶段命名必须与实际执行顺序严格对应：
        # 第一阶段的 probs 是「温度前」的概率，它**已经包含** Elo 与赛前信号
        # 的影响（两者在 λ 层面生效，早于温度）。把它叫「泊松基线」是错的，
        # 会把 Elo / 伤停的功劳算到泊松头上。记为「模型基线」，
        # Elo 与赛前信号单独在 features 里如实标注是否启用。
        stages: list[dict] = [
            {
                "key": "model", "label": "模型基线",
                "note": "泊松 + Dixon-Coles（含 Elo 与赛前信号对 λ 的修正）",
                "probs": [round(float(x), 4) for x in pre_t] if pre_t else None,
                "prematch_applied": bool(prematch),
            },
            {
                "key": "temperature", "label": "温度校准",
                "note": "拉平概率以修正过度自信",
                "temperature": round(float(temperature), 4),
                "probs": [round(float(x), 4) for x in model_probs],
            },
            {
                "key": "ensemble", "label": "多模型集成",
                "note": "模型 / 市场 / 联赛基线三路加权",
                "weights": {k: round(float(v), 4) for k, v in weights.items()},
                "used": list(used),
                "fallback": fallback,
                "probs": [round(float(x), 4) for x in final_probs],
            },
        ]
        return {
            "stages": stages,
            "features": analysis.get("features"),
            "market": (
                {"home": round(market_probs[0], 4),
                 "draw": round(market_probs[1], 4),
                 "away": round(market_probs[2], 4)}
                if market_probs else None),
            "final": [round(float(x), 4) for x in final_probs],
        }

    async def _predict_one(
        self, model: LeagueModel, kickoff: datetime, fx: dict[str, Any], fresh: bool = False
    ) -> Prediction:
        teams = fx.get("teams") or {}
        home, away = teams.get("home") or {}, teams.get("away") or {}
        fixture_id = fx["fixture"]["id"]  # 保持数据源原类型；_store 内部统一用 str key 查找

        # 球员评估要按「该场所属联赛」的赛季取，多联赛下各联赛赛季未必相同
        league_id = (fx.get("league") or {}).get("id")
        season = self._season_by_league.get(league_id, self.season_in_use) \
            if league_id is not None else self.season_in_use
        city = ((fx.get("fixture") or {}).get("venue") or {}).get("city")
        split_out: dict[str, Any] = {}
        prematch = await self._prematch_factors(
            fixture_id, kickoff, home["id"], away["id"], city=city, season=season,
            split_out=split_out)
        analysis = self.analyzer.predict_match(
            model, home["id"], away["id"], temperature=self.temperature,
            prematch=prematch,
        )
        # 对照采集紧跟在 predict_match 之后：此时 analysis 还是"刚算出"的状态，
        # 再往下就会被市场融合改写，采到的就不是模型口径了。
        # 采集失败只告警（capture 内部已保证），不影响本次预测。
        #
        # ⚠️ 这里的条件**不能要求 prematch 非空**：prematch 为 None 恰恰是
        # "所有信号都撤了"的场景（伤停球员赛前复出等），而"信号被撤销"必须
        # 写一行 revoked 记录。加上 `prematch and` 的话，信号一旦全部消失
        # 采集就被跳过，撤销永远记录不下来 —— 复盘会把"信号撤销后仍然命中"
        # 错算成"信号有效"。只按 split_out 判断：它空着时 capture 内部会
        # 立刻返回 0，不会白跑。
        if (getattr(self.settings, "signal_ab_enabled", False)
                and split_out):
            capture_signal_ab(
                self.repo, fixture_id=fixture_id, analyzer=self.analyzer,
                model=model, home_id=home["id"], away_id=away["id"],
                by_signal=split_out, temperature=self.temperature,
                config_snapshot=self._signal_ab_snapshot(),
            )
        odds_error: str | None = None
        try:
            odds_response = await self.api.get_odds(fixture_id, fresh=fresh)
        except (APIError, DataSourceError) as exc:  # 赔率缺失不应阻断整条预测
            odds_error = str(exc)
            log.warning("赔率获取失败（fixture=%s）：%s", fixture_id, exc)
            odds_response = []
        bookmakers = collect_1x2_odds(odds_response)
        odds = consensus_odds(bookmakers)
        # 亚盘：从同一份 /odds 响应里解析，**不再额外发请求**。
        # 盘口线由数据源实际开盘决定，这里只挑「公司数最多」的主流盘。
        asian = None
        try:
            ah_rows = collect_asian_handicap_odds(odds_response)
            ah_cons = consensus_asian_handicap(ah_rows)
            if ah_cons:
                asian = asian_handicap_edge(score_matrix_from_analysis(analysis), ah_cons)
        except Exception as exc:  # 亚盘是增强项，失败不能拖累整条预测
            log.debug("亚盘解析失败（fixture=%s）：%s", fixture_id, exc)
            asian = None
        # edge 由泊松口径计算：它是「模型 vs 市场」的差异，若先换成市场口径
        # 则 edge 恒为 -抽水，失去信息量。故两者的先后顺序不能颠倒。
        outcomes = self.analyzer.evaluate_outcomes(analysis, odds)
        # 展示概率换成市场口径（实测优于泊松：准确率 53.78% vs 50.99%，
        # log_loss 0.9692 vs 1.0053，McNemar p=0.007047，见 docs/MARKET_FUSION_EVAL.md）。
        # 无赔率时 apply_market_adjustment 原样返回，行为不变。
        # 市场锚定融合：w·市场 + (1−w)·模型，w 由 MARKET_BLEND_WEIGHT 配置
        # （默认 0.75）。矩阵同步缩放，故最可能比分与三项概率仍自洽。
        # 模型原始概率已由 apply_market_adjustment 存入 poisson_probs 供复盘对比。
        # ---- 多模型集成（ensemble 模块）----
        # 三路：统计模型 / 市场赔率 / 联赛基线。缺失的路自动重归一化，
        # 全缺时由集成器回落到经验值 —— 保证任何情况下都出得来结论。
        # 这里必须在 evaluate_outcomes 之后：edge 要用泊松口径算，
        # 换成集成口径后 edge 会系统性趋近负抽水，价值投注功能就废了。
        model_p = (
            float(analysis.get("win_prob") or 0.0),
            float(analysis.get("draw_prob") or 0.0),
            float(analysis.get("loss_prob") or 0.0),
        )
        market_raw = implied_probabilities(odds)
        market_p = None
        if market_raw:
            market_p = (market_raw["home"], market_raw["draw"], market_raw["away"])
        ens_out = self.ensemble.predict({
            "model": model_p,
            "market": market_p,
            "prior": FALLBACK_PROBS,
        })
        final_p = ens_out["probs"]
        analysis = apply_market_adjustment(
            analysis, odds,
            target_probs={"home": final_p[0], "draw": final_p[1],
                          "away": final_p[2]})
        analysis["ensemble"] = {
            "used": ens_out["used"],
            "weights": {k: round(v, 4) for k, v in ens_out["weights"].items()},
            "fallback": ens_out["fallback"],
        }
        # 可追溯性：把「这个概率是怎么一步步变成的」落成证据链。
        # 没有它，复盘只能看到最终数字，无法回答
        # 「是模型算错了还是市场带偏了」「温度校准动了多少」。
        analysis["trace"] = self._build_trace(
            analysis=analysis, prematch=prematch,
            model_probs=model_p, final_probs=final_p,
            market_probs=market_p, weights=ens_out.get("weights") or {},
            used=ens_out.get("used") or [], fallback=bool(ens_out.get("fallback")),
            temperature=self.temperature)
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
                # 模型原始（未融合）概率：温度校准必须用这个口径。
                # home_prob 存的是融合后的展示概率，市场占 w（默认 0.75）——
                # 拿它拟合温度等于在校准市场，而实测市场已校准良好
                # （校准后 Δlogloss=0、ΔECE=0），泊松才是过度自信的那一方
                # （泊松+温度：Δlogloss −0.0034、ΔECE −0.0126）。
                # 两者混用会让 T 被市场成分稀释到 ≈1.0，泊松的偏差得不到修正。
                "model_probs": _model_probs_of(analysis),
                "market_blended": bool(analysis.get("market_blended")),
                # 决策链证据：阶段式概率 + 输入特征 + 市场口径。
                # 落库而非现算——事后重算会重新拉积分榜并再烧一次额度，
                # 且可能算出一个与当初推送不一致的结论。
                "trace": analysis.get("trace"),
                # 展示详情所需的中间量：预期进球、大小球、双方进球、最可能比分。
                # 不落库的话，事后想在看板上展开单场详情就得整场重算——
                # 那会重新拉积分榜并再烧一次额度，而这些都是纯数学派生量，
                # 由同一份 λ 直接算出来即可，无需联网。
                "lambda_home": analysis.get("lambda_home"),
                "lambda_away": analysis.get("lambda_away"),
                "over_2_5": analysis.get("over_2_5"),
                "btts": analysis.get("btts"),
                "top_scores": analysis.get("top_scores") or [],
                "rho": analysis.get("rho"),
                # 1X2 赔率（各家中位数）落库：不存的话「赔率统计 / ROI」
                # 事后根本算不出来——赔率是**当时的市场报价**，事后拉不到，
                # 整季重拉既烧额度又拿不到历史盘口。这是赔率类统计的唯一数据源。
                "odds": (
                    {"home": round(float(odds["home"]), 3),
                     "draw": round(float(odds["draw"]), 3),
                     "away": round(float(odds["away"]), 3)}
                    if odds else None),
                # 赔率取不到时保留真实原因（套餐不含 / 未开盘 / 限流），
                # 展示层据此如实说明，而不是笼统显示「暂无赔率数据」。
                "odds_error": odds_error,
                "kickoff": kickoff.isoformat() if kickoff else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            fixture=fx,
            bookmakers=bookmakers,
            odds=odds,
            asian=asian,
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
            log.warning("获取可用赛季列表失败（%s），改用逐年降级", fmt_exc(exc))

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
    def _is_national(cls, fx: dict[str, Any]) -> bool:
        """该场是否国家队/洲际赛事（世预赛、欧国联、友谊赛等）。"""
        lg = fx.get("league") or {}
        country = str(lg.get("country") or "").strip().lower()
        if country and country in cls._NAT_COUNTRIES:
            return True
        name = str(lg.get("name") or "").strip().lower()
        return any(k in name for k in cls._NAT_KEYWORDS)

    async def _fetch_all_by_date_raw(self, day) -> list[dict[str, Any]]:
        """某一天的全部比赛，**不做联赛筛选**，供结算回填专用。

        与 ``_fetch_all_by_date`` 的两点关键差异，都是回填场景硬要求的：

        1. **接受 str 或 date**。``stale_pending_days()`` 返回的是 ISO
           字符串，而取数接口内部调 ``day.isoformat()``——str 没有该方法，
           会抛 AttributeError。实测：
           ``str("2026-10-19").isoformat()`` → AttributeError。
           而该异常会被本方法内层的 except 吞掉并返回 []，外层看到「没抛
           异常」就记为「取数成功」，于是出现「扫描 17 天、成功取数 17 天、
           取到比赛 0 场、补回 0 场」这种全程无报错却毫无进展的假象。

        2. **不做联赛筛选**。回填靠 fixture_id 精确匹配，ID 本身就是足够
           严格的过滤条件；而 ``_fetch_all_by_date`` 只保留当前配置的联赛，
           若僵尸行属于历史配置过、后来去掉的联赛，就永远取不到。

        异常照常向上抛——回填要把它记进 failed_days，不能静默。
        """
        d = date.fromisoformat(day) if isinstance(day, str) else day
        return await self.api.get_fixtures_by_date(d)

    async def _fetch_all_by_date(self, day) -> list[dict[str, Any]]:
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
            log.warning("按日拉取赛程失败（%s）：%s", day, fmt_exc(exc))
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
                              league_id: int | None = None) -> tuple[list[dict[str, Any]], int, str | None]:
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
    ) -> tuple[list[dict[str, Any]], int, str | None]:
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
        merged: list[dict[str, Any]] = []
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
        merged.sort(key=lambda fx: parse_kickoff((fx.get("fixture") or {}).get("date"))
                    or datetime.min.replace(tzinfo=timezone.utc))
        return merged, self.season_in_use, (notes[0] if notes else None)

    def _season_range(self) -> tuple[Any, ...] | None:
        """当前数据源记录的赛季日期范围（最早 / 最晚比赛日），用于排查窗口命中情况。"""
        fb = getattr(self.api, "fallback", None)
        return getattr(fb, "season_range", None) if fb else None

    def _limit_to_next(self, fixtures: list[dict[str, Any]], now: datetime, tz, result: dict) -> list[dict]:
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
                             now: datetime | None = None) -> dict[str, Any]:
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

        result: dict[str, Any] = {
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
                # 上面的过滤用 `or now` 兜底，date 缺失/格式异常的场次也会留在
                # upcoming 里且排在最前，因此这里 k 可能为 None，直接
                # .astimezone() 会抛 AttributeError。
                if k is not None:
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
            retry: list[dict[str, Any]] = []
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

    async def get_today_fixtures(self, now: datetime | None = None) -> list[dict[str, Any]]:
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

    def _match_fixture(self, fixtures: list[dict[str, Any]] | None, fixture_id) -> dict[str, Any] | None:
        """在一批赛程里按 ID 找单场比赛；赛程为空返回 None。"""
        if not fixtures:
            return None
        return next(
            (f for f in fixtures if str((f.get("fixture") or {}).get("id")) == str(fixture_id)),
            None,
        )

    async def _resolve_fixture(self, fixture_id, fixtures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """按 ID 取原始赛程数据，四级兜底，取不到才抛 KeyError。

        为什么必须多级：赛程缓存 TTL 只有 10 分钟（football_data.py:TTL_MATCHES），
        过期后用户再点昨天 / 几小时前的卡片，列表里已经没有这场，于是上层只能报
        「该场比赛已不在今日赛程中」——明明卡片还摆在屏幕上。

        1) 调用方传入的赛程缓存；
        2) 内存里的预测快照（最快，零 IO）；
        3) 库内落盘的赛程快照（机器人重启后仍有效，见 repository.get_fixture_json）；
        4) 重新拉取今日赛程——异常原样抛出，绝不把 API 故障吞成「没有比赛」。

        四级都落空才抛 KeyError，由上层如实提示「不在今日赛程」。

        ⚠️ 本方法在全类内**只能定义一次**。此前两个分支各改一次，合并后
        产生同名重载，Python 后定义覆盖先定义，落库兜底被内存版整个遮蔽，
        线上表现仍是旧行为。修改时务必确认没有第二个同名方法。
        """
        # 1) 传入缓存
        fx = self._match_fixture(fixtures, fixture_id)
        if fx is not None:
            return fx
        # 2) 内存快照：同一进程内算过就能立刻复用
        snap = self._store.get(str(fixture_id))
        if snap is not None and getattr(snap, "fixture", None):
            return snap.fixture
        # 3) 库内快照：跨重启仍能点开历史卡片
        if self.repo is not None:
            fx = self.repo.get_fixture_json(fixture_id)
            if fx is not None:
                log.info("赛程缓存已失效，用库内快照恢复比赛 fixture=%s", fixture_id)
                return fx
        # 4) 重新拉取，仍找不到才认定真的不在（异常不吞，如实上报）
        fx = self._match_fixture(await self.get_today_fixtures(), fixture_id)
        if fx is not None:
            return fx
        raise KeyError(fixture_id)

    # ---- 单场比赛预测 ----------------------------------------------------------
    async def predict_fixture(self, fixture_id: int, fixtures: list[dict[str, Any]] | None = None) -> Prediction:
        """为指定的一场比赛生成预测。

        fixtures 为今日赛程缓存；不传则先查预测快照、再重新拉取一次。
        找不到该场比赛抛 KeyError，API 失败抛 APIError（由上层展示真实原因）。
        """
        fx = await self._resolve_fixture(fixture_id, fixtures)
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

    async def analyze_fixture(self, fixture_id: int, fixtures: list[dict[str, Any]] | None = None) -> dict:
        """深度分析报告：近期状态 + 联赛排名 + 历史交锋 + 模型因素。

        积分榜是必需数据，取不到就抛 APIError，由上层显示真实原因；
        近期状态与历史交锋属于可选数据，失败时降级为「暂无可靠数据」并附上真实原因，
        绝不把权限错误伪装成「没有比赛 / 没有数据」。
        """
        fx = await self._resolve_fixture(fixture_id, fixtures)
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

        # 阵容未进赛前窗口时不请求（官方：开赛前 20–40 分钟才公布）
        # 球队名单（含位置）同样只在赛前窗口内取，用于伤停位置加权。
        lineups_raw: list[dict[str, Any]] = []
        if getattr(s, "prematch_enabled", False):
            lineups_call = getattr(self.api, "get_lineups", None)
            if lineups_call:
                lead = (kickoff - datetime.now(timezone.utc)).total_seconds()
                if lead <= PREMATCH_LINEUP_WINDOW:
                    lineups_raw = await _safe_api_call(lambda: lineups_call(fixture_id))
        # 与预测路径共用同一套赛前信号（伤停 / 首发 / 球员评估 / 天气），
        # 否则「分析」与「预测」会给出口径不一致的数字，用户无从判断信哪个。
        # 球员评估要按该场所属联赛的赛季取，多联赛下各联赛赛季未必相同。
        _lg = (fx.get("league") or {}).get("id")
        _season = self._season_by_league.get(_lg, season) if _lg is not None else season
        _city = ((fx.get("fixture") or {}).get("venue") or {}).get("city")
        prematch_factors = await self._prematch_factors(
            fixture_id, kickoff, home_id, away_id, city=_city, season=_season)

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
            "prematch": (prematch_factors or {}).get("detail"),
            "model": {
                "home_strength": model.strength(home_id),
                "away_strength": model.strength(away_id),
                "analysis": self.analyzer.predict_match(
                    model, home_id, away_id, temperature=self.temperature,
                    prematch=prematch_factors,
                ),
                "avg_home_goals": model.avg_home_goals,
                "avg_away_goals": model.avg_away_goals,
            },
            "errors": errors,
            "source": self.source_label,
            "has_team_data": bool(model.teams) and home_id in model.teams and away_id in model.teams,
            "created_at": datetime.now(timezone.utc),
        }

    async def get_standings_page(self, limit: int = 20) -> list[dict[str, Any]]:
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

    def _missing_result_days(self, pending, by_id: dict) -> list[Any]:
        """pending 中在今天窗口里没匹配上赛程的行，按开赛日期去重返回。

        只回溯最近 SETTLE_BACKFILL_DAYS 天：再往前要么是历史遗留脏数据
        （那时根本没有预测流水），要么补了也没意义，白白消耗额度。
        """
        from datetime import date as _date, timedelta as _td

        cutoff = (datetime.now(timezone.utc) - _td(days=SETTLE_BACKFILL_DAYS)).date()
        days: dict[str, Any] = {}
        for row in pending:
            fid = str(row["fixture_id"])
            if fid in by_id:
                continue
            raw = row["kickoff"]
            if not raw:
                continue
            try:
                kd = datetime.fromisoformat(str(raw)).date()
            except ValueError:
                continue
            if kd < cutoff or kd > datetime.now(timezone.utc).date():
                continue
            days.setdefault(kd.isoformat(), kd)
        # 由近及远：最近的比赛最可能已完赛
        out = sorted(days.values(), reverse=True)
        return out[:SETTLE_BACKFILL_DAYS]

    async def sync_results(self, fixtures: list[dict[str, Any]] | None = None) -> int:
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

        # ── 回溯补取：只在「今天」窗口取数的历史缺陷 ──────────────────
        # pending 返回的是**所有**未结算预测（含昨天、前天的），
        # 但 get_today_fixtures 只拉「今天」的赛程。于是隔夜的比赛
        # 永远匹配不上，比分永远回写不进去 —— 命中率统计因此长期缺失样本，
        # 复盘与校准都成了无米之炊。
        # 这里按 pending 实际缺失的日期补拉，且只补真正缺的那些天，
        # 避免为无关日期白烧 API 额度。
        missing_days = self._missing_result_days(pending, by_id)
        for d in missing_days:
            try:
                extra = await self._fetch_all_by_date(d)
            except Exception as exc:
                log.warning("回溯赛果：%s 取数失败：%s", d, exc)
                continue
            for fx in extra or []:
                fid = str((fx.get("fixture") or {}).get("id"))
                if fid not in by_id:
                    by_id[fid] = fx

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

    async def backfill_stale_settlements(self, *, max_days: int = 40) -> dict[str, Any]:
        """把「超期未结算」的历史遗留补上赛果，让它们重新进入统计。

        为什么需要：pending() 加了时间下界后，超期行不再堵死结算队列
        （这是好事），但它们**永远不会被结算**（这是代价）——
        看板上「超期未结算 463 场」就是这批。它们既不进命中率，
        也永远不会消失。

        按天补而不是按场补：数据源一次请求返回当天全部比赛，
        故成本是「天数」而非「场次数」。463 场若分布在 40 天里 = 40 次请求。

        Args:
            max_days: 本次最多补多少天（额度保护）。剩余的天下次再补。

        ID 匹配的两层问题
        ------------------
        第一层：库里可能是 ``fd-555051``（备用源前缀）或 ``555051``（主源），
        只精确匹配一种会导致「取到了却补不回来」（PR #103 已处理）。

        第二层（更隐蔽，**前缀剥离对它无效**）：两个源的 ID **不在同一个
        编号空间**。主源 API-Football 的 ID 是 1500000 量级；备用源
        football-data.org 用的是自己的编号（实测样例 560593 量级）。
        所以 ``fd-560593`` 剥掉前缀得到 ``560593``，与主源返回的
        ``1570410`` 毫无关系——线上线下都对不上。

        前缀剥离只对「同一源内部」有效（赛程页 JOIN 就是那种情况），
        对**跨源**匹配必然失败。故此处在 ID 匹配之后再加一道
        「日期 + 双方队名」兜底（见 _match_by_teams）。

        Returns:
            {scanned_days, settled, remaining_before, remaining_after,
             days_done, failed_days, fetched_fixtures, finished_fixtures,
             unmatched_sample}。
            额度耗尽或取数失败都会如实反映在 failed_days 里，不上报为成功。
            fetched/finished/unmatched_sample 用于区分
            「没取到数据」与「取到了但 ID 对不上」——两者表象都是补回 0 场，
            但处置完全不同。
        """
        days = self.repo.stale_pending_days(limit=int(max_days))
        if not days:
            return {"scanned_days": 0, "settled": 0,
                    "remaining_before": self.repo.count_stale_pending(),
                    "remaining_after": self.repo.count_stale_pending(),
                    "days_done": 0, "failed_days": [],
                    "fetched_fixtures": 0, "finished_fixtures": 0,
                    "unmatched_sample": [], "matched_by_name": 0,
                    "id_kinds": self.repo.stale_pending_id_kinds(),
                    "unmatched_detail": []}

        before = self.repo.count_stale_pending()
        settled = 0
        matched_by_name = 0
        failed: list[str] = []
        fetched = 0
        finished = 0
        unmatched: list[str] = []
        undetails: list[tuple[str, str, str, str, str]] = []
        for d in days:
            try:
                # ⚠️ 必须转成 date 再传：stale_pending_days() 返回的是
                # **ISO 字符串**，而取数接口内部调 day.isoformat()——
                # str 没有该方法，会抛 AttributeError。而这个异常会被
                # 取数函数内层的 except 吞掉并静默返回 []，于是外层看到
                # 「没抛异常」就记为「取数成功」，实际取到 0 场。
                # 实测：str("2026-10-19").isoformat() → AttributeError。
                # 这个坑让回填扫了 17 天、显示「成功取数 17 天」，
                # 却一场都没补回来，且不报错——最难排查的一类故障。
                #
                # ⚠️ 同时刻意不走 _fetch_all_by_date：那个方法按**当前
                # 配置的联赛**过滤，而僵尸行可能属于历史配置过、后来去掉
                # 的联赛（或国家队赛事）。回填靠 fixture_id 精确匹配，
                # ID 本身就是足够严格的过滤条件，再加联赛过滤只会漏掉
                # 本该补回的场次。
                fixtures = await self._fetch_all_by_date_raw(d)
            except Exception as exc:
                log.warning("结算回填：%s 取数失败：%s", d, exc)
                failed.append(d)
                continue
            by_id = {str((fx.get("fixture") or {}).get("id")): fx
                     for fx in fixtures or []}
            fetched += len(by_id)
            if not by_id:
                # 该日无数据（可能是无效日期或数据源无该日记录）。
                # 不标记为失败——它不是错误，只是没取到东西。
                continue
            finished_today: list[dict] = []
            for fx in by_id.values():
                short = ((fx.get("fixture") or {}).get("status") or {}).get("short")
                if short not in _FINISHED:
                    continue
                goals = fx.get("goals") or {}
                home, away = goals.get("home"), goals.get("away")
                if home is None or away is None:
                    continue
                finished += 1
                finished_today.append(fx)
                fid = str((fx.get("fixture") or {}).get("id"))
                if self._settle_any_id(fid, int(home), int(away), fx):
                    settled += 1
                elif len(unmatched) < 6:
                    unmatched.append(fid)

            # ── 队名兜底：跨源 ID 天然对不上时的唯一出路 ──
            # 备用源行的 ID（fd-560593）在主源的编号空间里根本不存在，
            # 再怎么剥离前缀也匹配不上。但这些行有 home/away 队名和日期，
            # 而当天全部比赛**已经取回来了**，不需要额外请求就能按名字定位。
            # 不匹配就什么都不做（安全）；匹配上且唯一才写库。
            if finished_today:
                by_name = self._settle_by_team_names(d, finished_today)
                settled += by_name
                matched_by_name += by_name
                undetails.extend(getattr(self, "_last_unmatched_detail", []) or [])

        after = self.repo.count_stale_pending()
        result = {
            "scanned_days": len(days),
            "settled": settled,
            "remaining_before": before,
            "remaining_after": after,
            "days_done": len(days) - len(failed),
            "failed_days": failed,
            "fetched_fixtures": fetched,
            "finished_fixtures": finished,
            "unmatched_sample": unmatched,
            # 补回 0 场时的定位信息：这批僵尸行到底是哪些联赛、哪些日期。
            "leagues": self.repo.stale_pending_leagues(),
            "date_range": self.repo.stale_pending_date_range(),
            "matched_by_name": matched_by_name,
            "id_kinds": self.repo.stale_pending_id_kinds(),
            # 剩余场次的**逐条明细**：看不到这些就只能笼统说「ID 对不上」，
            # 而实际原因可能是队名写法差异（可修）或比赛已被移除（不可修）。
            "unmatched_detail": undetails[:8],
        }
        if settled:
            log.info("结算回填：补回 %d 场（%d→%d）", settled, before, after)
            self.refresh_calibration()
        else:
            # 补回 0 场时必须留下可诊断的信息，否则无从判断卡在哪一环。
            log.warning("结算回填：0 场补回（扫描 %d 天，取到 %d 场，"
                        "其中已完场 %d）。库内僵尸行 ID 样例：%s；"
                        "未匹配上的数据源 ID 样例：%s",
                        len(days), fetched, finished,
                        self.repo.stale_pending_sample_ids(), unmatched)
        return result

    def _settle_by_team_names(self, day: str, finished: list[dict]) -> int:
        """按「日期 + 双方队名」给跨源 ID 的僵尸行补赛果。

        为什么需要：备用源（football-data.org）的 ID 在自己的编号空间里
        （实测样例 560593 量级），主源 API-Football 是 1500000 量级。
        ``fd-560593`` 剥掉前缀也变不出主源的 ID，ID 匹配必然失败。

        但这些行存了 home/away 队名和 kickoff 日期，而当天全部比赛
        **已经取回来了**，不需要额外请求——按名字定位不花额度。

        安全边界（宁可不补，也不写错比分）：
        - 只有**双方队名都匹配**才考虑；
        - 当天候选中**必须恰好一个**，有歧义就放弃；
        - 比分缺失或不是已完场的不参与（由调用方保证）。

        写错的代价比不写高得多：错误的比分会直接进命中率统计，
        污染模型评估；而少补一场只是样本少一点。

        Returns:
            成功补回的场次数。
        """
        rows = self.repo.stale_pending_rows_for_day(day)
        if not rows:
            return 0
        # 预先建索引，避免对每行都遍历全部比赛
        index: dict[tuple[str, str], list[dict]] = {}
        for fx in finished:
            teams = fx.get("teams") or {}
            hk = _team_key(((teams.get("home") or {}).get("name")) or "")
            ak = _team_key(((teams.get("away") or {}).get("name")) or "")
            if not hk or not ak:
                continue
            index.setdefault((hk, ak), []).append(fx)

        done = 0
        undetail: list[tuple[str, str, str, str, str]] = []
        for fid, home_name, away_name in rows:
            hk = _team_key(home_name or "")
            ak = _team_key(away_name or "")
            if not hk or not ak:
                continue
            cands = index.get((hk, ak)) or []
            if len(cands) != 1:      # 0 个=没匹配；≥2 个=有歧义，一律放弃
                if len(undetail) < 8:
                    undetail.append((
                        day, str(fid), home_name, away_name,
                        "无同名比赛" if not cands else f"有 {len(cands)} 场同名"))
                continue
            fx = cands[0]
            goals = fx.get("goals") or {}
            home, away = goals.get("home"), goals.get("away")
            if home is None or away is None:
                continue
            if self.repo.settle(str(fid), int(home), int(away)):
                self._sync_elo(fx, int(home), int(away))
                done += 1
        self._last_unmatched_detail = undetail
        return done

    def _settle_any_id(self, fid: str, home: int, away: int,
                       fixture: dict[str, Any]) -> bool:
        """用兼容两种写法的 ID 回写赛果，成功则同步 Elo。

        库里的 fixture_id 可能是 ``fd-555051``（备用源）或 ``555051``（主源）。
        数据源按日期返回的固定是其中一种，故逐个尝试，命中即止。
        绝不「两种都写」——那会重复同步 Elo，把评分算两遍。
        """
        for cand in self._id_candidates(fid):
            if self.repo.settle(cand, home, away):
                self._sync_elo(fixture, home, away)
                return True
        return False

    @staticmethod
    def _id_candidates(fid: str) -> list[str]:
        """生成 ID 的所有可能写法，按最可能命中的顺序排列。"""
        s = str(fid)
        if s.startswith("fd-"):
            return [s, s[3:]]
        return [s, f"fd-{s}"]

    def _sync_elo(self, fixture: dict[str, Any], home_score: int, away_score: int) -> None:
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

    def stats(self) -> dict[str, Any]:
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

    def stored_prediction(self, fixture_id) -> dict[str, Any] | None:
        """库里已落盘的预测结论；没有（或概率不全）返回 None。

        与 get() 的区别：get() 取内存里的完整 Prediction（重启即失），
        这个只读库里的落盘数字，**重启后依然可用**，且不依赖赛程快照。

        用途：赛程快照是后来才加的列，更早的预测只有结论没有快照；
        比赛开赛或跨天后今日赛程里也查不到，用户点旧卡片会撞上
        「该场比赛已不在今日赛程中」——可结论就在库里。
        回看旧结论不需要重算，所以这里直接把落盘数字交出去展示。
        """
        if self.repo is None:
            return None
        return self.repo.get_stored_prediction(fixture_id)

    async def refresh(self, fixture_id) -> Prediction:
        """重新拉取最新赔率并重算价值偏差（球队强度沿用当时的模型）。"""
        old = self.get(fixture_id)
        new = await self._predict_one(old.model, old.kickoff, old.fixture, fresh=True)
        self._remember(new)
        return new

    async def get_h2h(self, prediction: Prediction, last: int = 5) -> list[dict[str, Any]]:
        return await self.api.get_h2h(prediction.home_id, prediction.away_id, last)

    @property
    def cached_predictions(self) -> int:
        return len(self._store)
