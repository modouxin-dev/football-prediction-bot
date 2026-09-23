from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

class BotUI:
    @staticmethod
    def format_prediction(match_data, analysis, value_bet):
        # HTML 美化模板
        msg = (
            f"🏆 <b>{match_data['league']} | 重点赛事预测</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏠 <b>{match_data['home']}</b> 🆚 <b>{match_data['away']}</b>\n"
            f"📅 时间：<code>{match_data['date']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>量化分析报告</b>：\n"
            f"├ 预期比分：<code>{analysis['best_score']}</code>\n"
            f"├ 胜平负概率：<code>胜 {analysis['win_prob']:.1%} | 平 {analysis['draw_prob']:.1%} | 负 {analysis['loss_prob']:.1%}</code>\n"
            f"└ 价值偏差：<code>{value_bet:+.2%}</code> {'🚀 (Value Bet!)' if value_bet > 0.05 else ''}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>建议策略</b>：<b>{BotUI.get_strategy(analysis, value_bet)}</b>\n"
            f"💎 <b>信心指数</b>：{BotUI.get_confidence(analysis['win_prob'])}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        return msg

    @staticmethod
    def get_strategy(analysis, value):
        if value > 0.07: return "重仓主胜"
        if analysis['win_prob'] > 0.5: return "主队不败 (1X)"
        return "观望/小注"

    @staticmethod
    def get_confidence(prob):
        if prob > 0.7: return "⭐⭐⭐⭐⭐"
        if prob > 0.5: return "⭐⭐⭐"
        return "⭐⭐"

    @staticmethod
    def get_main_keyboard():
        keyboard = [
            [
                InlineKeyboardButton("🔍 深度分析", callback_data="deep_analysis"),
                InlineKeyboardButton("📊 H2H 对阵", callback_data="h2h")
            ],
            [InlineKeyboardButton("📉 赔率走势", callback_data="odds_trend")]
        ]
        return InlineKeyboardMarkup(keyboard)
