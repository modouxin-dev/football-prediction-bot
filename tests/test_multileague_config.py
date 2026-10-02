"""多联赛配置解析测试。

覆盖：默认单联赛、多联赛、去重保序、主联赛自动补首位、非法值报错。
"""

import pytest

from config import ConfigError, load_settings

BASE = {
    "TELEGRAM_TOKEN": "t",
    "API_FOOTBALL_KEY": "k",
    "CHAT_ID": "-100",
}


def _load(**extra):
    env = dict(BASE)
    env.update(extra)
    return load_settings(env)


def test_default_single_league():
    """不配 LEAGUE_IDS 时退化为单联赛，行为与旧版一致。"""
    s = _load()
    assert s.league_id == 39
    assert s.league_ids == (39,)


def test_multiple_leagues():
    s = _load(LEAGUE_IDS="39,140,78")
    assert s.league_ids == (39, 140, 78)


def test_duplicates_removed():
    """重复编号去重，且保持首次出现顺序。"""
    s = _load(LEAGUE_IDS="39,140,39,78")
    assert s.league_ids == (39, 140, 78)


def test_primary_always_first():
    """用户列表不含主联赛时，主联赛自动插到最前，保证 league_ids[0] == league_id。"""
    s = _load(LEAGUE_ID="39", LEAGUE_IDS="140,78")
    assert s.league_ids[0] == s.league_id == 39
    assert s.league_ids == (39, 140, 78)


def test_empty_value_falls_back():
    s = _load(LEAGUE_IDS="   ")
    assert s.league_ids == (39,)


def test_invalid_value_raises():
    with pytest.raises(ConfigError):
        _load(LEAGUE_IDS="39,abc")


def test_zero_value_raises():
    with pytest.raises(ConfigError):
        _load(LEAGUE_IDS="39,0")
