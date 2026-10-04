# -*- coding: utf-8 -*-
"""队名写法变体的回归测试 / Tests for team-name spelling variants.

数据源对同一支队的写法并不唯一，且省略写法（丢掉省份/词缀）无法用既有
归一化规则推导——只能逐条收录。本文件守护这些"手工补录"的条目：删掉
任意一条，对应的测试必须变红。

Data sources spell one club in many ways, and abbreviated forms (dropped
province/suffix) cannot be derived by the normalisation rules — they must be
recorded explicitly. This file guards those hand-added entries.
"""
from __future__ import annotations

import pytest

from formatkit import _team_key, team_short_name
from templates import TEAM_NAMES

try:  # 冲突表在部分精简环境下可能未导出
    from formatkit import TEAM_COLLISIONS
except ImportError:  # pragma: no cover - 防御性
    TEAM_COLLISIONS = {}


# 埋点（/teammiss）实测抓到的未翻译原名 / Raw names captured by the
# unmatched-name probe in production. 这些是真实数据，不是推测。
PROBE_CAPTURED = [
    ("Central Cordoba de Santiago", "中央科尔多瓦"),
]


@pytest.mark.parametrize("raw, expected", PROBE_CAPTURED)
def test_probe_captured_names_now_translated(raw, expected):
    """埋点抓到的名字必须已能翻译，否则用户会一直看到英文原名。"""
    assert team_short_name(raw) == expected


def test_central_cordoba_variants_share_one_chinese_name():
    """同一支队的所有写法都归一到同一个中文名，不能出现两套译名。"""
    variants = [
        "Central Cordoba",
        "Central Córdoba",
        "Central Cordoba SdE",
        "Central Cordoba de Santiago",
        "Central Córdoba de Santiago",
        "CA Central Córdoba de Santiago del Estero",
    ]
    got = {team_short_name(v) for v in variants}
    assert got == {"中央科尔多瓦"}, f"写法变体出现不一致译名: {got}"


def test_omitted_province_variant_has_distinct_key():
    """省略 del Estero 后归一键不同——这正是当初落空的原因，锁住以防退化。"""
    assert _team_key("Central Cordoba de Santiago") == "central cordoba santiago"
    assert _team_key("Central Cordoba") == "central cordoba"
    # 两个键必须分别存在于 TEAM_NAMES，否则其中一个写法会退回英文
    assert "Central Cordoba de Santiago" in TEAM_NAMES
    assert "Central Cordoba" in TEAM_NAMES


def test_no_collision_introduced():
    """新增变体不得制造归一键冲突（同键多译名会被索引丢弃）。"""
    for raw, _ in PROBE_CAPTURED:
        key = _team_key(raw)
        assert key not in TEAM_COLLISIONS, f"{raw} 的归一键 {key} 出现冲突"


def test_similar_clubs_still_distinguished():
    """易混队名仍要区分：补变体不能把别的队也吸进来。"""
    assert team_short_name("Estudiantes de La Plata") == "拉普拉塔大学生"
    assert team_short_name("Estudiantes de Río Cuarto") == "里奥夸尔托"
    assert team_short_name("Instituto AC Córdoba") == "科尔多瓦学院"


def test_unknown_name_still_falls_back_to_english():
    """未收录仍返回原名——宁可显示英文也不臆造中文名。"""
    raw = "Zzz Nonexistent Club FC"
    assert team_short_name(raw) == raw
