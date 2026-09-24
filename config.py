"""环境变量配置：集中解析、校验，缺少必需变量时给出明确的错误信息。"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time

import pytz

log = logging.getLogger(__name__)

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(RuntimeError):
    """配置缺失或格式错误。"""


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
    if hour is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.warning("PUSH_TIME=%r 无法解析，改用默认值 %s", raw, default.strftime("%H:%M"))
        return default
    return time(hour, minute)


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
    season: int
    push_time: time
    timezone: object  # pytz 时区对象
    log_level: str
    max_matches: int
    lookahead_hours: int

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

    # 推送时间：优先 PUSH_TIME；兼容 v2.0 引入的 SCHEDULED_HOUR / SCHEDULED_MINUTE
    push_raw = _get(env, "PUSH_TIME")
    if push_raw is None and (_get(env, "SCHEDULED_HOUR") or _get(env, "SCHEDULED_MINUTE")):
        push_raw = f"{_get(env, 'SCHEDULED_HOUR', '8')}:{_get(env, 'SCHEDULED_MINUTE', '0')}"

    return Settings(
        telegram_token=_get(env, "TELEGRAM_TOKEN"),
        api_key=api_key,
        api_provider=provider,
        chat_id=chat_id,
        admin_ids=admin_ids,
        league_id=_get_int(env, "LEAGUE_ID", 39),  # 39 = 英超
        season=_get_int(env, "SEASON", default_season()),
        push_time=parse_push_time(push_raw),
        timezone=tz,
        log_level=level,
        max_matches=_get_int(env, "MAX_MATCHES", 3),
        lookahead_hours=_get_int(env, "LOOKAHEAD_HOURS", 36),
    )
