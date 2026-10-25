"""队名中文化 / Team-name Chinese coverage.

覆盖两条规则：
  1. 已启用联赛的球队必须有中文名（实时板块曾整页英文）
  2. 未命中时不得编造，但必须留下可补录的痕迹（/teammiss）
"""
from __future__ import annotations

import pytest

from formatkit import team_short_name
from templates import TEAM_NAMES


# 实时板块实测出现过的联赛（克罗地亚甲 210 / 丹麦超 119）
# 名单取自 hnl.hr 积分榜与 Wikipedia Superliga 2025-26 Current teams
CROATIA = [
    "NK Osijek", "HNK Rijeka", "HNK Hajduk Split", "NK Dinamo Zagreb",
    "NK Lokomotiva Zagreb", "NK Slaven Belupo", "NK Istra 1961",
    "NK Varazdin", "HNK Gorica", "NK Rudes", "NK Vukovar 1991",
]

DENMARK = [
    "Aarhus GF", "Brondby IF", "FC Copenhagen", "FC Fredericia",
    "FC Midtjylland", "FC Nordsjaelland", "Odense Boldklub", "Randers FC",
    "Silkeborg IF", "SonderjyskE", "Viborg FF", "Vejle Boldklub",
    "Lyngby Boldklub", "AC Horsens",
]


@pytest.mark.parametrize("name", CROATIA + DENMARK)
def test_enabled_league_teams_have_chinese_name(name):
    """已启用联赛的球队必须有中文名。

    实时板块此前整页英文：这两个联赛此前在字典里一条都没有。
    """
    cn = team_short_name(name)
    assert cn != name, f"{name} 未命中中文名"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in cn), f"{name} → {cn} 不是中文"


def test_lineups_suffix_variants_resolve_to_same_team():
    """同一支队的多种写法必须落到同一个中文名。

    数据源下发的写法不统一（"Coritiba" / "Coritiba FBC"），
    若两种写法显示成不同名字，看板上同一支队会忽中忽英。
    """
    pairs = [
        ("Coritiba", "Coritiba FBC"),
        ("Benfica", "SL Benfica"),
        ("Flamengo", "CR Flamengo"),
        ("Viborg", "Viborg FF"),
        ("Aarhus", "Aarhus GF"),
    ]
    for a, b in pairs:
        assert team_short_name(a) == team_short_name(b), f"{a} 与 {b} 中文名不一致"


def test_unmatched_names_never_fabricated():
    """未收录的队名原样返回英文，绝不编造中文。"""
    unknown = "Zzz Not A Real Club FC"
    assert team_short_name(unknown) == unknown


def test_teammiss_export_is_available():
    """未命中名单必须能导出，否则缺字只能靠人肉猜。"""
    import formatkit

    assert callable(getattr(formatkit, "unmatched_team_names", None))
