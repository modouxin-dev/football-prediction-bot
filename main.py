"""足球量化分析 Telegram 机器人入口。

- 每天在 PUSH_TIME（时区 TIMEZONE）向 CHAT_ID 推送即将开赛的比赛预测
- /test：手动触发一次推送（仅管理员）；/status：运行状态与数据源诊断（仅管理员）
- 推送消息下方的按钮（预测 / 深度分析 / 历史交锋 / 赔率对比 / 刷新）在原消息上就地切换
"""
from __future__ import annotations

import logging
import os

import paths
import pytz
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from analyzer import MatchAnalyzer, calculate_prediction_level
from api_client import APIError, FootballAPI
from data_source import DataSourceRouter
from football_data import FootballDataAPI
from bot_handler import MENU_ITEMS, BotUI, esc, league_label
from commands import CommandRuntime, build_dispatcher
# 定时任务已迁到 scheduler.py。以下两个名字仍是既有测试与调用方的入口，
# 保留再导出；其余不再从 main 暴露。
from scheduler import daily_push, run_push, setup_scheduler  # noqa: F401
from keyboards import nav_row
from views.digest import DigestView
from tghtml import normalize
from support import (back_to_menu_markup, begin_task, deny, describe_error,
                     edit_view, end_task, is_admin,
                     panel_state, reply_html, set_panel_state)
from templates import (PANEL_CLOSE_LABEL, PANEL_COLLAPSED, PANEL_EXPANDED,
                       PANEL_HINTS, PANEL_OPEN_LABEL)
from commands.adapters import FakeUpdate
from commands.admin import storage_cmd
from commands.basic import toggle_panel_message
from config import ConfigError, Settings, load_settings
from service import (
    MODE_DATE,
    ST_NO_DATA,
    MODE_NEXT,
    MODE_TODAY,
    Prediction,
    PredictionService,
)

try:  # 图表依赖缺失时机器人仍要能正常跑，只是不出图
    import chart
except ImportError:  # pragma: no cover
    chart = None

log = logging.getLogger("bot")
ui = BotUI()


# 机器人指令表 / Bot command list
# 每项为 (命令, 说明)；说明为中英双语，方便中文用户与英文用户各自识别。
# Each item is (command, description); descriptions are bilingual (CN/EN).
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "欢迎信息与推送时间 / Welcome & push time"),
    ("menu", "打开功能菜单 / Open main menu"),
    ("help", "命令说明 / Command help"),
    ("fixtures", "今日赛程 / Today's fixtures"),
    ("predict", "比赛预测 / Match prediction"),
    ("standings", "联赛排名 / League standings"),
    ("refresh", "刷新数据 / Refresh data"),
    ("next", "下一场比赛 / Next match"),
    ("web", "网页端入口 / Web app"),
    ("analysis", "深度分析 / Deep analysis"),
    ("date", "指定日期赛程 / Fixtures by date"),
    ("stats", "预测命中率（管理员） / Prediction stats (admin)"),
    ("storage", "存储自检与挂载卷验证（管理员） / Storage check (admin)"),
    ("test", "立即推送一次预测（管理员） / Push now (admin)"),
    ("status", "运行状态与数据源诊断（管理员） / Status & diagnostics (admin)"),
]
WIDE_HOURS = 24 * 14  # /test 在近期无比赛（如国际比赛日）时放宽到 14 天，方便看到示例消息

FX_PER_PAGE = 5  # 今日赛程每页比赛数
MENU_BY_LABEL = {label: key for key, label in MENU_ITEMS}  # 底部键盘文字 → 菜单 key


# ---- 日志 -------------------------------------------------------------------
class RedactingFormatter(logging.Formatter):
    """把密钥从日志（含异常堆栈）里抹掉，避免泄露到 Railway 日志。"""

    def __init__(self, fmt: str, secrets: list[str | None]) -> None:
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)  # 输出到 stdout，Railway 才会按 info 级别显示
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            [settings.telegram_token, settings.api_key],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)
    # httpx 在 INFO 级别会打印完整请求 URL，而 Telegram 的 URL 里包含 bot token
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---- 工具 -------------------------------------------------------------------
async def _dispatch_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    """直接命令 → 菜单分发（与按钮共用 on_menu_key）。

    供 commands 包通过 CommandRuntime 注入使用；命令层不反向导入本模块。
    """
    await on_menu_key(FakeUpdate(update), context, key)


async def show_fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    """渲染今日赛程。有 callback_query 时编辑原消息，否则（底部键盘）发新消息。"""
    query = update.callback_query
    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    user_id = update.effective_user.id if update.effective_user else 0

    if not await begin_task(app.bot_data, user_id, "fixtures"):
        if query:
            await query.answer("正在获取，请稍候…")
        return
    try:
        tz = settings.timezone
        label = datetime.now(tz).strftime("%Y-%m-%d")
        mode = context.user_data.get("fx_mode") or MODE_TODAY
        target = context.user_data.get("fx_date")
        cache = app.bot_data.get("fx_cache")
        ck = f"{label}|{mode}|{target}"
        if cache is None or cache.get("date") != ck:
            # 统一查询入口：区分「接口无数据」与「窗口内无比赛」
            res = await service.query_fixtures(mode, target)
            items = res["fixtures"]
            cache = {"date": ck, "items": items, "status": res["status"],
                     "note": res["note"], "season_range": res["season_range"],
                     "error": res.get("error"),
                     "day_label": res["day_label"], "mode": mode}
            app.bot_data["fx_cache"] = cache
        items = cache["items"]
    except APIError as exc:
        text = f"❌ <b>获取今日赛程失败</b>\n{esc(exc)}\n\n{ui.error_hint(exc)}"
        markup = back_to_menu_markup()
    except Exception as exc:  # 任何异常都不能让机器人崩掉
        log.exception("获取今日赛程失败")
        text = f"❌ <b>获取今日赛程失败</b>\n{esc(describe_error(exc))}"
        markup = back_to_menu_markup()
    else:
        multi_day = bool(getattr(service, "using_upcoming", False))
        day_label = cache.get("day_label") or getattr(service, "fixture_day_label", "") or label
        # 日期导航条要能标出「你现在停在哪一天」，否则点完明天就分不清
        # 看到的是哪天的球——尤其是今天没比赛、自动扩窗之后。
        _active = target if mode == MODE_DATE else (cache.get("active_day"))
        # 联赛筛选按用户隔离（user_data），赛程缓存是全局共享（bot_data）：
        # 筛的是同一批数据，不重新请求，切联赛零延迟。
        lid = context.user_data.get("fx_league")
        if lid is not None:
            try:
                lid = int(lid)
            except (TypeError, ValueError):
                lid = None
        text, markup, page, _ = ui.format_fixtures_page(
            items, tz, page, FX_PER_PAGE, day_label, multi_day=multi_day,
            empty_range=cache.get("season_range"),
            empty_source_ok=cache.get("status") != ST_NO_DATA,
            active_day=_active,
            all_items=items, league_filter=lid,
        )
        # 三态渲染：有数据不啰嗦；窗口无比赛给范围+下一步；接口无数据说清真实原因
        note = cache.get("note")
        if not items and note and lid is None:
            span = cache.get("season_range")
            if cache.get("status") == ST_NO_DATA:
                err = cache.get("error")
                reason = esc(describe_error(err)) if err is not None else esc(note)
                hint = ui.error_hint(err) if err is not None else ""
                text = (
                    "❌ <b>获取赛程失败</b>\n"
                    f"{reason}\n\n{hint}"
                )
                markup = back_to_menu_markup()
            else:
                hint = ""
                if span:
                    hint = (f"\n\n📆 该赛季数据范围：<code>{span[0]}</code> ~ <code>{span[1]}</code>"
                            f"\n可点「下一场」查看最近一场比赛。")
                text = f"{text}\n\n{esc(note)}{hint}"
    finally:
        end_task(app.bot_data, user_id, "fixtures")

    context.user_data["fx_page"] = page  # 页码按用户隔离
    if query:
        await edit_view(query, text, markup)
    else:
        await reply_html(update.effective_message, text, markup)


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """菜单按钮回调 / Inline menu callback."""
    query = update.callback_query
    _, _, key = (query.data or "").partition(":")
    await query.answer()
    await on_menu_key(update, context, key)


async def on_menu_key(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    """菜单业务分发：按钮回调与直接命令共用同一条路径（逻辑只写一份）。

    Menu dispatcher shared by button callbacks and direct commands.
    """
    query = update.callback_query
    settings: Settings = context.application.bot_data["settings"]
    if key == "home":
        await edit_view(query, ui.format_menu(settings), ui.menu_keyboard())
    elif key == "digest":
        await generate_digest(update, context)
    elif key == "fixtures":
        await show_fixtures(update, context, page=0)
    elif key == "refresh":
        context.application.bot_data["fx_cache"] = None
        await show_fixtures(update, context, page=0)
    elif key == "help":
        await edit_view(query, ui.format_help(), ui.menu_keyboard())
    elif key == "web":
        await edit_view(query, ui.web_text(settings.web_url), ui.menu_keyboard())
    elif key == "standings":
        await show_standings(update, context)
    elif key == "analysis":
        await show_fixtures(update, context, page=0)  # 深度分析要先选比赛 / pick a match first
    elif key == "predict":
        await show_fixtures(update, context, page=0)  # 比赛预测要先选比赛 / pick a match first
    elif key == "storage":
        await storage_cmd(update, context)
    else:
        await edit_view(query, ui.format_coming(key), ui.menu_keyboard())


def settings_timezone_of(service):
    """取服务配置的时区，供汇总按本地自然日对齐赛程页。"""
    return service.settings.timezone


async def _build_digest_predictions(service) -> tuple[list, str | None]:
    """取今日预测；无比赛时放宽到 WIDE_HOURS，并返回说明文案。

    抽出是因为「按钮回调」和「底部键盘文字」两个入口都要用同一套取数逻辑，
    各写一份必然漂移（一个放宽了另一个没放宽，用户会看到两种不一致的结论）。
    """
    # 用「本地自然日」窗口取数，与赛程页口径一致：
    # 否则赛程显示 10-04 的比赛，汇总却按滚动 36 小时窗口跳到 10-05，
    # 用户会看到两个模块日期对不上、场数也对不上。
    today = datetime.now(settings_timezone_of(service)).date()
    predictions = await service.build_predictions(day=today)
    note = service.last_note
    if not predictions:
        predictions = await service.build_predictions(lookahead_hours=WIDE_HOURS)
        if predictions:
            note = (f"当前时间范围内没有未开赛的比赛，"
                    f"已放宽到 {WIDE_HOURS // 24} 天内。")
        elif service.last_note:
            note = service.last_note
    return predictions, note


async def generate_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """手动生成「今日预测汇总」并就地展示。

    定时推送是每天一次，但赛程会变（补赛、改期），用户也需要随时看当前口径的
    结论，所以给一个随时可点的生成按钮。

    与定时推送的差别只有两点：
    1. 手动触发时无比赛会**放宽到 14 天**——用户点了按钮却只看到「暂无比赛」，
       无法判断是机器人坏了还是真没比赛；放宽后至少能验证链路是通的。
    2. 无比赛也必须给出可见反馈（就地编辑），不能静默。定时推送才静默。
    """
    query = update.callback_query
    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]

    await query.answer("正在生成今日预测…")
    user_id = query.from_user.id
    # 生成要调 API 算几十场，耗时数秒；用 pending 守卫避免重复点击把 API 打爆
    if not await begin_task(app.bot_data, user_id, "digest"):
        return
    try:
        predictions, note = await _build_digest_predictions(service)
    except Exception as exc:  # 手动触发：失败必须让用户看见，不能只写日志
        log.exception("手动生成汇总失败")
        await edit_view(
            query,
            f"❌ <b>生成失败</b>\n{esc(describe_error(exc))}",
            back_to_menu_markup(),
        )
        return
    finally:
        end_task(app.bot_data, user_id, "digest")

    if not predictions:
        # 兜底文案用汇总视图的空态，保证与推送口径一致
        await edit_view(
            query,
            DigestView.format_digest_empty(note),
            back_to_menu_markup(),
        )
        return

    pages = DigestView.format_daily_digest_pages(
        predictions, settings, settings.timezone, note=note,
    )
    await edit_view(query, pages[0], InlineKeyboardMarkup([nav_row()]))
    # 分页的其余几页新发：edit_message_text 只能改当前这一条
    for extra in pages[1:]:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=normalize(extra),
            parse_mode=ParseMode.HTML,
        )


async def on_fixtures_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """切换赛程查询模式：今日 / 未来 7 天 / 下一场。

    Switch fixture query mode: today / upcoming / next.
    """
    query = update.callback_query
    _, _, raw = (query.data or "").partition(":")
    await query.answer()
    if raw == "date":
        # 指定日期需要参数，按钮无法携带，引导用命令输入
        await query.edit_message_text(
            "📆 查询指定日期，请发送命令并带上日期：\n"
            "<code>/date 2026-10-10</code>",
            parse_mode="HTML",
        )
        return
    context.user_data["fx_mode"] = raw
    context.user_data["fx_page"] = 0
    # 清空缓存，强制按新模式重新查询（不同模式查询区间不同，不能复用）
    context.application.bot_data["fx_cache"] = None
    await show_fixtures(update, context, page=0)


async def on_fixtures_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """日期导航条：点「明天」直接看明天的赛程，不用手打 /date 命令。

    「明天有什么球」是最常见的需求，而它原本只能靠记命令完成——
    按钮化的代价只是把偏移天数换算成具体日期，其余走既有的指定日期流程。
    """
    query = update.callback_query
    _, _, raw = (query.data or "").partition(":")
    await query.answer()
    try:
        offset = int(raw)
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, min(offset, 30))  # 只允许看未来一个月，防越界
    tz = context.application.bot_data["settings"].timezone
    day = (datetime.now(tz) + timedelta(days=offset)).date()
    context.user_data["fx_mode"] = MODE_DATE
    context.user_data["fx_date"] = day
    context.user_data["fx_page"] = 0
    context.application.bot_data["fx_cache"] = None
    await show_fixtures(update, context, page=0)


async def on_fixtures_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, raw = (query.data or "").partition(":")
    await query.answer()
    await show_fixtures(update, context, page=int(raw))


async def on_fixtures_league(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """点联赛按钮只看这一个联赛的赛程。

    全部联赛一起列时，五大联赛的比赛会交替出现、同一联赛标题被反复打印，
    想只看英超的比赛得自己在几十行里挑。筛选后序号与「预测/分析」按钮
    一并重排，点「预测 3」落在的就是当前这个联赛的第 3 场。

    筛选只切视图、不重新请求：赛程数据已在缓存里，切联赛零延迟。
    """
    query = update.callback_query
    _, _, raw = (query.data or "").partition(":")
    await query.answer()
    if raw == "all":
        context.user_data.pop("fx_league", None)
    else:
        try:
            context.user_data["fx_league"] = int(raw)
        except (TypeError, ValueError):
            pass
    context.user_data["fx_page"] = 0
    await show_fixtures(update, context, page=0)


async def show_standings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🏆 联赛排名：渲染积分榜。"""
    query = update.callback_query
    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    user_id = update.effective_user.id if update.effective_user else 0

    if not await begin_task(app.bot_data, user_id, "standings"):
        if query:
            await query.answer("正在获取，请稍候…")
        return
    try:
        rows = await service.get_standings_page()
    except APIError as exc:
        text = f"❌ <b>获取联赛排名失败</b>\n{esc(exc)}\n\n{ui.error_hint(exc)}"
        markup = back_to_menu_markup()
    except Exception as exc:
        log.exception("获取联赛排名失败")
        text = f"❌ <b>获取联赛排名失败</b>\n{esc(describe_error(exc))}"
        markup = back_to_menu_markup()
    else:
        text = ui.format_standings_page(
            rows,
            settings.timezone,
            league_name=f"联赛 {settings.league_id} · 赛季 {service.season_in_use}",
            updated=datetime.now(settings.timezone),
        )
        markup = ui.standings_keyboard()
    finally:
        end_task(app.bot_data, user_id, "standings")

    if query:
        await edit_view(query, text, markup)
    else:
        await reply_html(update.effective_message, text, markup)


async def on_analysis_fixture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """赛程里的 [📊 分析]：生成单场深度分析报告。"""
    query = update.callback_query
    _, _, raw = (query.data or "").partition(":")
    await query.answer()
    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    user_id = update.effective_user.id if update.effective_user else 0

    try:
        fixture_id = raw  # ID 可能是 'fd-123'（备用源）或纯数字（主源），不再强转 int
    except ValueError:
        return
    if not await begin_task(app.bot_data, user_id, f"analysis:{fixture_id}"):
        await query.answer("正在分析，请稍候…")
        return

    try:
        cache = app.bot_data.get("fx_cache")
        fixtures = cache["items"] if cache else None
        try:
            report = await service.analyze_fixture(fixture_id, fixtures)
        except KeyError:
            await edit_view(query, "⚠️ 该场比赛已不在今日赛程中，请返回赛程重新选择。", back_to_menu_markup())
            return
        except APIError as exc:
            text = f"❌ <b>生成深度分析失败</b>\n{esc(exc)}\n\n{ui.error_hint(exc)}"
            await edit_view(query, text, back_to_menu_markup())
            return
    except Exception as exc:  # 兜底：任何异常都不能让机器人崩掉
        log.exception("生成深度分析失败")
        await edit_view(query, f"❌ <b>生成深度分析失败</b>\n{esc(describe_error(exc))}", back_to_menu_markup())
        return
    finally:
        end_task(app.bot_data, user_id, f"analysis:{fixture_id}")

    await edit_view(query, ui.format_deep_report(report, settings.timezone), ui.analysis_keyboard(fixture_id))


async def on_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """图表按钮：chart:<类型>:<fixture_id>，图片以新消息发送并附返回按钮。"""
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("无效的图表请求")
        return
    kind, raw = parts[1], parts[2]
    await query.answer()
    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    user_id = update.effective_user.id if update.effective_user else 0

    try:
        fixture_id = raw  # ID 可能是 'fd-123'（备用源）或纯数字（主源），不再强转 int
    except ValueError:
        return
    if not await begin_task(app.bot_data, user_id, f"chart:{kind}:{fixture_id}"):
        await query.answer("正在生成图表，请稍候…")
        return

    try:
        if chart is None:  # matplotlib 未安装时优雅降级，不崩机器人
            await query.answer("图表功能不可用：缺少绘图依赖", show_alert=True)
            return
        cache = app.bot_data.get("fx_cache")
        fixtures = cache["items"] if cache else None
        if kind == "schedule":
            # 赛程总览图：用当前缓存的赛程，不针对单场比赛
            if not fixtures:
                await query.answer("暂无赛程数据，请先打开今日赛程", show_alert=True)
                return
            blob = chart.schedule_chart(
                fixtures,
                settings.timezone,
                day_label=getattr(service, "fixture_day_label", "") or "",
                source=service.source_label,
            )
            caption = f"📊 赛程分布 · 共 {len(fixtures)} 场"
        elif kind == "ring":
            try:
                prediction = service.get(fixture_id)
            except KeyError:
                prediction = await service.predict_fixture(fixture_id, fixtures)
            blob = chart.prob_ring(prediction, settings.timezone)
            caption = f"🎯 胜平负概率环 · {esc(prediction.home)} vs {esc(prediction.away)}"
        elif kind == "card":
            try:
                prediction = service.get(fixture_id)
            except KeyError:
                prediction = await service.predict_fixture(fixture_id, fixtures)
            a = prediction.analysis or {}
            lv = calculate_prediction_level({
                "home_win": a.get("win_prob", 0), "draw": a.get("draw_prob", 0),
                "away_win": a.get("loss_prob", 0),
            })
            blob = chart.match_card(
                prediction, settings.timezone, lv,
                league_name=league_label(settings.league_id),
                source=service.source_label,
            )
            caption = f"🎴 比赛主卡 · {esc(prediction.home)} vs {esc(prediction.away)}"
        elif kind == "prob":
            try:
                prediction = service.get(fixture_id)
            except KeyError:
                prediction = await service.predict_fixture(fixture_id, fixtures)
            blob = chart.prob_chart(prediction, settings.timezone)
            caption = f"📈 胜平负概率 · {esc(prediction.home)} vs {esc(prediction.away)}"
        else:
            report = await service.analyze_fixture(fixture_id, fixtures)
            blob = {
                "form": chart.form_chart,
                "goals": chart.goals_chart,
                "h2h": chart.h2h_chart,
            }[kind](report, settings.timezone)
            caption = {
                "form": "📊 近 5 场战绩对比",
                "goals": "📊 场均进失球对比",
                "h2h": "📊 历史交锋",
            }[kind] + " · " + esc(report["home"]) + " vs " + esc(report["away"])
    except (KeyError, ValueError):
        await query.answer("该场比赛已不在今日赛程中，请返回赛程重新选择。", show_alert=True)
        return
    except APIError as exc:
        await query.answer(f"获取数据失败：{exc}"[:180], show_alert=True)
        return
    except Exception as exc:  # 图表失败不能连累机器人
        log.exception("生成图表失败")
        await query.answer(f"生成图表失败：{type(exc).__name__}", show_alert=True)
        return
    finally:
        end_task(app.bot_data, user_id, f"chart:{kind}:{fixture_id}")

    if blob is None:  # 数据不足：明确提示，不发误导性图片
        await query.answer("暂无足够数据生成该图表", show_alert=True)
        return
    keyboard = None if kind == "schedule" else ui.chart_keyboard(fixture_id, kind)
    await query.message.reply_photo(
        photo=blob,
        caption=normalize(caption),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """页码指示器（不可点），只用于占位。"""
    await update.callback_query.answer()


# 未知命令回复里最多列几条指令：列全了会把用户的聊天记录刷掉一屏，
# 反而看不清自己刚才打错了什么。
_UNKNOWN_CMD_HINT_LIMIT = 8


def _unknown_command_text(name: str, commands: list, is_admin_user: bool) -> str:
    """拼「未识别命令」的回复文案。"""
    lines = [f"❓ <b>未识别的命令</b> /{esc(name)}"]
    if commands:
        rows = [f"/{n}　{d}" for n, d in commands[:_UNKNOWN_CMD_HINT_LIMIT]]
        lines.append("")
        lines.append("可用指令：")
        lines.extend(rows)
        if len(commands) > _UNKNOWN_CMD_HINT_LIMIT:
            lines.append(f"…等共 {len(commands)} 条，发送 /help 查看全部")
    else:
        lines.append("")
        lines.append("暂无可用指令。")
    if is_admin_user:
        lines.append("")
        lines.append("另外：管理员指令发送 /help 可查看完整列表。")
    else:
        lines.append("")
        lines.append("也可以直接点下方按钮开始。")
    return "\n".join(lines)


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """未注册命令的兜底：明确告知，而不是静默忽略。

    PTB 对没有任何 CommandHandler 匹配的命令默认不回应——用户看到自己发的
    消息孤零零挂着、机器人毫无反应，只会以为机器人掉线了。这里统一回一句
    并给出可用指令。

    注册在 group=1：group 0 里的所有 CommandHandler 先跑，都没匹配才轮到
    这里。用 group 而不是「放在最后注册」是因为后者依赖注册顺序，以后有人
    在中间插入一个 handler 就会把它顶掉，而这种错误测试很难发现。
    """
    message = update.effective_message
    if message is None or not message.text:
        return
    parts = message.text.strip().split()
    # 纯空白：strip 后 split 得到空列表，直接取 [0] 会 IndexError
    if not parts:
        return
    raw = parts[0]
    # 群里常见 /cmd@BotName 写法，@ 及之后是 bot 用户名，不参与匹配
    name = raw.lstrip("/").split("@", 1)[0].strip().lower()
    if not name:
        return
    app = context.application
    settings: Settings = app.bot_data.get("settings")
    is_admin_user = settings is not None and is_admin(update, settings)
    if is_admin_user:
        commands = list(app.bot_data.get("public_commands") or [])
        commands += list(app.bot_data.get("admin_commands") or [])
    else:
        commands = list(app.bot_data.get("public_commands") or [])
    await reply_html(message, _unknown_command_text(name, commands, is_admin_user), None)


async def on_panel_reopen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """「☰ 打开面板」按钮：把已收起的底部键盘重新展开。

    Reply 键盘只能随消息下发，所以这里必须补发一条消息把键盘带下去；
    不能只 answer 回调——那样键盘不会出现在屏幕上。
    """
    query = update.callback_query
    await query.answer()
    set_panel_state(context, PANEL_EXPANDED)
    await reply_html(
        query.message, PANEL_HINTS[PANEL_EXPANDED], ui.reply_menu_keyboard(True)
    )


async def on_predict_fixture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """赛程里的 [⚽ 预测]：为单场比赛生成预测卡片。"""
    query = update.callback_query
    _, _, raw = (query.data or "").partition(":")
    await query.answer()
    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    user_id = update.effective_user.id if update.effective_user else 0

    try:
        fixture_id = raw  # ID 可能是 'fd-123'（备用源）或纯数字（主源），不再强转 int
    except ValueError:
        return
    if not await begin_task(app.bot_data, user_id, f"pred:{fixture_id}"):
        await query.answer("正在生成预测，请稍候…")
        return

    try:
        cache = app.bot_data.get("fx_cache")
        fixtures = cache["items"] if cache else None
        try:
            prediction = await service.predict_fixture(fixture_id, fixtures)
        except KeyError:
            await edit_view(query, "⚠️ 该场比赛已不在今日赛程中，请返回赛程重新选择。", back_to_menu_markup())
            return
        except APIError as exc:
            text = f"❌ <b>生成预测失败</b>\n{esc(exc)}\n\n{ui.error_hint(exc)}"
            await edit_view(query, text, back_to_menu_markup())
            return
    except Exception as exc:  # 兜底：任何异常都不能让机器人崩掉
        log.exception("生成单场预测失败")
        await edit_view(query, f"❌ <b>生成预测失败</b>\n{esc(describe_error(exc))}", back_to_menu_markup())
        return
    finally:
        end_task(app.bot_data, user_id, f"pred:{fixture_id}")

    await edit_view(
        query,
        ui.format_prediction_card(prediction, settings.timezone),
        ui.prediction_keyboard(fixture_id),
    )


async def reply_digest(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """底部键盘入口：生成汇总后作为新消息回复（不是就地编辑）。

    与按钮入口的差别只在于输出方式：底部键盘点出来的是一条普通文字消息，
    没有可编辑的原消息，所以整份汇总逐页新发。
    """
    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    user_id = message.from_user.id
    if not await begin_task(app.bot_data, user_id, "digest"):
        return
    try:
        predictions, note = await _build_digest_predictions(service)
    except Exception as exc:
        log.exception("手动生成汇总失败")
        await reply_html(message, f"❌ <b>生成失败</b>\n{esc(describe_error(exc))}", None)
        return
    finally:
        end_task(app.bot_data, user_id, "digest")

    if not predictions:
        await reply_html(message, DigestView.format_digest_empty(note), None)
        return
    pages = DigestView.format_daily_digest_pages(
        predictions, settings, settings.timezone, note=note,
    )
    for page in pages:
        await reply_html(message, page, None)


def _panel_key_from_text(text: str) -> str | None:
    """识别底部面板的收放按钮，命中则返回目标状态。

    必须排在 _menu_key_from_text 之前判断：面板标签不是菜单项，若先走菜单
    解析会落到兜底分支，被误报成「功能正在开发中」。
    同时支持去掉 emoji 后的写法（手打「收起面板」/「菜单」同样生效）。
    """
    raw = (text or "").strip()
    if raw == PANEL_CLOSE_LABEL:
        return PANEL_COLLAPSED
    if raw == PANEL_OPEN_LABEL:
        return PANEL_EXPANDED
    normalized = _strip_emoji(raw)
    if normalized and normalized == _strip_emoji(PANEL_CLOSE_LABEL):
        return PANEL_COLLAPSED
    if normalized and normalized == _strip_emoji(PANEL_OPEN_LABEL):
        return PANEL_EXPANDED
    # 兼容旧写法：标签改成「打开面板」后，用户手打「菜单」仍应唤回面板，
    # 而不是掉进菜单解析被当成未知输入。
    if normalized in ("菜单", "打开面板", "展开面板"):
        return PANEL_EXPANDED
    return None


def _menu_key_from_text(text: str) -> str | None:
    """把用户输入或键盘文字解析成菜单 key。

    底部键盘按钮带 emoji（如 "⚽ 比赛预测"），但用户也可能手打纯文字
    （"比赛预测"）。这里先精确匹配，再按「去掉 emoji 与空白」归一化匹配，
    避免手打文字落到兜底分支被误报成"功能开发中"。
    """
    raw = (text or "").strip()
    key = MENU_BY_LABEL.get(raw)
    if key:
        return key
    normalized = _strip_emoji(raw)
    for label, menu_key in MENU_BY_LABEL.items():
        if _strip_emoji(label) == normalized:
            return menu_key
    return None


def _strip_emoji(text: str) -> str:
    """去掉 emoji 与所有空白，只保留可打印的普通字符用于比对。"""
    return "".join(ch for ch in (text or "") if not _is_emoji(ch) and not ch.isspace())


def _is_emoji(ch: str) -> bool:
    """判断单字符是否为 emoji/变体选择符/杂项符号。"""
    code = ord(ch)
    return (
        0x1F000 <= code <= 0x1FAFF  # 表情与符号主区
        or 0x2600 <= code <= 0x27BF  # 杂项符号与装饰符号
        or code in (0xFE0F, 0x20E3, 0x200D)  # 变体选择符 / 组合键 / 零宽连接
    )


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """底部 Reply 键盘：点击后按同一套逻辑处理。"""
    message = update.effective_message
    # 面板收放优先于菜单解析：两者都是底部文字按钮，但面板按钮不是功能入口
    panel = _panel_key_from_text(message.text)
    if panel:
        return await toggle_panel_message(message, context, panel)
    key = _menu_key_from_text(message.text)
    if not key:
        return
    settings: Settings = context.application.bot_data["settings"]
    if key == "storage":
        return await storage_cmd(update, context)
    if key == "help":
        await reply_html(update.effective_message, ui.format_help(), None)
    elif key == "web":
        await reply_html(update.effective_message, ui.web_text(settings.web_url), None)
    elif key == "refresh":
        context.application.bot_data["fx_cache"] = None
        await show_fixtures(update, context, page=0)
    elif key == "digest":
        await reply_digest(message, context)
    elif key in ("fixtures", "analysis", "predict"):
        # 这三个入口都需要先选一场比赛，统一落到赛程列表
        await show_fixtures(update, context, page=0)
    elif key == "standings":
        await show_standings(update, context)
    else:
        await reply_html(update.effective_message, ui.format_coming(key), None)


def render_view(action: str, p: Prediction, settings: Settings, h2h: list[dict] | None = None) -> str:
    tz = settings.timezone
    if action == "deep":
        return ui.format_deep_analysis(p, tz)
    if action == "odds":
        return ui.format_odds_detail(p, tz)
    if action == "h2h":
        return ui.format_h2h(p, h2h or [], tz)
    return ui.format_prediction(p, tz)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, raw_id = (query.data or "").partition(":")
    settings: Settings = context.application.bot_data["settings"]
    service: PredictionService = context.application.bot_data["service"]

    fixture_id = raw_id  # 支持 'fd-' 前缀（备用源 ID）
    try:
        prediction = service.get(fixture_id)
    except KeyError:
        await query.answer("这条预测已过期（机器人重启过），请等待下一次推送或让管理员使用 /test。", show_alert=True)
        return
    await query.answer()  # 先应答，避免按钮一直转圈

    view = "home" if action == "refresh" else action
    try:
        if action == "refresh":
            prediction = await service.refresh(fixture_id)
        h2h = await service.get_h2h(prediction) if view == "h2h" else None
        text = render_view(view, prediction, settings, h2h)
    except APIError as exc:
        text = f"❌ <b>获取数据失败</b>\n{esc(exc)}"
    await edit_view(query, text, ui.get_main_keyboard(fixture_id, view))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("处理更新时出错", exc_info=context.error)


# --- Heartbeat Start ---
# logging / datetime 已在模块顶部导入（第 9、14 行），此处不再重复导入。

async def send_heartbeat(application):
    admin_id = os.getenv("ADMIN_CHAT_ID")
    if admin_id:
        try:
            # 服务器多为 UTC，直接用 datetime.now() 会让心跳时间比本地早 8 小时，
            # 看起来像"时间不对"。按 TIMEZONE 配置取本地时间展示。
            tz = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))
            now_text = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
            await application.bot.send_message(
                chat_id=admin_id,
                text=f"🚀 [Bot Online]\n时间: {now_text}\n状态: 工业级部署已生效，心跳正常。"
            )
            logging.info("Heartbeat message sent to admin.")
        except Exception as e:
            logging.error(f"Heartbeat failed: {e}")

# --- Heartbeat End ---


# ---- 应用装配 ---------------------------------------------------------------
async def post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    # Webhook 残留会让 getUpdates 返回 409 并终止进程，必须在轮询前清掉。
    # 这一步只能放在 post_init（即 run_polling 内部的 event loop 里）：
    # 若在 main() 里用 asyncio.run() 提前清理，asyncio.run 结束时会关闭并置空
    # 当前 event loop，run_polling 内部的 get_event_loop 随即报错，
    # 导致 Updater.start_polling 协程从未被 await（容器启动几秒即退出、命令无响应）。
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except TelegramError as exc:
        log.warning("清理 Webhook 失败（继续启动）：%s", exc)
    api = FootballAPI(settings.api_key, provider=settings.api_provider)
    # 备用源：仅在配置了 Token 且启用时创建，否则为 None（行为与改动前完全一致）
    fallback = None
    if settings.football_data_available:
        fallback = FootballDataAPI(settings.football_data_token, timeout=settings.football_data_timeout)
        log.info("备用数据源 football-data.org 已启用")
    else:
        log.info("备用数据源 football-data.org 未配置或未启用，仅使用主数据源")
    # 免费层额度（100 次/天）在备用源侧没有查询端点，改为本地记录实际请求次数。
    try:
        from repository import bump_quota as _bump_quota
        api = DataSourceRouter(api, fallback, mode=settings.data_source_mode,
                               quota_sink=_bump_quota)
    except Exception:  # 计数不可用时机器人功能不受影响
        api = DataSourceRouter(api, fallback, mode=settings.data_source_mode)
    app.bot_data["api"] = api
    service = PredictionService(settings, api, MatchAnalyzer())
    app.bot_data["service"] = service
    # 启动同步：先把未来 30 天赛程拉到本地库，机器人起来就有数据可展示
    try:
        first = await service.sync.run_startup()
        log.info("启动同步：收到 %d 场，保存 %d 场（本地库共 %d 场）",
                 first.get("received", 0), first.get("saved", 0), service.repo.matches_count())
    except Exception as exc:  # 启动同步失败也要让机器人正常起来
        log.warning("启动同步失败（将按需回源）：%s", exc)

    # 在 application 启动后调用（异步上下文）
    await send_heartbeat(app)
    try:
        await app.bot.set_my_commands([BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS])
    except TelegramError as exc:
        log.warning("设置命令菜单失败：%s", exc)


async def post_shutdown(app: Application) -> None:
    api = app.bot_data.get("api")
    if api:
        await api.aclose()


def build_cmd_runtime() -> CommandRuntime:
    """把命令层需要的依赖注入进去。

    之所以在这里装配而不是模块导入时：_dispatch_menu_cmd / show_fixtures /
    reply_html 都定义在本模块靠后位置，运行时装配可避开定义顺序问题。
    """
    return CommandRuntime(
        dispatch_menu=_dispatch_menu_cmd,
        show_fixtures=show_fixtures,
        modes={"next": MODE_NEXT, "date": MODE_DATE},
    )


def register_commands(app: Application) -> int:
    """注册全部指令。新增指令只需在 commands/ 对应模块里 register 一行。"""
    app.bot_data["cmd_runtime"] = build_cmd_runtime()
    dispatcher = build_dispatcher()
    for spec in dispatcher.specs:
        app.add_handler(CommandHandler(spec.name, dispatcher.wrap(spec)))
    # 兜底 handler 要给出「可用指令」提示，需要这份清单。
    # admin_only 的单独存一份：不向普通用户暴露管理指令，但管理员自己
    # 打错字时应该看到完整列表，否则会以为机器人坏了。
    app.bot_data["public_commands"] = [
        (spec.name, spec.description)
        for spec in dispatcher.specs if not spec.admin_only
    ]
    app.bot_data["admin_commands"] = [
        (spec.name, spec.description)
        for spec in dispatcher.specs if spec.admin_only
    ]
    return len(dispatcher)


def build_application(settings: Settings) -> Application:
    app = (
        ApplicationBuilder()
        .token(settings.telegram_token)
        .defaults(Defaults(tzinfo=settings.timezone))  # 定时任务按 TIMEZONE 计时
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings

    register_commands(app)
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(on_fixtures_page, pattern=r"^fxp:\d+$"))
    app.add_handler(CallbackQueryHandler(on_fixtures_mode, pattern=r"^fxm:(today|upcoming|next|date)$"))
    app.add_handler(CallbackQueryHandler(on_fixtures_day, pattern=r"^fxd:\d+$"))
    app.add_handler(CallbackQueryHandler(on_fixtures_league, pattern=r"^fxl:(all|-?\d+)$"))
    app.add_handler(CallbackQueryHandler(on_predict_fixture, pattern=r"^fx:.+$"))
    app.add_handler(CallbackQueryHandler(on_analysis_fixture, pattern=r"^fa:.+$"))
    app.add_handler(CallbackQueryHandler(on_chart, pattern=r"^chart:(prob|ring|card|form|goals|h2h|schedule):.+$"))
    app.add_handler(CallbackQueryHandler(on_panel_reopen, pattern=r"^panel:open$"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(home|deep|h2h|odds|refresh):.+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
    # group=1：group 0 的所有 CommandHandler 先跑，都没匹配才到这里兜底。
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command), group=1)
    app.add_error_handler(on_error)

    if app.job_queue is None:
        raise RuntimeError('缺少定时任务依赖，请安装 "python-telegram-bot[job-queue]"')
    setup_scheduler(app, settings)
    return app


def main() -> None:
    load_dotenv()
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"配置错误：{exc}") from None
    setup_logging(settings)
    log.info(
        "机器人启动：league=%s season=%s 推送=%s(%s) 渠道=%s 管理员数=%d",
        settings.league_id,
        settings.season,
        f"{settings.push_time:%H:%M}",
        settings.timezone.zone,
        settings.api_provider,
        len(settings.admin_ids),
    )
    # 持久卷自检：未挂载时 Elo 评分与命中率统计会在重启后清零，必须提前暴露
    warn = paths.persistence_warning()
    if warn:
        log.warning("⚠️ %s", warn)
    else:
        log.info("持久化已生效：数据目录 %s（跨重启保留）", paths.DATA_DIR)
    app = build_application(settings)
    # run_polling 自行创建并管理 event loop，内部 await Updater.start_polling，
    # 并阻塞到收到停止信号——它是唯一的阻塞点，容器因此保持存活。
    # 事故复盘：此前"coroutine never awaited"的真正原因不是 run_polling 本身，
    # 而是它之前有一次 asyncio.run() 把 event loop 关闭置空了。
    # 异步清理（delete_webhook）已移入 post_init，故此处禁止再引入 asyncio.run。
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
