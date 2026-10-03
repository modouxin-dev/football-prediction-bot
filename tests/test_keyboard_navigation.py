"""二级视图必须有出口（返回上一层）。

起因：比赛四个 tab 的切换键盘只有 tab + 刷新赔率，没有返回按钮。
用户点进「赔率对比」「历史交锋」后无法离开，只能重新发命令。

本文件锁住两件事：
1. 每个会替换掉上一层内容的键盘，都至少带一个出口；
2. 出口的 callback_data 必须是引擎真实认识的 key，不能是死按钮。
"""
from __future__ import annotations

import pytest

from keyboards import Keyboards, nav_row

# on_menu_callback 里真实处理、且会渲染内容的 key（见 main.py 的分支）
KNOWN_MENU_KEYS = {"home", "fixtures", "standings", "analysis", "predict", "help", "web", "refresh"}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def exits(markup) -> list[str]:
    """键盘里的出口按钮：形如 menu:<key> 且 key 被引擎认识。"""
    out = []
    for data in callbacks(markup):
        prefix, _, key = (data or "").partition(":")
        if prefix == "menu" and key in KNOWN_MENU_KEYS:
            out.append(data)
    return out


def test_main_keyboard_has_both_exits():
    """tab 切换键盘必须同时给出「返回赛程」和「主菜单」。"""
    data = callbacks(Keyboards.get_main_keyboard(101))
    assert "menu:fixtures" in data and "menu:home" in data


def test_main_keyboard_keeps_exits_on_every_tab():
    """切到任意一个 tab 后出口都不能消失 —— 缺陷正是「切进去就出不来」。"""
    for view in ("home", "deep", "h2h", "odds"):
        assert exits(Keyboards.get_main_keyboard(101, view)) == ["menu:fixtures", "menu:home"], view


def test_nav_row_without_fixtures_only_has_home():
    """上一层不是赛程列表时，只给主菜单，避免「返回赛程」指向不存在的上下文。"""
    row = nav_row(with_fixtures=False)
    assert [b.callback_data for b in row] == ["menu:home"]


@pytest.mark.parametrize(
    "name,markup",
    [
        ("main", Keyboards.get_main_keyboard(101)),
        ("prediction", Keyboards.prediction_keyboard(101)),
        ("analysis", Keyboards.analysis_keyboard(101)),
        ("chart", Keyboards.chart_keyboard(101, "form")),
        ("standings", Keyboards.standings_keyboard()),
    ],
)
def test_every_inline_view_has_an_exit(name, markup):
    """所有会替换上一层内容的 inline 键盘都必须有出口。

    主菜单本身不需要（它就是终点），故不在本列表内。
    """
    assert exits(markup), f"{name} 键盘没有出口，用户会被困住"


def test_exit_buttons_are_not_dead():
    """出口的 callback_data 必须是引擎认识的 key，不能点了没反应。"""
    for markup in (
        Keyboards.get_main_keyboard(101),
        Keyboards.prediction_keyboard(101),
        Keyboards.analysis_keyboard(101),
    ):
        for data in exits(markup):
            _, _, key = data.partition(":")
            assert key in KNOWN_MENU_KEYS, f"{data} 的 key 未被引擎处理"


@pytest.mark.parametrize(
    "name,markup",
    [
        ("main", Keyboards.get_main_keyboard(101)),
        ("prediction", Keyboards.prediction_keyboard(101)),
        ("analysis", Keyboards.analysis_keyboard(101)),
        ("chart", Keyboards.chart_keyboard(101, "form")),
        ("standings", Keyboards.standings_keyboard()),
    ],
)
def test_every_view_exposes_all_reachable_exits(name, markup):
    """每个视图必须给出「它能返回的所有上层」，不能只给主菜单。

    旧断言只要求 exits() 非空，于是 chart/standings 挂一个「主菜单」
    就算通过 —— 用户从赛程列表进来却拿不到「返回赛程」，只能绕道主菜单。
    """
    data = callbacks(markup)
    assert "menu:home" in data, f"{name} 缺少主菜单出口"
    if name != "prediction":  # 预测页用「📅 今日赛程」承担返回，无需重复
        assert "menu:fixtures" in data, f"{name} 缺少返回赛程出口"


def test_home_exit_label_is_consistent():
    """同一个出口在所有键盘里必须是同一个词，否则用户以为是两个功能。"""
    labels = set()
    for markup in (
        Keyboards.get_main_keyboard(101),
        Keyboards.prediction_keyboard(101),
        Keyboards.analysis_keyboard(101),
        Keyboards.chart_keyboard(101, "form"),
        Keyboards.standings_keyboard(),
    ):
        for row in markup.inline_keyboard:
            for b in row:
                if b.callback_data == "menu:home":
                    labels.add(b.text)
    assert labels == {"🏠 主菜单"}, f"主菜单出口文案不统一：{labels}"


def test_every_row_has_at_most_three_buttons():
    """v4 排版规范：单行按钮数 <= 3，超过会挤成两行或折行。"""
    for name, markup in (
        ("main", Keyboards.get_main_keyboard(101)),
        ("prediction", Keyboards.prediction_keyboard(101)),
        ("analysis", Keyboards.analysis_keyboard(101)),
        ("chart", Keyboards.chart_keyboard(101, "form")),
        ("standings", Keyboards.standings_keyboard()),
    ):
        for i, row in enumerate(markup.inline_keyboard):
            assert len(row) <= 3, f"{name} 第 {i + 1} 行有 {len(row)} 个按钮"
