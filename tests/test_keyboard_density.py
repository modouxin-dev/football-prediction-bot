"""赛程页 / 预测卡的按钮密度约束。

起因（用户实测反馈，非推测）：赛程页按钮行数超过正文本身，手机上
一屏放不下；且存在独占一行的按钮，被拉成又宽又空的长条。

这些约束一旦被打破，页面就会重新变得不可用，因此用测试锁死：
- 每页场次数受 FX_PER_PAGE 控制，按钮行数必须明显少于正文行数
- 键盘里不允许出现只有 1 个按钮的独占行（会被拉满整行宽度）
"""
from __future__ import annotations

import pytest

from keyboards import Keyboards
from templates import TABS
from views.fixtures import FixturesView

# 与线上 main.FX_PER_PAGE 保持一致；此处独立声明只为说明期望量级。
MAX_ROWS_PER_FIXTURE = 1.0  # 每场比赛最多占 1 行按钮


def _mk_fixtures(n: int):
    import pytz
    from datetime import datetime, timedelta

    tz = pytz.timezone("Asia/Shanghai")
    now = datetime.now(tz)
    out = []
    for i in range(n):
        out.append({
            "fixture": {"id": 1000 + i, "date": (now + timedelta(hours=2 + i)).isoformat()},
            "league": {"id": 39 + (i % 4), "name": "L"},
            "teams": {"home": {"name": f"主队{i}"}, "away": {"name": f"客队{i}"}},
            "goals": {"home": None, "away": None},
            "status": {"short": "NS"},
        })
    return out, tz


def _no_single_row(markup, label: str) -> None:
    """不允许独占一行的按钮：Telegram 会把它拉满整行，又宽又空。"""
    for row in markup.inline_keyboard:
        assert len(row) != 1, (
            f"{label} 出现独占一行的按钮「{row[0].text}」，"
            f"会被拉满整行宽度"
        )


class TestFixtureListDensity:
    def test_button_rows_fewer_than_text_rows(self):
        """按钮行数必须少于正文行数，否则按钮区比内容还长。"""
        items, tz = _mk_fixtures(10)
        text, kb, _, _ = FixturesView.format_fixtures_page(
            items, tz, page=0, per_page=10, day_label="今天",
            active_day=None, all_items=items,
        )
        text_rows = len(text.splitlines())
        btn_rows = len(kb.inline_keyboard)
        assert btn_rows < text_rows, (
            f"按钮 {btn_rows} 行 ≥ 正文 {text_rows} 行，一屏放不下"
        )

    def test_no_single_button_row(self):
        items, tz = _mk_fixtures(10)
        _, kb, _, _ = FixturesView.format_fixtures_page(
            items, tz, page=0, per_page=10, day_label="今天",
            active_day=None, all_items=items,
        )
        _no_single_row(kb, "赛程页")


class TestPredictionCardDensity:
    def test_no_single_button_row(self):
        _no_single_row(Keyboards.prediction_keyboard(1), "预测卡")

    def test_row_count_within_limit(self):
        assert len(Keyboards.prediction_keyboard(1).inline_keyboard) <= 2

    def test_tab_keyboard_no_single_row(self):
        _no_single_row(Keyboards.get_main_keyboard(1, TABS[0][0]), "比赛 tab")
