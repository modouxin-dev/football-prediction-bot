"""面向所有人的基础指令：/start /help /menu /web"""
from __future__ import annotations

import logging

from bot_handler import BotUI
from telegram.constants import ParseMode

from support import panel_expanded, panel_state, reply_html, set_panel_state
from tghtml import normalize
from templates import (PANEL_COLLAPSED, PANEL_EXPANDED, PANEL_HINTS)

from . import CommandDispatcher

log = logging.getLogger("bot")
ui = BotUI()

try:  # 图表依赖缺失时机器人仍要能正常跑，只是不出图
    import chart
except ImportError:  # pragma: no cover
    chart = None


async def start(update, context) -> None:
    """/start — 欢迎信息与推送时间。"""
    settings = context.application.bot_data["settings"]
    text = ui.format_welcome(settings)
    # 跟随该用户上次选择的面板形态，不每次都强制展开
    keyboard = ui.reply_menu_keyboard(panel_expanded(context))
    banner = chart.brand_banner() if chart else None
    if banner:
        # 品牌头图 + 文案说明（图片失败时降级为纯文字，不影响使用）
        await update.effective_message.reply_photo(
            photo=banner,
            caption=normalize(text),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    else:
        await update.effective_message.reply_text(
            normalize(text), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=keyboard,
        )


async def help_cmd(update, context) -> None:
    """/help — 命令说明。"""
    await reply_html(update.effective_message, ui.format_help(), ui.menu_keyboard())


async def menu_cmd(update, context) -> None:
    """/menu — 功能菜单（Inline 按钮，点击后原地切换）。"""
    settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        normalize(ui.format_menu(settings)), parse_mode=ParseMode.HTML,
        reply_markup=ui.menu_keyboard(),
    )


async def toggle_panel_message(message, context, target: str) -> None:
    """收放底部面板并回一条提示。

    Reply 键盘只有随消息下发才会更新，所以必须补发一条消息把新键盘带下去；
    提示文案同时说明「收起不影响命令」，避免用户误以为功能丢失。
    按钮点击（main.py）与 /panel 命令共用这一份实现，行为一致。
    """
    set_panel_state(context, target)
    await reply_html(
        message,
        PANEL_HINTS[target],
        ui.reply_menu_keyboard(target == PANEL_EXPANDED),
    )


async def panel_cmd(update, context) -> None:
    """/panel — 展开或收起底部功能面板；不带参数则翻转当前状态。"""
    args = (update.effective_message.text or "").split()
    arg = args[1].strip().lower() if len(args) > 1 else ""
    if arg in ("open", "expand", "on", "展开"):
        target = PANEL_EXPANDED
    elif arg in ("close", "collapse", "off", "收起"):
        target = PANEL_COLLAPSED
    else:
        # 不带参数：翻转当前状态
        target = (
            PANEL_COLLAPSED
            if panel_state(context) == PANEL_EXPANDED
            else PANEL_EXPANDED
        )
    await toggle_panel_message(update.effective_message, context, target)


async def web_cmd(update, context) -> None:
    """/web — 网页端入口。"""
    runtime = context.application.bot_data["cmd_runtime"]
    await runtime.dispatch_menu(update, context, "web")


def register(dispatcher: CommandDispatcher) -> None:
    dispatcher.register("start", start, description="欢迎信息与推送时间")
    dispatcher.register("help", help_cmd, description="命令说明")
    dispatcher.register("menu", menu_cmd, description="功能菜单")
    dispatcher.register("web", web_cmd, description="网页端入口")
    dispatcher.register("panel", panel_cmd, description="展开/收起底部面板")
