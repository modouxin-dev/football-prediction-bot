"""足球量化分析 Telegram 机器人入口。

- 每天在 PUSH_TIME（时区 TIMEZONE）向 CHAT_ID 推送即将开赛的比赛预测
- /test：手动触发一次推送（仅管理员）；/status：运行状态与数据源诊断（仅管理员）
- 推送消息下方的按钮（预测 / 深度分析 / 历史交锋 / 赔率对比 / 刷新）在原消息上就地切换
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
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

from analyzer import MatchAnalyzer
from api_client import APIError, FootballAPI
from data_source import DataSourceError, DataSourceRouter
from football_data import FootballDataAPI
from bot_handler import MENU_ITEMS, BotUI, esc
from config import ConfigError, Settings, load_settings
from service import Prediction, PredictionService

try:  # 图表依赖缺失时机器人仍要能正常跑，只是不出图
    import chart
except ImportError:  # pragma: no cover
    chart = None

log = logging.getLogger("bot")
ui = BotUI()

DAILY_JOB = "daily_push"
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
def is_admin(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    return user is not None and user.id in settings.admin_ids


def describe_error(exc: Exception) -> str:
    if isinstance(exc, APIError):
        return f"数据源错误：{exc}"
    if isinstance(exc, TelegramError):
        return f"Telegram 错误：{exc}"
    return f"{type(exc).__name__}: {exc}"[:300]


async def deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "未知"
    await update.effective_message.reply_text(
        f"🚫 仅管理员可用。你的 Telegram 用户 ID 是 {uid}，如需授权请把它加入 ADMIN_ID 环境变量。"
    )


async def notify_admins(app: Application, text: str) -> None:
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


# ---- 推送 -------------------------------------------------------------------
@dataclass
class PushResult:
    sent: int
    note: str | None = None


async def run_push(app: Application, *, widen: bool = False) -> PushResult:
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    if settings.chat_target is None:
        raise RuntimeError("未设置 CHAT_ID，无法推送")

    predictions = await service.build_predictions()
    note = service.last_note
    if not predictions and widen:
        predictions = await service.build_predictions(lookahead_hours=WIDE_HOURS)
        if predictions:
            note = f"未来 {settings.lookahead_hours} 小时内没有未开赛的比赛，已放宽到 {WIDE_HOURS // 24} 天内用于测试。"
        elif service.last_note:
            note = service.last_note  # 降级/空结果的真实原因，必须让用户看到

    for p in predictions:
        await app.bot.send_message(
            chat_id=settings.chat_target,
            text=ui.format_prediction(p, settings.timezone),
            parse_mode=ParseMode.HTML,
            reply_markup=ui.get_main_keyboard(p.fixture_id, "home"),
        )
    return PushResult(len(predictions), note)


async def daily_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    try:
        result = await run_push(app)
        log.info("每日推送完成，共 %d 场", result.sent)
    except Exception as exc:  # 定时任务里的任何失败都要让管理员知道
        log.exception("每日推送失败")
        await notify_admins(app, f"❌ 每日推送失败：{describe_error(exc)}")


# ---- 命令 -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        "⚽ 足球量化预测机器人\n\n"
        "功能：\n"
        "├ 每日自动推送赛事预测\n"
        "├ 泊松分布量化分析\n"
        "├ 赔率价值评估\n"
        "└ 历史交锋数据\n\n"
        f"推送时间：每天 {s.push_time:%H:%M}（{ui.tz_label(s.timezone)}）\n"
        "发送 /help 查看命令",
        reply_markup=ui.reply_menu_keyboard(),  # 底部常驻菜单
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📖 命令列表\n"
        "/start  欢迎信息与推送时间\n"
        "/help  显示本帮助\n"
        "/test  立即生成并推送一次预测（管理员）\n"
        "/status  运行状态与数据源诊断（管理员）\n\n"
        "预测消息下方的按钮：预测 / 深度分析 / 历史交锋 / 赔率对比，点击后在原消息上切换；"
        "「刷新赔率」会重新拉取最新赔率。"
    )


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """手动触发一次推送，走与定时任务完全相同的路径，用来验证 Telegram 与数据源都正常。"""
    app = context.application
    if not is_admin(update, app.bot_data["settings"]):
        return await deny(update)
    progress = await update.effective_message.reply_text("⏳ 正在获取数据并生成预测…")
    try:
        result = await run_push(app, widen=True)
    except Exception as exc:
        log.exception("/test 失败")
        await progress.edit_text(f"❌ 发送失败：{describe_error(exc)}")
        return
    if result.sent == 0:
        await progress.edit_text("ℹ️ 没有可推送的比赛（近期赛程为空或已全部开赛），未发送任何消息。")
    else:
        extra = f"\n{result.note}" if result.note else ""
        await progress.edit_text(f"✅ 已向 CHAT_ID 发送 {result.sent} 条预测。{extra}")


# ---- 主菜单与今日赛程 -----------------------------------------------------------
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
        cache = app.bot_data.get("fx_cache")
        if cache is None or cache.get("date") != label:
            items = await service.get_today_fixtures()  # 失败会抛 APIError，错误不写入缓存
            cache = {"date": label, "items": items}
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
        text, markup, page, _ = ui.format_fixtures_page(items, tz, page, FX_PER_PAGE, label)
        if not items and service.last_note:
            text = f"{text}\n\n{esc(service.last_note)}"
    finally:
        end_task(app.bot_data, user_id, "fixtures")

    context.user_data["fx_page"] = page  # 页码按用户隔离
    if query:
        await edit_view(query, text, markup)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        ui.format_menu(settings), parse_mode=ParseMode.HTML, reply_markup=ui.menu_keyboard()
    )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, key = (query.data or "").partition(":")
    await query.answer()
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
    elif key == "standings":
        await show_standings(update, context)
    elif key == "analysis":
        await show_fixtures(update, context, page=0)  # 深度分析要先选比赛
    elif key == "prediction":
        await show_fixtures(update, context, page=0)  # 比赛预测要先选比赛
    else:
        await edit_view(query, ui.format_coming(key), ui.menu_keyboard())


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
            league_label=f"联赛 {settings.league_id} · 赛季 {service.season_in_use}",
            updated=datetime.now(settings.timezone),
        )
        markup = ui.standings_keyboard()
    finally:
        end_task(app.bot_data, user_id, "standings")

    if query:
        await edit_view(query, text, markup)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


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
        if kind == "prob":
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
    await query.message.reply_photo(
        photo=blob,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=ui.chart_keyboard(fixture_id, kind),
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


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """底部 Reply 键盘：点击后按同一套逻辑处理。"""
    key = MENU_BY_LABEL.get((update.effective_message.text or "").strip())
    if not key:
        return
    if key == "help":
        await update.effective_message.reply_text(ui.format_help(), parse_mode=ParseMode.HTML)
    elif key == "refresh":
        context.application.bot_data["fx_cache"] = None
        await show_fixtures(update, context, page=0)
    elif key == "fixtures" or key == "analysis":
        await show_fixtures(update, context, page=0)
    elif key == "standings":
        await show_standings(update, context)
    else:
        await update.effective_message.reply_text(ui.format_coming(key), parse_mode=ParseMode.HTML)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    s: Settings = app.bot_data["settings"]
    if not is_admin(update, s):
        return await deny(update)
    api = app.bot_data["api"]
    service: PredictionService = app.bot_data["service"]

    jobs = app.job_queue.get_jobs_by_name(DAILY_JOB) if app.job_queue else []
    next_run = jobs[0].next_t.astimezone(s.timezone).strftime("%m-%d %H:%M") if jobs and jobs[0].next_t else "未启用"
    version = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "unknown")[:7]
    season_line = f"联赛/赛季：{s.league_id} / {s.season}"
    if s.season != s.expected_season:
        season_line += f"（⚠️ 按日期应为 {s.expected_season}，请更新 SEASON 变量；无赛程时会自动改用 {s.expected_season}）"
    if service.season_in_use != s.season:
        season_line += f"\n实际使用赛季：{service.season_in_use}（已自动降级）"

    lines = [
        "🤖 运行状态",
        f"版本：{version}",
        season_line,
        f"推送：每天 {s.push_time:%H:%M}（{s.timezone.zone}），每次最多 {s.max_matches} 场，窗口 {s.lookahead_hours} 小时",
        f"下次推送：{next_run}",
        f"CHAT_ID：{'已配置' if s.chat_id else '未配置'}",
        f"内存中的预测：{service.cached_predictions} 场",
        f"数据源渠道：{'官方直连 (API-Sports)' if s.api_provider == 'apisports' else 'RapidAPI'}",
        f"数据源：{getattr(api, 'source_label', 'API-Football')}"
        + ("（备用源生效中）" if getattr(api, 'using_fallback', False) else ""),
        f"备用源 football-data.org：{'已配置' if s.football_data_available else '未配置'}",
    ]
    try:
        account = await api.get_account_status()
        sub, req = account.get("subscription") or {}, account.get("requests") or {}
        lines.append(f"套餐：{sub.get('plan', '未知')}（{'有效' if sub.get('active') else '未激活或未知'}）")
        lines.append(f"今日请求：{req.get('current', '?')} / {req.get('limit_day', '?')}")
        lines.append("数据源连通：✅")
    except APIError as exc:
        lines.append(f"数据源连通：❌ {exc}")
    await update.effective_message.reply_text("\n".join(lines))


# ---- 按钮 -------------------------------------------------------------------
def render_view(action: str, p: Prediction, settings: Settings, h2h: list[dict] | None = None) -> str:
    tz = settings.timezone
    if action == "deep":
        return ui.format_deep_analysis(p, tz)
    if action == "odds":
        return ui.format_odds_detail(p, tz)
    if action == "h2h":
        return ui.format_h2h(p, h2h or [], tz)
    return ui.format_prediction(p, tz)


async def edit_view(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():  # 点击的正是当前页面时 Telegram 会报这个，忽略即可
            raise


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


# ---- 应用装配 ---------------------------------------------------------------
async def post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
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
    app.bot_data["service"] = PredictionService(settings, api, MatchAnalyzer())
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "欢迎信息与推送时间"),
                BotCommand("menu", "打开功能菜单"),
                BotCommand("help", "命令说明"),
                BotCommand("test", "立即推送一次预测（管理员）"),
                BotCommand("status", "运行状态与数据源诊断（管理员）"),
            ]
        )
    except TelegramError as exc:
        log.warning("设置命令菜单失败：%s", exc)


async def post_shutdown(app: Application) -> None:
    api = app.bot_data.get("api")
    if api:
        await api.aclose()


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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(on_fixtures_page, pattern=r"^fxp:\d+$"))
    app.add_handler(CallbackQueryHandler(on_predict_fixture, pattern=r"^fx:.+$"))
    app.add_handler(CallbackQueryHandler(on_analysis_fixture, pattern=r"^fa:.+$"))
    app.add_handler(CallbackQueryHandler(on_chart, pattern=r"^chart:(prob|form|goals|h2h):.+$"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(home|deep|h2h|odds|refresh):.+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))
    app.add_error_handler(on_error)

    if app.job_queue is None:
        raise RuntimeError('缺少定时任务依赖，请安装 "python-telegram-bot[job-queue]"')
    if settings.chat_id:
        app.job_queue.run_daily(daily_push, time=settings.push_time, name=DAILY_JOB)
    else:
        log.warning("未设置 CHAT_ID：定时推送已停用（仍可使用命令）")
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
    app = build_application(settings)
    app.run_polling(drop_pending_updates=True, allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
