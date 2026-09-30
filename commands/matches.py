"""赛程与预测相关指令。

这些指令本质都是「换一种模式打开赛程视图」，因此统一委托给菜单分发器，
只负责设置好 user_data 里的模式与分页状态。
"""
from __future__ import annotations

import logging
from datetime import datetime

from bot_handler import esc
from tghtml import normalize

from . import CommandDispatcher

log = logging.getLogger("bot")


def _runtime(context):
    return context.application.bot_data["cmd_runtime"]


async def fixtures_cmd(update, context) -> None:
    """/fixtures — 今日赛程，按联赛分组、支持翻页。"""
    await _runtime(context).dispatch_menu(update, context, "fixtures")


async def predict_cmd(update, context) -> None:
    """/predict — 比赛预测：胜平负概率、比分、信心等级。"""
    await _runtime(context).dispatch_menu(update, context, "predict")


async def standings_cmd(update, context) -> None:
    """/standings — 实时积分榜与攻防数据。"""
    await _runtime(context).dispatch_menu(update, context, "standings")


async def refresh_cmd(update, context) -> None:
    """/refresh — 清空缓存重新拉取。"""
    await _runtime(context).dispatch_menu(update, context, "refresh")


async def analysis_cmd(update, context) -> None:
    """/analysis — 深度分析：近期状态、主客场、历史交锋。"""
    await _runtime(context).dispatch_menu(update, context, "analysis")


async def next_cmd(update, context) -> None:
    """/next — 下一场比赛。"""
    runtime = _runtime(context)
    context.user_data["fx_mode"] = runtime.modes["next"]
    context.user_data["fx_page"] = 0
    context.application.bot_data["fx_cache"] = None
    await runtime.dispatch_menu(update, context, "fixtures")


async def date_cmd(update, context) -> None:
    """/date YYYY-MM-DD — 查询指定日期的赛程。"""
    args = context.args or []
    usage = "📆 请带上日期，例如：<code>/date 2026-10-10</code>"
    if not args:
        await update.effective_message.reply_text(normalize(usage), parse_mode="HTML")
        return
    try:
        day = datetime.strptime(args[0].strip(), "%Y-%m-%d").date()
    except ValueError:
        await update.effective_message.reply_text(
            normalize(
                f"❌ 日期格式不对（{esc(args[0])}），请用 YYYY-MM-DD，例如 /date 2026-10-10"
            ),
            parse_mode="HTML",
        )
        return
    context.user_data["fx_mode"] = _runtime(context).modes["date"]
    context.user_data["fx_date"] = day
    context.user_data["fx_page"] = 0
    context.application.bot_data["fx_cache"] = None
    await _runtime(context).show_fixtures(_FakeUpdate(update), context, page=0)


from .adapters import FakeUpdate as _FakeUpdate  # noqa: E402  延迟导入避免循环


def register(dispatcher: CommandDispatcher) -> None:
    dispatcher.register("fixtures", fixtures_cmd, description="今日赛程")
    dispatcher.register("predict", predict_cmd, description="比赛预测")
    dispatcher.register("standings", standings_cmd, description="联赛积分榜")
    dispatcher.register("refresh", refresh_cmd, description="清空缓存重新拉取")
    dispatcher.register("analysis", analysis_cmd, description="深度分析")
    dispatcher.register("next", next_cmd, description="下一场比赛")
    dispatcher.register("date", date_cmd, description="指定日期的赛程")
