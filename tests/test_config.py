from datetime import date, time

import pytest

from config import (ConfigError, default_season, load_settings, parse_id_list,
                    parse_push_time)

BASE = {"TELEGRAM_TOKEN": "123:abc", "RAPID_API_KEY": "key", "CHAT_ID": "555"}


@pytest.mark.parametrize(
    "raw,expected",
    [("08:00", time(8, 0)), ("8:30", time(8, 30)), ("9", time(9, 0)),
     ("0745", time(7, 45)), ("21：15", time(21, 15)), ("07:05:00", time(7, 5))],
)
def test_parse_push_time_accepts_common_formats(raw, expected):
    assert parse_push_time(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "25:00", "8:75", "早上八点", "abc"])
def test_parse_push_time_falls_back_to_default(raw):
    assert parse_push_time(raw) == time(8, 0)


def test_default_season_switches_in_july():
    assert default_season(date(2026, 9, 24)) == 2026
    assert default_season(date(2027, 3, 1)) == 2026
    assert default_season(date(2026, 6, 30)) == 2025


def test_parse_id_list():
    assert parse_id_list("1, 2;3  4，5 x") == frozenset({1, 2, 3, 4, 5})
    assert parse_id_list(None) == frozenset()


def test_missing_required_variables():
    with pytest.raises(ConfigError, match="TELEGRAM_TOKEN"):
        load_settings({"RAPID_API_KEY": "k"})
    with pytest.raises(ConfigError, match="RAPID_API_KEY"):
        load_settings({"TELEGRAM_TOKEN": "t"})


def test_defaults_and_admin_fallback_to_private_chat_id():
    s = load_settings(BASE)
    # max_matches 默认必须能覆盖一个完整比赛日（默认 3 会让赛程与预测场数对不上）
    assert (s.league_id, s.max_matches, s.lookahead_hours) == (39, 50, 36)
    assert s.timezone.zone == "Asia/Shanghai"
    assert s.push_time == time(8, 0)
    assert s.admin_ids == frozenset({555})
    assert s.chat_target == 555
    assert s.api_provider == "rapidapi"


def test_group_chat_id_is_not_treated_as_admin_and_channel_names_pass_through():
    s = load_settings({**BASE, "CHAT_ID": "-100123"})
    assert s.admin_ids == frozenset() and s.chat_target == -100123
    assert load_settings({**BASE, "CHAT_ID": "@mychannel"}).chat_target == "@mychannel"


def test_explicit_settings_and_legacy_schedule_variables():
    s = load_settings({**BASE, "ADMIN_ID": "7,8", "PUSH_TIME": "07:30", "SEASON": "2025", "TIMEZONE": "UTC"})
    assert s.admin_ids == frozenset({7, 8}) and s.push_time == time(7, 30) and s.season == 2025
    legacy = load_settings({**BASE, "SCHEDULED_HOUR": "9", "SCHEDULED_MINUTE": "15"})
    assert legacy.push_time == time(9, 15)
    assert load_settings({**BASE, "PUSH_TIME": "6:00", "SCHEDULED_HOUR": "9"}).push_time == time(6, 0)


def test_direct_api_sports_key_takes_precedence():
    s = load_settings({**BASE, "API_FOOTBALL_KEY": "direct"})
    assert (s.api_provider, s.api_key) == ("apisports", "direct")
    assert load_settings({"TELEGRAM_TOKEN": "t", "API_FOOTBALL_KEY": "d"}).api_provider == "apisports"


def test_invalid_values_raise_readable_errors():
    with pytest.raises(ConfigError, match="LEAGUE_ID"):
        load_settings({**BASE, "LEAGUE_ID": "英超"})
    with pytest.raises(ConfigError, match="TIMEZONE"):
        load_settings({**BASE, "TIMEZONE": "Mars/Base"})
    assert load_settings({**BASE, "LOG_LEVEL": "loud"}).log_level == "INFO"


def test_prematch_settings_reach_settings_object():
    """回归：赛前信号配置必须真的进入 Settings。

    此前这五个值只被 load_settings 解析、却没传进 Settings(...)，于是
    service.py 里 getattr(settings, "prematch_enabled", False) 恒为 False，
    整个赛前信号（伤停/首发/评分/天气）在生成环境从未生效，且
    PREMATCH_ENABLED 关不掉、RATING_STRENGTH 开不起来。

    这里逐字段断言存在且可改，确保这类"配置被静默丢弃"不再复发。
    """
    s = load_settings(BASE)
    # 默认值：伤停/阵容半强度、评分半强度、天气关闭、总开关开启
    assert s.prematch_enabled is True
    assert s.prematch_strength == 0.5
    assert s.prematch_tilt == 0.0
    assert s.rating_strength == 0.5
    assert s.weather_strength == 0.0


def test_prematch_settings_are_configurable():
    """环境变量必须能真正改变行为（不是被解析后丢弃）。"""
    off = load_settings({**BASE, "PREMATCH_ENABLED": "false"})
    assert off.prematch_enabled is False      # 能关掉
    tuned = load_settings({
        **BASE,
        "PREMATCH_STRENGTH": "0.9",
        "PREMATCH_TILT": "0.15",
        "RATING_STRENGTH": "1.0",
        "WEATHER_STRENGTH": "0.3",
    })
    assert tuned.prematch_strength == 0.9
    assert tuned.prematch_tilt == 0.15
    assert tuned.rating_strength == 1.0
    assert tuned.weather_strength == 0.3      # 天气能开


def test_prematch_settings_out_of_range_fall_back():
    """越界值回退默认，而不是抛出或穿帮。"""
    s = load_settings({
        **BASE,
        "PREMATCH_STRENGTH": "5",
        "RATING_STRENGTH": "abc",
        "WEATHER_STRENGTH": "-1",
    })
    assert s.prematch_strength == 0.5
    assert s.rating_strength == 0.5
    assert s.weather_strength == 0.0
