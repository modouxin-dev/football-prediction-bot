"""足球量化分析 Telegram 机器人入口。

- 每天在 PUSH_TIME（时区 TIMEZONE）向 CHAT_ID 推送即将开赛的比赛预测
- /test：手动触发一次推送（仅管理员）；/status：运行状态与数据源诊断（仅管理员）
- 推送消息下方的按钮（预测 / 深度分析 / 历史交锋 / 赔率对比 / 刷新）在原消息上就地切换
"""
from __future__ import annotations

import logging
import os

import paths
import sys
from datetime import datetime

from dotenv import load_dotenv
from telegram import BotCommand, Update
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
from support import (back_to_menu_markup, begin_task, deny, describe_error,
                     edit_view, end_task, is_admin, reply_html)
from commands.adapters import FakeUpdate
from commands.admin import storage_cmd
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
        text, markup, page, _ = ui.format_fixtures_page(
            items, tz, page, FX_PER_PAGE, day_label, multi_day=multi_day,
            empty_range=cache.get("season_range"),
            empty_source_ok=cache.get("status") != ST_NO_DATA,
        )
        # 三态渲染：有数据不啰嗦；窗口无比赛给范围+下一步；接口无数据说清真实原因
        note = cache.get("note")
        if not items and note:
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


async def on_fixtures_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, raw = (query.data or "").partition(":")
    await query.answer()
    await show_fixtures(update, context, page=int(raw))


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
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """页码指示器（不可点），只用于占位。"""
    await update.callback_query.answer()


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
    key = _menu_key_from_text(update.effective_message.text)
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
import logging
from datetime import datetime

async def send_heartbeat(application):
    admin_id = os.getenv("ADMIN_CHAT_ID")
    if admin_id:
        try:
            await application.bot.send_message(
                chat_id=admin_id, 
                text=f"🚀 [Bot Online]\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n状态: 工业级部署已生效，心跳正常。"
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
    app.add_handler(CallbackQueryHandler(on_predict_fixture, pattern=r"^fx:.+$"))
    app.add_handler(CallbackQueryHandler(on_analysis_fixture, pattern=r"^fa:.+$"))
    app.add_handler(CallbackQueryHandler(on_chart, pattern=r"^chart:(prob|ring|card|form|goals|h2h|schedule):.+$"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(home|deep|h2h|odds|refresh):.+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
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
