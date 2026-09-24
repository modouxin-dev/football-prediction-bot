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
    MessageHandler,
    filters,
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
LEAGUE_ID = os.getenv("LEAGUE_ID", "39")  # 默认英超
SEASON = int(os.getenv("SEASON", "2024"))
CHAT_ID = os.getenv("CHAT_ID")
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "UTC"))


async def send_daily_prediction(context: ContextTypes.DEFAULT_TYPE):
    """定时任务：推送每日重点预测"""
    try:
        logger.info("Starting daily prediction task...")
        
        # 获取未来10场比赛
        fixtures = api.get_fixtures(LEAGUE_ID, SEASON, next_matches=10)
        
        if not fixtures:
            logger.warning("No fixtures found")
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text="⚠️ 暂无未来赛程数据"
            )
            return
        
        # 分析前 3 场比赛
        for idx, fixture in enumerate(fixtures[:3]):
            try:
                await process_fixture(context, fixture)
                # 避免 API 频率限制
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error processing fixture {idx}: {e}")
                continue
        
        logger.info("Daily prediction task completed")
        
    except Exception as e:
        logger.error(f"Daily prediction task failed: {e}")
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"❌ 预测任务失败：{str(e)[:100]}"
        )


async def process_fixture(context: ContextTypes.DEFAULT_TYPE, fixture: dict):
    """处理单场比赛的预测"""
    try:
        # 提取比赛信息
        fixture_id = fixture["fixture"]["id"]
        home_team = fixture["teams"]["home"]
        away_team = fixture["teams"]["away"]
        fixture_date = fixture["fixture"]["date"]
        
        logger.info(f"Processing: {home_team['name']} vs {away_team['name']}")
        
        # 获取球队统计
        h_stats = api.get_statistics(home_team["id"], LEAGUE_ID, SEASON)
        a_stats = api.get_statistics(away_team["id"], LEAGUE_ID, SEASON)
        
        # 提取强度指标
        h_strength = analyzer.extract_stats(h_stats)
        a_strength = analyzer.extract_stats(a_stats)
        
        logger.debug(f"Home: {h_strength}, Away: {a_strength}")
        
        # 执行分析
        analysis = analyzer.calculate_prediction(h_strength, a_strength)
        
        # 获取赔率（可选）
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
        
        # 格式化消息
        match_info = {
            "league": "英超",
            "home": home_team["name"],
            "away": away_team["name"],
            "date": fixture_date
        }
        
        message = ui.format_prediction(match_info, analysis, value_bet, odds_value)
        
        # 发送消息
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
        "• 价值度：模型概率 vs 赔率\n"
        "• Value Bet：价值偏差 > 5%"
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
            ui.get_error_message("general", str(e)[:100]),
            parse_mode="HTML"
        )


# ========== 回调处理函数 ==========

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理内联按钮回调"""
    query = update.callback_query
    await query.answer()  # 移除加载动画
    
    try:
        # 从消息获取原始数据（需要修改消息以保存 fixture_id）
        # 简化实现：仅返回提示信息
        
        if query.data == "deep_analysis":
            await query.edit_message_text(
                text="🔬 <b>深度分析模块</b>\n"
                     "此功能需要消息中包含比赛ID。\n"
                     "在下次推送时将包含该功能。",
                parse_mode="HTML"
            )
        elif query.data == "h2h":
            await query.edit_message_text(
                text="📊 <b>H2H 历史对阵</b>\n"
                     "此功能需要消息中包含比赛ID。\n"
                     "在下次推送时将包含该功能。",
                parse_mode="HTML"
            )
        elif query.data == "odds_trend":
            await query.edit_message_text(
                text="📉 <b>赔率走势</b>\n"
                     "此功能需要消息中包含比赛ID。\n"
                     "在下次推送时将包含该功能。",
                parse_mode="HTML"
            )
        elif query.data == "refresh":
            await query.edit_message_text(
                text="🔄 数据已刷新\n"
                     "预测信息保持不变（实时刷新需要消息ID）。",
                parse_mode="HTML"
            )
            
    except TelegramError as e:
        logger.error(f"Callback error: {e}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理错误"""
    logger.error(f"Update {update} caused error {context.error}")


def main():
    """主函数 - 启动机器人"""
    # 验证必需的环境变量
    if not CHAT_ID:
        logger.error("CHAT_ID environment variable is not set")
        exit(1)
    
    if not os.getenv("TELEGRAM_TOKEN"):
        logger.error("TELEGRAM_TOKEN environment variable is not set")
        exit(1)
    
    if not os.getenv("RAPID_API_KEY"):
        logger.error("RAPID_API_KEY environment variable is not set")
        exit(1)
    
    # 创建应用
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    
    # 添加命令处理器
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("test", test_send))
    
    # 添加回调处理器
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # 添加错误处理器
    app.add_error_handler(error_handler)
    
    # 配置定时任务
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    
    # 每天早上 8 点运行
    scheduler.add_job(
        send_daily_prediction,
        "cron",
        hour=8,
        minute=0,
        args=[app],
        id="daily_prediction",
        name="Daily Football Prediction"
    )
    
    # 启动定时器
    app.post_init = lambda: scheduler.start()
    
    logger.info("=" * 50)
    logger.info("🤖 Football Prediction Bot Starting...")
    logger.info(f"League ID: {LEAGUE_ID}, Season: {SEASON}")
    logger.info(f"Timezone: {TIMEZONE}")
    logger.info(f"Daily prediction scheduled at 08:00 {TIMEZONE}")
    logger.info("=" * 50)
    
    # 启动机器人
    app.run_polling()


if __name__ == "__main__":
    main()

