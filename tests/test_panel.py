"""底部面板的收放行为（不联网）。

背景：面板占屏近一半高度，与正文抢空间。收放按钮此前虽然定义了文案常量，
却没有接进消息处理链路，点击后会落到菜单兜底分支被误报成「功能正在开发中」。
本组测试锁住三件事：按钮能识别、点击不报「开发中」、状态能被记住。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import KeyboardButton

import main
from bot_handler import BotUI
from commands.basic import panel_cmd, toggle_panel_message
from config import load_settings
from support import panel_expanded, panel_state, set_panel_state
from templates import (PANEL_CLOSE_LABEL, PANEL_COLLAPSED, PANEL_EXPANDED,
                       PANEL_HINTS, PANEL_OPEN_LABEL)

ENV = {
    "TELEGRAM_TOKEN": "123456:TEST-TOKEN",
    "RAPID_API_KEY": "k",
    "CHAT_ID": "555",
    "SEASON": "2026",
    "ADMIN_ID": "555",
}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self


def make_ctx(user_data=None):
    app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={"settings": SETTINGS, "fx_cache": None},
        job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []),
    )
    return SimpleNamespace(application=app, user_data=user_data or {})


def text_update(text):
    msg = FakeMessage(text)
    return (
        SimpleNamespace(
            callback_query=None,
            effective_user=SimpleNamespace(id=555),
            effective_message=msg,
        ),
        msg,
    )


# ---- 键盘形态 -----------------------------------------------------------------

def test_collapsed_keyboard_has_only_open_button():
    """收起态：整块缩成一行，把屏幕还给正文。"""
    markup = BotUI.reply_menu_keyboard(expanded=False)
    flat = [b for row in markup.keyboard for b in row]
    assert [b.text for b in flat] == [PANEL_OPEN_LABEL]
    assert len(markup.keyboard) == 1


def test_expanded_keyboard_ends_with_close_row():
    """展开态：收起按钮独占最后一行，不与功能按钮混排。"""
    markup = BotUI.reply_menu_keyboard(expanded=True)
    assert [b.text for b in markup.keyboard[-1]] == [PANEL_CLOSE_LABEL]


def test_default_is_expanded():
    """未显式指定时保持展开，避免老用户升级后找不到功能入口。"""
    assert [b.text for b in BotUI.reply_menu_keyboard().keyboard[-1]] == [PANEL_CLOSE_LABEL]


# ---- 文字识别（必须在菜单解析之前命中） ---------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        (PANEL_CLOSE_LABEL, PANEL_COLLAPSED),
        (PANEL_OPEN_LABEL, PANEL_EXPANDED),
        ("收起面板", PANEL_COLLAPSED),      # 手打、不带 emoji
        ("菜单", PANEL_EXPANDED),            # 手打、不带 emoji
    ],
)
def test_panel_key_from_text(text, expected):
    assert main._panel_key_from_text(text) == expected


def test_panel_labels_do_not_collide_with_menu_items():
    """面板标签若与菜单项重名，会被菜单解析抢先命中而失效。"""
    from bot_handler import MENU_ITEMS

    menu_labels = {label for _, label in MENU_ITEMS}
    assert PANEL_CLOSE_LABEL not in menu_labels
    assert PANEL_OPEN_LABEL not in menu_labels


@pytest.mark.parametrize("text", ["📅 今日赛程", "🏆 联赛排名", "随便说句话"])
def test_non_panel_text_is_not_mistaken(text):
    assert main._panel_key_from_text(text) is None


# ---- 点击行为 -----------------------------------------------------------------

def test_close_button_never_reports_coming_soon():
    """回归：面板按钮曾落到菜单兜底，被渲染成「功能正在开发中」。"""
    ctx = make_ctx()
    update, msg = text_update(PANEL_CLOSE_LABEL)
    run(main.on_menu_text(update, ctx))
    assert msg.replies
    sent = msg.replies[-1][0]
    assert "正在开发中" not in sent
    assert "收起" in sent


def test_toggle_remembers_state_per_user():
    ctx = make_ctx()
    update, msg = text_update(PANEL_OPEN_LABEL)
    run(main.on_menu_text(update, ctx))
    # 收起后状态应被记住，后续消息沿用
    assert panel_state(ctx) == PANEL_EXPANDED
    assert panel_expanded(ctx) is True

    update2, _ = text_update(PANEL_CLOSE_LABEL)
    run(main.on_menu_text(update2, ctx))
    assert panel_state(ctx) == PANEL_COLLAPSED
    assert panel_expanded(ctx) is False


def test_toggle_sends_matching_keyboard():
    """Reply 键盘只有随消息下发才更新，所以每次切换必须带回新键盘。"""
    ctx = make_ctx()
    update, msg = text_update(PANEL_CLOSE_LABEL)
    run(main.on_menu_text(update, ctx))
    markup = msg.replies[-1][1]["reply_markup"]
    assert [b.text for row in markup.keyboard for b in row] == [PANEL_OPEN_LABEL]

    update2, msg2 = text_update(PANEL_OPEN_LABEL)
    run(main.on_menu_text(update2, ctx))
    markup2 = msg2.replies[-1][1]["reply_markup"]
    assert [b.text for b in markup2.keyboard[-1]] == [PANEL_CLOSE_LABEL]


def test_hints_explain_commands_still_work():
    """收起提示要说明命令不受影响，否则用户会以为功能丢了。"""
    assert PANEL_COLLAPSED in PANEL_HINTS and PANEL_EXPANDED in PANEL_HINTS
    assert "命令" in PANEL_HINTS[PANEL_COLLAPSED]


# ---- /panel 命令 --------------------------------------------------------------

def test_panel_cmd_without_arg_flips():
    ctx = make_ctx()
    update, msg = text_update("/panel")
    run(panel_cmd(update, ctx))
    # 默认展开 → 不带参数翻转为收起
    assert panel_state(ctx) == PANEL_COLLAPSED

    update2, _ = text_update("/panel")
    run(panel_cmd(update2, ctx))
    assert panel_state(ctx) == PANEL_EXPANDED


@pytest.mark.parametrize(
    "arg,expected",
    [("open", PANEL_EXPANDED), ("close", PANEL_COLLAPSED), ("展开", PANEL_EXPANDED), ("收起", PANEL_COLLAPSED)],
)
def test_panel_cmd_explicit_arg(arg, expected):
    ctx = make_ctx()
    update, _ = text_update(f"/panel {arg}")
    run(panel_cmd(update, ctx))
    assert panel_state(ctx) == expected


def test_set_panel_state_tolerates_missing_user_data():
    """context 没有 user_data 时（如某些回调）不能抛异常。"""
    set_panel_state(SimpleNamespace(user_data=None), PANEL_COLLAPSED)
    assert panel_expanded(SimpleNamespace(user_data=None)) is True  # 回退到默认展开
