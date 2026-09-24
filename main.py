import os
import asyncio
import logging
import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from api_client import FootballAPI, APIError
from analyzer import MatchAnalyzer
from bot_handler import BotUI
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 初始化组件
api = FootballAPI(cache_ttl_seconds=3600)
analyzer = MatchAnalyzer()
ui = BotUI()

# 配置
LEAGUE_ID = os.getenv("LEAGUE_ID", "39")
SEASON = int(os.getenv("SEASON", "2024"))
CHAT_ID = os.getenv("CHAT_ID")
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "UTC"))

# 全局变量
scheduler = None
app = None


async def send_daily_prediction(context: ContextTypes.DEFAULT_TYPE):
    """定时任务：推送每日重点预测"""
    try:
        logger.info("Starting daily prediction task...")
        
        fixtures = api.get_fixtures(LEAGUE_ID, SEASON, next_matches=10)
        
        if not fixtures:
            logger.warning("No fixtures found")
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text="⚠️ 暂无未来赛程数据"
            )
            return
        
        for idx, fixture in enumerate(fixtures[:3]):
            try:
                await process_fixture(context, fixture)
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error processing fixture {idx}: {e}")
                continue
        
        logger.info("Daily prediction task completed")
        
    except Exception as e:
        logger.error(f"Daily prediction task failed: {e}")
        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"❌ 预测任务失败：{str(e)[:100]}"
            )
        except:
            pass


async def process_fixture(context: ContextTypes.DEFAULT_TYPE, fixture: dict):
    """处理单场比赛的预测"""
    try:
        fixture_id = fixture["fixture"]["id"]
        home_team = fixture["teams"]["home"]
        away_team = fixture["teams"]["away"]
        fixture_date = fixture["fixture"]["date"]
        
        logger.info(f"Processing: {home_team['name']} vs {away_team['name']}")
        
        h_stats = api.get_statistics(home_team["id"], LEAGUE_ID, SEASON)
        a_stats = api.get_statistics(away_team["id"], LEAGUE_ID, SEASON)
        
        h_strength = analyzer.extract_stats(h_stats)
        a_strength = analyzer.extract_stats(a_stats)
        
        analysis = analyzer.calculate_prediction(h_strength, a_strength)
        
        odds_data = api.get_odds(fixture_id)
        odds_value = None
        value_bet = 0
        
        if odds_data and "bookmakers" in odds_data and len(odds_data["bookmakers"]) > 0:
            try:
                first_bet = odds_data["bookmakers"][0].get("bets", [])[0]
                values = first_bet.get("values", [])
                if values:
                    odds_value = float(values[0]["odd"])
                    value_bet = analyzer.analyze_value(analysis["win_prob"], odds_value)
            except (IndexError, KeyError, ValueError):
                pass
        
        match_info = {
            "league": "英超",
            "home": home_team["name"],
            "away": away_team["name"],
            "date": fixture_date
        }
        
        message = ui.format_prediction(match_info, analysis, value_bet, odds_value)
        
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode="HTML",
            reply_markup=ui.get_main_keyboard()
        )
        
        logger.info(f"✅ Prediction sent for {home_team['name']} vs {away_team['name']}")
        
    except APIError as e:
        logger.error(f"API error: {e}")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in process_fixture: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    await update.message.reply_text(
        "⚽ <b>足球量化预测机器人</b>\n\n"
        "功能：\n"
        "├ 每日自动推送赛事预测\n"
        "├ 泊松分布量化分析\n"
        "├ 赔率价值评估\n"
        "└ 历史对阵数据\n\n"
        "使用 /test 进行测试推送\n"
        "使用 /help 获取帮助",
        parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    help_text = (
        "📖 <b>命令列表</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/start - 显示欢迎信息\n"
        "/test - 手动测试推送\n"
        "/help - 显示此帮助\n"
        "/status - 查看机器人状态\n\n"
        "<b>预测解读</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• 预期比分：基于泊松分布计算\n"
        "• 胜平负概率：历史数据驱动\n"
        "• 价值度：模型概率 vs 赔率"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看机器人状态"""
    status_msg = (
        f"🤖 <b>机器人状态</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ 在线\n"
        f"⚙️ 联赛ID：{LEAGUE_ID}\n"
        f"📅 赛季：{SEASON}\n"
        f"🕐 时区：{TIMEZONE}\n"
        f"📊 缓存状态：活跃\n"
        f"⏰ 当前时间：{datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await update.message.reply_text(status_msg, parse_mode="HTML")


async def test_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动触发一次推送"""
    try:
        await update.message.reply_text("⏳ 正在获取数据并生成预测...")
        await send_daily_prediction(context)
        await update.message.reply_text("✅ 测试推送完成")
    except Exception as e:
        logger.error(f"Test send failed: {e}")
        await update.message.reply_text(
            f"❌ 错误：{str(e)[:100]}",
            parse_mode="HTML"
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理内联按钮回调"""
    query = update.callback_query
    await query.answer()
    
    try:
        if query.data == "deep_analysis":
            await query.edit_message_text(
                text="🔬 <b>深度分析</b>\n此功能开发中",
                parse_mode="HTML"
            )
        elif query.data == "h2h":
            await query.edit_message_text(
                text="📊 <b>H2H 对阵</b>\n此功能开发中",
                parse_mode="HTML"
            )
        elif query.data == "odds_trend":
            await query.edit_message_text(
                text="📉 <b>赔率走势</b>\n此功能开发中",
                parse_mode="HTML"
            )
    except TelegramError as e:
        logger.error(f"Callback error: {e}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理错误"""
    logger.error(f"Error: {context.error}")


def main():
    """主函数 - 启动机器人"""
    global scheduler, app
    
    # 环境变量验证
    if not CHAT_ID:
        logger.error("CHAT_ID is not set")
        exit(1)
    
    if not os.getenv("TELEGRAM_TOKEN"):
        logger.error("TELEGRAM_TOKEN is not set")
        exit(1)
    
    if not os.getenv("RAPID_API_KEY"):
        logger.error("RAPID_API_KEY is not set")
        exit(1)
    
    # 创建应用
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    
    # 添加处理器
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("test", test_send))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    
    # 配置定时任务
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        send_daily_prediction,
        "cron",
        hour=8,
        minute=0,
        args=[app],
        id="daily_prediction",
        name="Daily Football Prediction"
    )
    
    # 正确的 post_init 方式
    async def post_init_callback(app_instance):
        scheduler.start()
        logger.info("APScheduler started successfully")
    
    app.post_init = post_init_callback
    
    logger.info("=" * 50)
    logger.info("🤖 Football Prediction Bot Starting...")
    logger.info(f"League ID: {LEAGUE_ID}, Season: {SEASON}")
    logger.info(f"Timezone: {TIMEZONE}")
    logger.info(f"Daily task scheduled at 08:00 {TIMEZONE}")
    logger.info("=" * 50)
    
    # 启动机器人 - run_polling 会一直监听消息
    app.run_polling()


if __name__ == "__main__":
    main()

