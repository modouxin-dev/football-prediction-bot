"""无依赖的共享支撑层 / Shared support helpers.

放在这里的原因：`commands/` 与 `main.py`、`scheduler.py` 都要用到这些工具，
若留在 main.py 会形成 `commands → main` 的反向依赖（进而循环导入）；
若各写一份则会重复。本模块只依赖 telegram 与 bot_handler，不依赖 main/service，
因此谁都可以安全导入。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError

from api_client import APIError
from bot_handler import split_html_blocks
from config import Settings

log = logging.getLogger("bot")


def is_admin(update: Update, settings: Settings) -> bool:
    """是否为管理员。"""
    user = update.effective_user
    return user is not None and user.id in settings.admin_ids


def describe_error(exc: BaseException) -> str:
    """把异常翻译成给用户看的一句话。"""
    if isinstance(exc, APIError):
        return f"数据源错误：{exc}"
    if isinstance(exc, TelegramError):
        return f"Telegram 错误：{exc}"
    return f"{type(exc).__name__}: {exc}"[:300]


async def deny(update: Update) -> None:
    """无权限提示（顺带告知用户自己的 ID，方便加入 ADMIN_ID）。"""
    uid = update.effective_user.id if update.effective_user else "未知"
    await update.effective_message.reply_text(
        f"🚫 仅管理员可用。你的 Telegram 用户 ID 是 {uid}，如需授权请把它加入 ADMIN_ID 环境变量。"
    )


async def notify_admins(app, text: str) -> None:
    """把异常通知给全部管理员；单个失败不影响其他人。"""
    for admin_id in app.bot_data["settings"].admin_ids:
        try:
            await app.bot.send_message(chat_id=admin_id, text=text)
        except TelegramError as exc:
            log.warning("通知管理员 %s 失败：%s", admin_id, exc)


# ---- 防重复点击（同一用户同一任务并发只放行一次） -----------------------------------
async def begin_task(bot_data: dict, user_id: int, task: str) -> bool:
    pending = bot_data.setdefault("pending", set())
    key = (user_id, task)
    if key in pending:
        return False
    pending.add(key)
    return True


def end_task(bot_data: dict, user_id: int, task: str) -> None:
    bot_data.setdefault("pending", set()).discard((user_id, task))


def back_to_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 重试", callback_data="menu:fixtures"),
                InlineKeyboardButton("↩️ 返回主菜单", callback_data="menu:home"),
            ]
        ]
    )


async def reply_html(message, text: str, markup) -> None:
    """发送 HTML 消息：统一加无链接预览，超长时拆分（后续块不带键盘）。"""
    blocks = split_html_blocks(text)
    for idx, block in enumerate(blocks):
        await message.reply_text(
            block,
            parse_mode=ParseMode.HTML,
            reply_markup=markup if idx == len(blocks) - 1 else None,
            disable_web_page_preview=True,
        )


async def edit_view(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """就地编辑上一条消息。文本过长时按行拆分，只编辑第一块。"""
    from telegram.error import BadRequest

    blocks = split_html_blocks(text)
    if len(blocks) > 1:
        log.warning("消息过长（%d 字符），已拆分为 %d 块，仅展示第一块", len(text), len(blocks))
        text = blocks[0] + "\n\n…（内容过长，已省略部分）"
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        # 点击的正是当前页面时 Telegram 会报这个，忽略即可
        if "not modified" not in str(exc).lower():
            raise
