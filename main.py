import os
import asyncio
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from api_client import FootballAPI
from analyzer import MatchAnalyzer
from bot_handler import BotUI
from dotenv import load_dotenv

load_dotenv()

api = FootballAPI()
analyzer = MatchAnalyzer()
ui = BotUI()

async def send_daily_prediction(context: ContextTypes.DEFAULT_TYPE):
    """定时任务：推送今日重点预测"""
    fixtures = api.get_fixtures(os.getenv("LEAGUE_ID"), os.getenv("SEASON"))
    for fix in fixtures[:3]: # 推送前三场
        # 1. 获取数据
        h_id, a_id = fix['teams']['home']['id'], fix['teams']['away']['id']
        h_stats = api.get_statistics(h_id, os.getenv("LEAGUE_ID"), os.getenv("SEASON"))
        a_stats = api.get_statistics(a_id, os.getenv("LEAGUE_ID"), os.getenv("SEASON"))
        
        # 简化强度计算 (实际应从 stats 中计算平均进球)
        # 这里示意：实际代码需接入计算逻辑
        mock_stats_h = {'attack': 1.5, 'defense': 0.8} 
        mock_stats_a = {'attack': 1.1, 'defense': 1.2}
        
        res = analyzer.calculate_prediction(mock_stats_h, mock_stats_a)
        
        # 获取赔率
        odds_data = api.get_odds(fix['fixture']['id'])
        odds_win = 2.1 if odds_data else 2.0 # 默认值
        value = analyzer.analyze_value(res['win_prob'], odds_win)
        
        match_info = {
            'league': '英超', 'home': fix['teams']['home']['name'], 
            'away': fix['teams']['away']['name'], 'date': fix['fixture']['date']
        }
        
        await context.bot.send_message(
            chat_id=os.getenv("CHAT_ID"), 
            text=ui.format_prediction(match_info, res, value),
            parse_mode='HTML',
            reply_markup=ui.get_main_keyboard()
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚽ 足球量化分析机器人已启动，等待定时推送...")

async def test_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动触发一次推送，便于测试 Telegram 发送是否正常"""
    try:
        await send_daily_prediction(context)
        await update.message.reply_text("✅ 测试消息已发送")
    except Exception as e:
        await update.message.reply_text(f"❌ 发送失败：{e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    
    # 指令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_send))
    
    # 调度器（需显式指定 pytz 时区，否则在部分环境下会因系统时区非 pytz 对象而报错崩溃）
    scheduler = AsyncIOScheduler(timezone=pytz.utc)
    # 每天早上 8 点（UTC）运行
    scheduler.add_job(send_daily_prediction, 'cron', hour=8, args=[app])
    scheduler.start()
    
    print("Robot is running...")
    app.run_polling()
