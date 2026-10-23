"""环境变量配置：集中解析、校验，缺少必需变量时给出明确的错误信息。"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查用；运行时本模块已依赖 pytz
    from pytz.tzinfo import BaseTzInfo

import pytz

from paths import DB_PATH

from ensemble import DEFAULT_ETA
from context import DEFAULT_RATING_STRENGTH, DEFAULT_WEATHER_STRENGTH
from prematch import (
    DEFAULT_STRENGTH,
    DEFAULT_TILT,
    STRENGTH_MAX,
    STRENGTH_MIN,
    TILT_MAX,
)
from analyzer import (
    DEFAULT_MARKET_WEIGHT,
    MARKET_WEIGHT_MAX,
    MARKET_WEIGHT_MIN,
)

log = logging.getLogger(__name__)

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_SEASON_MODES = {"auto", "fixed"}
VALID_DATA_SOURCE_MODES = {"auto", "api-football", "football-data"}

DEFAULT_DB_PATH = None  # 由 paths.DB_PATH 统一决定（默认 /data/football.db）
TRUE_VALUES = {"1", "true", "yes", "on", "y"}

# 赛前刷新间隔（分钟）。0 = 关闭该任务。
# 为什么是 15 分钟：首发阵容官方写明「开赛前 20–40 分钟公布」，
# 45 分钟窗口内跑 3 轮，既能覆盖不同联赛公布时间参差不齐的情况，
# 又不至于每轮都重复请求（拿到阵容后该场会被标记，后续轮次跳过）。
# 上限 180 是防止误填成 0.5 这类小数被当成分钟而刷爆额度。
DEFAULT_PREMATCH_REFRESH_MINUTES = 15
PREMATCH_REFRESH_MAX_MINUTES = 180

# 赛前二次推送：开赛前多久进入"可推送"窗口（分钟）。
#   用户要的是"赛前一小时用实时数据再推一次"。但官方明确说明首发阵容
#   要开赛前 20–40 分钟才公布，T-60 拿到的往往只有更新过的伤停与赔率。
#   因此在窗口内**优先等阵容**：拿到阵容立刻推；始终拿不到（部分赛事
#   官方不提供）则在 PREMATCH_PUSH_FORCE_MINUTES 兜底推一次，
#   免得这场比赛永远等不到更新。两种情形都只推一次，靠 prematch_push 表去重。
DEFAULT_PREMATCH_PUSH_LEAD_MINUTES = 60
DEFAULT_PREMATCH_PUSH_FORCE_MINUTES = 20


class ConfigError(RuntimeError):
    """配置缺失或格式错误。"""


def parse_bool(raw: str | None, default: bool = True) -> bool:
    """把 'true/false/1/0/yes/no' 解析为布尔值，无法识别时回退默认值。"""
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    log.warning("ALLOW_SEASON_FALLBACK=%r 无法识别，改用默认值 %s", raw, default)
    return default


def default_season(today: date | None = None) -> int:
    """欧洲联赛的赛季以开赛年份命名，7 月起算新赛季（2026 年 9 月 → 2026 赛季）。"""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def parse_push_time(raw: str | None, default: time = time(8, 0)) -> time:
    """解析推送时间。支持 '08:00' / '8:00' / '8' / '0800' / '08:00:00'，失败时回退默认值。"""
    if not raw:
        return default
    text = raw.strip()
    hour = minute = None
    m = re.fullmatch(r"(\d{1,2})(?:[:：](\d{1,2}))?(?:[:：]\d{1,2})?", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
    elif re.fullmatch(r"\d{4}", text):
        hour, minute = int(text[:2]), int(text[2:])
    if hour is None or minute is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.warning("PUSH_TIME=%r 无法解析，改用默认值 %s", raw, default.strftime("%H:%M"))
        return default
    return time(hour, minute)


def _plus_hours(t: time, hours: int) -> time:
    """时间加 N 小时（跨日回绕）。用于推导复盘推送的默认时刻。"""
    return time((t.hour + hours) % 24, t.minute)


def parse_id_list(raw: str | None) -> frozenset[int]:
    """解析以逗号/空格/分号分隔的数字 ID 列表。"""
    ids: set[int] = set()
    for token in re.split(r"[,\s;，]+", raw or ""):
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            log.warning("ADMIN_ID 中的 %r 不是有效的数字 ID，已忽略", token)
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    api_key: str
    api_provider: str  # "rapidapi"（RAPID_API_KEY）或 "apisports"（API_FOOTBALL_KEY，官方直连）
    chat_id: str | None
    admin_ids: frozenset[int]
    league_id: int
    league_ids: tuple[int, ...]  # 全部启用联赛；league_ids[0] 恒等于 league_id
    season: int
    push_time: time
    review_time: time  # 每日复盘推送时刻（REVIEW_TIME，默认比推送晚 1 小时）
    timezone: "BaseTzInfo"  # pytz 时区对象
    log_level: str
    max_matches: int
    lookahead_hours: int
    topn_enabled: bool
    topn_min: int
    topn_max: int
    topn_min_confidence: float
    season_mode: str  # "auto"（自动识别可用赛季并降级）/ "fixed"（只用目标赛季，不降级）
    allow_season_fallback: bool
    # 备用数据源 football-data.org（主源不可用时接管赛程/赛果/积分榜）
    football_data_token: str
    football_data_enabled: bool
    football_data_timeout: float
    data_source_mode: str  # "auto" / "api-football" / "football-data"
    db_path: str  # 预测落盘路径（SQLite）；目录不可写时自动回退内存存储
    web_url: str  # 网页看板地址（WEB_URL）；为空时 /web 提示未部署
    market_weight: float  # 市场锚定权重 w：0=纯模型，1=纯市场（见 analyzer 模块说明）
    ensemble_learn: bool  # 集成器是否开启动态权重学习（ENSEMBLE_LEARN）
    ensemble_eta: float   # 动态权重学习率 η（ENSEMBLE_ETA，见 ensemble 模块说明）
    # ---- 赛前实时信号（伤停 / 首发 / 球员评估 / 天气）----------------------
    # ⚠️ 这五个字段此前**只被解析、从未传进 Settings**，导致 service.py 里
    #    getattr(settings, "prematch_enabled", False) 恒取默认 False ——
    #    整个赛前信号在生成环境是死代码，且 PREMATCH_ENABLED 无法关闭、
    #    RATING_STRENGTH 无法开启。必须显式声明并传入，否则配置形同虚设。
    prematch_enabled: bool    # PREMATCH_ENABLED：总开关，关闭后行为与接入前逐位一致
    prematch_strength: float  # PREMATCH_STRENGTH：伤停/阵容折损强度，0 = 只接线不改结果
    prematch_tilt: float      # PREMATCH_TILT：阵型攻守倾向权重
    rating_strength: float    # RATING_STRENGTH：首发评分强度，0 = 不启用球员评估
    weather_strength: float   # WEATHER_STRENGTH：天气强度，默认 0（关闭）
    signal_ab_enabled: bool   # SIGNAL_AB_ENABLED：赛前信号对照采集，默认开启
    signal_ab_write_enabled: bool
    prematch_refresh_minutes: int
    # PREMATCH_REFRESH_MINUTES：赛前刷新间隔（分钟），0 = 不启用
    # 存在的理由见 service.prematch_refresh —— 自动预测 6 小时一轮，
    # 而首发阵容开赛前 20–40 分钟才公布，两者对不上时阵容信号永远取不到。
    prematch_push_enabled: bool
    # PREMATCH_PUSH_ENABLED：赛前二次推送总开关，默认开启
    prematch_push_lead_minutes: int
    # PREMATCH_PUSH_LEAD_MINUTES：进入窗口后允许推送（默认 60 分钟）
    prematch_push_force_minutes: int
    # PREMATCH_PUSH_FORCE_MINUTES：临近开赛仍未拿到阵容时的兜底推送点
    # SIGNAL_AB_WRITE_ENABLED：**默认关闭**。只有显式开启才允许改信号权重，
    # 线上服务不给写权限 —— 这样任何代码 bug 都不可能意外改掉权重。

    @property
    def requested_season(self) -> int:
        """用户期望的赛季（REQUESTED_SEASON → SEASON → 按日期推算）。"""
        return self.season

    @property
    def football_data_available(self) -> bool:
        """备用源是否可用：必须显式启用且配置了 Token（Token 缺失时安全降级为不可用）。"""
        return self.football_data_enabled and bool(self.football_data_token)

    @property
    def chat_target(self) -> int | str | None:
        """Telegram 的 chat_id 可能是整数（用户/群组）或 '@频道名'。"""
        if self.chat_id is None:
            return None
        try:
            return int(self.chat_id)
        except ValueError:
            return self.chat_id

    @property
    def expected_season(self) -> int:
        """按当前日期推算出的赛季，用来提示/兜底 SEASON 变量过期的情况。"""
        return default_season()


def _get(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _get_int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} 必须是整数，当前值：{raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，当前值：{value}")
    return value


def _get_float(env: Mapping[str, str], name: str, default: float,
               minimum: float = 0.0, maximum: float = 1.0) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} 必须是数字，当前值：{raw!r}") from None
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} 必须在 {minimum}~{maximum} 之间，当前值：{value}")
    return value


def _parse_league_ids(env: Mapping[str, str], primary: int) -> tuple[int, ...]:
    """解析 LEAGUE_IDS（逗号分隔）。

    - 未设置 / 为空时退化为单联赛 (primary,)，行为与旧版完全一致
    - 去重且保持原顺序
    - 结果首个元素恒为 primary：若用户列表不含 primary，则自动插到最前
    """
    raw = _get(env, "LEAGUE_IDS")
    if not raw:
        return (primary,)
    items: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            raise ConfigError(f"LEAGUE_IDS 必须是逗号分隔的整数，当前值：{raw!r}") from None
        if value < 1:
            raise ConfigError(f"LEAGUE_IDS 中的联赛编号不能小于 1，当前值：{value}")
        if value not in items:
            items.append(value)
    if not items:
        return (primary,)
    if items[0] != primary:
        items = [primary] + [v for v in items if v != primary]
    return tuple(items)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    missing = []
    if not _get(env, "TELEGRAM_TOKEN"):
        missing.append("TELEGRAM_TOKEN")
    direct_key, rapid_key = _get(env, "API_FOOTBALL_KEY"), _get(env, "RAPID_API_KEY")
    if not (direct_key or rapid_key):
        missing.append("RAPID_API_KEY（或官方直连的 API_FOOTBALL_KEY）")
    if missing:
        raise ConfigError("缺少必需的环境变量：" + ", ".join(missing))
    api_key, provider = (direct_key, "apisports") if direct_key else (rapid_key, "rapidapi")

    tz_name = _get(env, "TIMEZONE", "Asia/Shanghai")
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        raise ConfigError(f"TIMEZONE 无法识别：{tz_name!r}（示例：Asia/Shanghai）") from None

    chat_id = _get(env, "CHAT_ID")
    admin_ids = parse_id_list(_get(env, "ADMIN_ID"))
    if not admin_ids and chat_id and re.fullmatch(r"\d+", chat_id):
        admin_ids = frozenset({int(chat_id)})  # 私聊场景下 chat_id 就是用户 ID

    level = (_get(env, "LOG_LEVEL", "INFO") or "INFO").upper()
    if level not in VALID_LOG_LEVELS:
        level = "INFO"

    # 赛季策略：REQUESTED_SEASON 优先于 SEASON，便于明确区分「期望赛季」与「实际赛季」
    season = _get_int(env, "REQUESTED_SEASON", None) or _get_int(env, "SEASON", default_season())
    season_mode = (_get(env, "SEASON_MODE", "auto") or "auto").strip().lower()
    if season_mode not in VALID_SEASON_MODES:
        log.warning("SEASON_MODE=%r 无法识别，改用默认值 auto", season_mode)
        season_mode = "auto"
    allow_season_fallback = parse_bool(_get(env, "ALLOW_SEASON_FALLBACK"), default=True)

    # 备用数据源 football-data.org
    data_source_mode = (_get(env, "DATA_SOURCE_MODE", "auto") or "auto").strip().lower()
    if data_source_mode not in VALID_DATA_SOURCE_MODES:
        log.warning("DATA_SOURCE_MODE=%r 无法识别，改用默认值 auto", data_source_mode)
        data_source_mode = "auto"
    football_data_token = _get(env, "FOOTBALL_DATA_API_TOKEN") or ""
    football_data_enabled = parse_bool(_get(env, "FOOTBALL_DATA_ENABLED"), default=True)
    football_data_timeout = float(_get_int(env, "FOOTBALL_DATA_TIMEOUT", 10) or 10)

    # 预测落盘：默认走 paths 模块（DATA_DIR/DATABASE_PATH），挂载卷生效时重启不丢。
    # DATABASE_PATH 与 DB_PATH 等价，前者优先；两者都未设置时才用 paths.DB_PATH。
    # 用 str 而非 Path：repository 以该值做连接缓存的 dict key，
    # 类型需与回退路径 str(paths.DB_PATH) 一致，否则同一库会打开两条连接。
    db_path = _get(env, "DATABASE_PATH") or _get(env, "DB_PATH") or str(DB_PATH)

    # 网页看板地址。Railway 上可用 ${{RAILWAY_PUBLIC_DOMAIN}} 之类的变量注入；
    # 未配置时 /web 会明确提示未部署，而不是给出一条打不开的链接。
    web_url = (_get(env, "WEB_URL") or "").strip().rstrip("/")

    # 市场锚定权重：P_final = w·市场 + (1−w)·模型。
    # 非法值（非数字 / 越界）一律回退默认，绝不因为配错就把预测算废。
    market_weight = DEFAULT_MARKET_WEIGHT
    raw_w = _get(env, "MARKET_BLEND_WEIGHT")
    if raw_w:
        try:
            parsed_w = float(raw_w)
        except ValueError:
            log.warning("MARKET_BLEND_WEIGHT 不是数字，回退默认 %s：%r",
                        DEFAULT_MARKET_WEIGHT, raw_w)
        else:
            if MARKET_WEIGHT_MIN <= parsed_w <= MARKET_WEIGHT_MAX:
                market_weight = parsed_w
            else:
                log.warning(
                    "MARKET_BLEND_WEIGHT 越界 [%s, %s]，回退默认 %s：%r",
                    MARKET_WEIGHT_MIN, MARKET_WEIGHT_MAX,
                    DEFAULT_MARKET_WEIGHT, raw_w)

    # 多模型集成（ensemble 模块）：
    #   ENSEMBLE_LEARN=1 → 权重按各路实测表现动态调整（η 越大越倾向强者）。
    #   默认关闭 → 用固定经验权重（市场 0.75），行为完全确定、便于回归测试。
    #   实测（英超三赛季 1140 场走前）：动态模式 η=200 时市场权重收敛到 0.842。
    # 赛前实时信号（伤停 / 首发阵容）：
    #   这是模型第一次拥有市场之外的独立信息，但权重**未经本项目语料校准**
    #   ——回测语料没有伤停/阵容列，拟合不了。故默认半强度，并保留关闭开关，
    #   待线上积累 A/B 样本后再定标。详见 prematch.py 顶部说明。
    prematch_enabled = parse_bool(_get(env, "PREMATCH_ENABLED"), True)
    prematch_strength = DEFAULT_STRENGTH
    raw_ps = _get(env, "PREMATCH_STRENGTH")
    if raw_ps:
        try:
            parsed_ps = float(raw_ps)
        except ValueError:
            log.warning("PREMATCH_STRENGTH 不是数字，回退默认 %s：%r",
                        DEFAULT_STRENGTH, raw_ps)
        else:
            if STRENGTH_MIN <= parsed_ps <= STRENGTH_MAX:
                prematch_strength = parsed_ps
            else:
                log.warning("PREMATCH_STRENGTH 越界 [%s, %s]，回退默认 %s：%r",
                            STRENGTH_MIN, STRENGTH_MAX, DEFAULT_STRENGTH, raw_ps)
    prematch_tilt = DEFAULT_TILT
    raw_pt = _get(env, "PREMATCH_TILT")
    if raw_pt:
        try:
            parsed_pt = float(raw_pt)
        except ValueError:
            log.warning("PREMATCH_TILT 不是数字，回退默认 %s：%r", DEFAULT_TILT, raw_pt)
        else:
            if 0.0 <= parsed_pt <= TILT_MAX:
                prematch_tilt = parsed_pt
            else:
                log.warning("PREMATCH_TILT 越界 [0, %s]，回退默认 %s：%r",
                            TILT_MAX, DEFAULT_TILT, raw_pt)

    # 球员评估（首发评分）：默认半强度，与伤停口径一致。
    #   0 = 只接线不改结果（用于 A/B 对照）。
    rating_strength = DEFAULT_RATING_STRENGTH
    raw_rs = _get(env, "RATING_STRENGTH")
    if raw_rs:
        try:
            parsed_rs = float(raw_rs)
        except ValueError:
            log.warning("RATING_STRENGTH 不是数字，回退默认 %s：%r",
                        DEFAULT_RATING_STRENGTH, raw_rs)
        else:
            if STRENGTH_MIN <= parsed_rs <= STRENGTH_MAX:
                rating_strength = parsed_rs
            else:
                log.warning("RATING_STRENGTH 越界 [%s, %s]，回退默认 %s：%r",
                            STRENGTH_MIN, STRENGTH_MAX, DEFAULT_RATING_STRENGTH, raw_rs)

    # 天气：**默认 0（关闭）**。公开研究结论互相矛盾（英超湿润月份 +0.22 球/场
    #   与英冠 4826 场「降雨无显著效应」并存），且本项目尚未验证。
    #   天气是四类信号里唯一可用历史归档回测的，启用前应在自己库上验证为正。
    weather_strength = DEFAULT_WEATHER_STRENGTH
    raw_ws = _get(env, "WEATHER_STRENGTH")
    if raw_ws:
        try:
            parsed_ws = float(raw_ws)
        except ValueError:
            log.warning("WEATHER_STRENGTH 不是数字，回退默认 %s：%r",
                        DEFAULT_WEATHER_STRENGTH, raw_ws)
        else:
            if STRENGTH_MIN <= parsed_ws <= STRENGTH_MAX:
                weather_strength = parsed_ws
            else:
                log.warning("WEATHER_STRENGTH 越界 [%s, %s]，回退默认 %s：%r",
                            STRENGTH_MIN, STRENGTH_MAX, DEFAULT_WEATHER_STRENGTH, raw_ws)

    # 赛前信号对照采集：**默认开启**。它只是旁路写库（每行几 KB 的三项概率），
    #   不参与预测计算，关掉它就永远攒不出"信号到底有没有用"的证据，
    #   权重就只能继续拍脑袋。真正要关的场景只有：库容量紧张、或确认不打算
    #   做信号定标。注意它依赖 prematch_enabled —— 信号都没施加就没有对照可言。
    signal_ab_enabled = parse_bool(_get(env, "SIGNAL_AB_ENABLED"), True)

    # 权重写权限：**默认关闭**。改权重是本项目唯一会真实改变预测结果的操作，
    #   比改代码更危险（代码错了测试会失败，权重错了只是悄悄变差）。
    #   线上服务只开读权限，调权重走 signal_admin.apply_weight 并二次确认。
    signal_ab_write_enabled = parse_bool(_get(env, "SIGNAL_AB_WRITE_ENABLED"), False)

    # 赛前刷新间隔：默认 15 分钟。设为 0 表示不启用该定时任务
    #   （例如只想手动点预测、或 API 额度紧张）。
    prematch_refresh_minutes = DEFAULT_PREMATCH_REFRESH_MINUTES
    raw_prm = _get(env, "PREMATCH_REFRESH_MINUTES")
    if raw_prm:
        try:
            parsed_prm = int(float(raw_prm))
        except (TypeError, ValueError):
            log.warning("PREMATCH_REFRESH_MINUTES 不是数字，回退默认 %s：%r",
                        DEFAULT_PREMATCH_REFRESH_MINUTES, raw_prm)
        else:
            if 0 <= parsed_prm <= PREMATCH_REFRESH_MAX_MINUTES:
                prematch_refresh_minutes = parsed_prm
            else:
                log.warning("PREMATCH_REFRESH_MINUTES 越界 [0, %s]，回退默认 %s：%r",
                            PREMATCH_REFRESH_MAX_MINUTES,
                            DEFAULT_PREMATCH_REFRESH_MINUTES, raw_prm)

    # 赛前二次推送：**默认开启**。赛前刷新只是把新概率落库，不推送的话
    #   用户看到的还是早上 08:00 那条旧预测 —— 赛前信号接了等于没接。
    prematch_push_enabled = parse_bool(_get(env, "PREMATCH_PUSH_ENABLED"), True)

    def _minutes_env(key: str, default: int) -> int:
        raw = _get(env, key)
        if not raw:
            return default
        try:
            parsed = int(float(raw))
        except (TypeError, ValueError):
            log.warning("%s 不是数字，回退默认 %s：%r", key, default, raw)
            return default
        if not 0 <= parsed <= 24 * 60:
            log.warning("%s 越界 [0, %d]，回退默认 %s：%r", key, 24 * 60, default, raw)
            return default
        return parsed

    prematch_push_lead_minutes = _minutes_env(
        "PREMATCH_PUSH_LEAD_MINUTES", DEFAULT_PREMATCH_PUSH_LEAD_MINUTES)
    prematch_push_force_minutes = _minutes_env(
        "PREMATCH_PUSH_FORCE_MINUTES", DEFAULT_PREMATCH_PUSH_FORCE_MINUTES)

    ensemble_learn = _get(env, "ENSEMBLE_LEARN", "").strip().lower() in (
        "1", "true", "yes", "on")
    ensemble_eta = DEFAULT_ETA
    raw_eta = _get(env, "ENSEMBLE_ETA")
    if raw_eta:
        try:
            parsed_eta = float(raw_eta)
        except ValueError:
            log.warning("ENSEMBLE_ETA 不是数字，回退默认 %s：%r",
                        DEFAULT_ETA, raw_eta)
        else:
            if parsed_eta >= 0:
                ensemble_eta = parsed_eta
            else:
                log.warning("ENSEMBLE_ETA 为负，回退默认 %s：%r",
                            DEFAULT_ETA, raw_eta)

    # 推送时间：优先 PUSH_TIME；兼容 v2.0 引入的 SCHEDULED_HOUR / SCHEDULED_MINUTE
    push_raw = _get(env, "PUSH_TIME")
    if push_raw is None and (_get(env, "SCHEDULED_HOUR") or _get(env, "SCHEDULED_MINUTE")):
        push_raw = f"{_get(env, 'SCHEDULED_HOUR', '8')}:{_get(env, 'SCHEDULED_MINUTE', '0')}"

    _league_primary = _get_int(env, "LEAGUE_ID", 39)  # 39 = 英超

    return Settings(
        telegram_token=_get(env, "TELEGRAM_TOKEN"),
        api_key=api_key,
        api_provider=provider,
        chat_id=chat_id,
        admin_ids=admin_ids,
        league_id=_league_primary,  # 39 = 英超
        league_ids=_parse_league_ids(env, _league_primary),
        push_time=parse_push_time(push_raw),
        # 复盘推送时间默认比每日推送晚 1 小时：复盘要读「昨晚已结算的赛果」，
        # 与推送挤在同一时刻会读到尚未结算完的数据，结论必然偏低。
        review_time=parse_push_time(_get(env, "REVIEW_TIME"),
                                    default=_plus_hours(parse_push_time(push_raw), 1)),
        timezone=tz,
        log_level=level,
        # 默认必须能覆盖一个完整比赛日：五大联赛一天可达 30+ 场，
        # 默认 3 会让「赛程 4 场 / 预测 3 场」这种不一致成为常态。
        max_matches=_get_int(env, "MAX_MATCHES", 50),
        lookahead_hours=_get_int(env, "LOOKAHEAD_HOURS", 36),
        # 只推最有把握的 N 场：场次铺满会把重点淹没，且低把握场次拉低整体命中率。
        # 下限保证「有东西可推」，上限保证「宁缺毋滥」，两者之间是弹性区间。
        topn_enabled=parse_bool(_get(env, "TOPN_ENABLED"), True),
        topn_min=_get_int(env, "TOPN_MIN", 3),
        topn_max=_get_int(env, "TOPN_MAX", 7),
        topn_min_confidence=_get_float(env, "TOPN_MIN_CONFIDENCE", 0.0),
        season=season,
        season_mode=season_mode,
        allow_season_fallback=allow_season_fallback,
        football_data_token=football_data_token,
        football_data_enabled=football_data_enabled,
        football_data_timeout=football_data_timeout,
        data_source_mode=data_source_mode,
        db_path=db_path,
        web_url=web_url,
        market_weight=market_weight,
        ensemble_learn=ensemble_learn,
        ensemble_eta=ensemble_eta,
        prematch_enabled=prematch_enabled,
        prematch_strength=prematch_strength,
        prematch_tilt=prematch_tilt,
        rating_strength=rating_strength,
        weather_strength=weather_strength,
        signal_ab_enabled=signal_ab_enabled,
        signal_ab_write_enabled=signal_ab_write_enabled,
        prematch_refresh_minutes=prematch_refresh_minutes,
        prematch_push_enabled=prematch_push_enabled,
        prematch_push_lead_minutes=prematch_push_lead_minutes,
        prematch_push_force_minutes=prematch_push_force_minutes,
    )


# ============================================================================
# 配置自检
# ============================================================================
# 事故复盘：赛前信号（prematch_* / rating_strength）曾长期"解析了却没传进
# Settings"，线上恒取默认 False/0，整个信号链路是死代码，而日志一片正常。
# 根因是**没有任何地方打印实际生效值** —— 大家以为配了就是配了。
#
# 因此启动时必须把关键字段的真实生效值打出来，并在"环境变量写了、
# 生效值却不一致"时告警。这不是调试便利，是防死代码复发的护栏。
#
# 只列真实存在的字段；不存在的用 getattr 兜底为 None，绝不凭印象编名字。

AUDIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("PREMATCH_ENABLED", "prematch_enabled"),
    ("PREMATCH_STRENGTH", "prematch_strength"),
    ("PREMATCH_TILT", "prematch_tilt"),
    ("RATING_STRENGTH", "rating_strength"),
    ("WEATHER_STRENGTH", "weather_strength"),
    ("MARKET_WEIGHT", "market_weight"),
    ("PREMATCH_REFRESH_MINUTES", "prematch_refresh_minutes"),
    ("PREMATCH_PUSH_ENABLED", "prematch_push_enabled"),
    ("SIGNAL_AB_ENABLED", "signal_ab_enabled"),
)


def audit_settings(settings: Any, env: Mapping[str, str] | None = None
                   ) -> tuple[list[str], list[str]]:
    """返回 (生效值行, 告警行)。

    告警规则：环境变量显式设置了、但 Settings 上的生效值为假值
    （False / 0）—— 这正是"解析了没传进去"的典型症状。
    """
    src: Mapping[str, str] = os.environ if env is None else env
    lines: list[str] = []
    warnings: list[str] = []
    for env_name, attr in AUDIT_FIELDS:
        value = getattr(settings, attr, None)
        lines.append(f"  {env_name}={value!r}")
        raw = src.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        # 显式配置却没生效 —— 八成是漏传字段或解析回退到了默认值
        if value is None or value is False or value == 0:
            warnings.append(
                f"[CONFIG_WARNING] {env_name} 已设为 {raw!r}，"
                f"但生效值是 {value!r}（疑似未接入 Settings）"
            )
    return lines, warnings
