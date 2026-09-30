"""面向所有人的基础指令：/start /help /menu /web"""
from __future__ import annotations

import logging

from bot_handler import BotUI
from telegram.constants import ParseMode

from support import reply_html
from tghtml import normalize

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
    keyboard = ui.reply_menu_keyboard()
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


async def web_cmd(update, context) -> None:
    """/web — 网页端入口。"""
    runtime = context.application.bot_data["cmd_runtime"]
    await runtime.dispatch_menu(update, context, "web")


def register(dispatcher: CommandDispatcher) -> None:
    dispatcher.register("start", start, description="欢迎信息与推送时间")
    dispatcher.register("help", help_cmd, description="命令说明")
    dispatcher.register("menu", menu_cmd, description="功能菜单")
    dispatcher.register("web", web_cmd, description="网页端入口")
