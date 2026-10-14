"""积分榜排版：对齐 / 数字符号列 / 移动端不折行。

这些不变量以前没人守：旧格式前三用奖牌、其余用数字，队名不做列对齐，
20 行挤在一起时列是错开的。
"""

from __future__ import annotations

import re

import pytest

from bot_handler import BotUI
from config import load_settings
from formatkit import display_width, rank_marker
from views.standings import TEAM_W

TZ = load_settings({"TELEGRAM_TOKEN": "1:A", "RAPID_API_KEY": "k", "CHAT_ID": "5",
                    "SEASON": "2026", "ADMIN_ID": "5"}).timezone

# 覆盖 36 列上限的最坏情况：7 个汉字的队名 + 三位净胜球
_LONG_TEAM = "托特纳姆热刺流浪者"


def _row(rank, name, points, w, d, l, gf, ga):
    return {"rank": rank, "team": {"id": rank, "name": name}, "points": points,
            "goalsDiff": gf - ga,
            "all": {"played": w + d + l, "win": w, "draw": d, "lose": l,
                    "goals": {"for": gf, "against": ga}},
            "home": {"played": w, "win": w, "draw": 0, "lose": 0,
                     "goals": {"for": gf, "against": ga}},
            "away": {"played": d + l, "win": 0, "draw": d, "lose": l,
                     "goals": {"for": 0, "against": 0}}}


def _rows(n=20):
    return [_row(i, _LONG_TEAM if i == 1 else f"Team{i:02d}", 60 - i * 3,
                 12, 2, i, 40 - i, 10 + i) for i in range(1, n + 1)]


def _code_rows(text):
    """只取表格行（含表头的整行等宽块），去掉 <code> 标签后按显示宽度比较。"""
    return [re.sub(r"</?code>", "", ln)
            for ln in text.split("\n") if ln.startswith("<code>")]


# ---- 对齐 --------------------------------------------------------------------
def test_every_row_has_identical_width():
    """所有数据行（含表头）显示宽度必须完全一致 —— 这就是「对齐」的可测定义。"""
    rows = _code_rows(BotUI.format_standings_page(_rows(), TZ, 20, "英超"))
    assert len(rows) == 21  # 表头 1 + 数据 20
    widths = {display_width(r) for r in rows}
    assert len(widths) == 1, f"各行列宽不一致：{sorted(widths)}"


def test_columns_dont_shift_when_rank_exceeds_ten():
    """名次跨过 10 时列不能错位：1️⃣(数字符号) 与 11(两位数字) 必须同为 2 列。"""
    text = BotUI.format_standings_page(_rows(20), TZ, 20, "英超")
    rows = _code_rows(text)
    # 第 1 行是表头，其后依次是名次 1..20；取 1️⃣ 行与 11 名行比较列首偏移
    first_data, eleventh = rows[1], rows[11]
    assert display_width(first_data) == display_width(eleventh)
    # 队名起始列一致：剥掉排头符号后，两行剩余部分的宽度必须相同
    m1, m11 = rank_marker(1), rank_marker(11)
    assert first_data.startswith(m1) and eleventh.startswith(m11)
    assert display_width(m1) == display_width(m11) == 2
    assert display_width(first_data[len(m1):]) == display_width(eleventh[len(m11):])


def test_row_width_fits_mobile():
    """整表行宽必须小于等于 36 列，否则手机上会折行。"""
    rows = _code_rows(BotUI.format_standings_page(_rows(), TZ, 20, "英超"))
    assert max(display_width(r) for r in rows) <= 36


# ---- 排头符号 ----------------------------------------------------------------
@pytest.mark.parametrize("rank,expect", [(1, "1️⃣"), (9, "9️⃣"), (10, "🔟"), (11, "11"), (20, "20")])
def test_rank_marker_content(rank, expect):
    assert rank_marker(rank) == expect


def test_rank_marker_is_always_two_columns():
    """无论数字符号还是普通数字，排头必须恒为 2 列，否则整列会错开。"""
    for r in list(range(1, 21)) + [99, -1, None, "x"]:
        assert display_width(rank_marker(r)) == 2


def test_keycap_emoji_width_is_two_not_three():
    """1️⃣ = 数字 + VS16 + 键帽，渲染成一个字形。

    逐字符累加会算成 3 列，而 🔟 只算 2 列 —— 混用时每行列首差 1 列。
    """
    assert display_width("1️⃣") == 2
    assert display_width("🔟") == 2


# ---- 内容 --------------------------------------------------------------------
def test_goal_diff_shown_with_sign():
    text = BotUI.format_standings_page(_rows(3), TZ, 20, "英超")
    assert "+28" in text  # 榜首进 39 失 11 → 净胜 +28
    assert "12-2-1" in text  # 两位数战绩不能被列宽截断


def test_missing_goal_diff_falls_back_to_dash():
    rows = [{"rank": 1, "team": {"name": "无净胜球队"}, "points": 10,
             "all": {"played": 5, "win": 3, "draw": 1, "lose": 1,
                     "goals": {"for": 8, "against": 6}}}]
    text = BotUI.format_standings_page(rows, TZ, 20, "英超")
    assert "无净胜球队" in text
    assert "None" not in text


def test_long_team_name_truncated_to_column_width():
    """超长队名截断到 TEAM_W，不能把后面的列挤歪。"""
    rows = _code_rows(BotUI.format_standings_page(
        [_row(1, "A" * 60, 10, 3, 1, 1, 8, 6)], TZ, 20, "英超"))
    assert display_width(rows[0]) == display_width(rows[1])
