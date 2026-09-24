import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


class BotUI:
    """Telegram 消息格式化和 UI 处理"""

    @staticmethod
    def format_prediction(match_data, analysis, value_bet=0, odds=None):
        """
        格式化预测消息
        
        Args:
            match_data: {'league': str, 'home': str, 'away': str, 'date': str}
            analysis: 分析结果
            value_bet: 价值偏差
            odds: 赔率信息（可选）
        """
        # 策略建议
        strategy = BotUI.get_strategy(analysis, value_bet)
        confidence = BotUI.get_confidence(analysis["win_prob"])
        
        # 预期进球数
        xg_home = f"{analysis['lambda_home']:.2f}"
        xg_away = f"{analysis['lambda_away']:.2f}"
        
        msg = (
            f"🏆 <b>{match_data['league']} | 重点赛事预测</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏠 <b>{match_data['home']}</b> 🆚 <b>{match_data['away']}</b>\n"
            f"📅 <code>{match_data['date']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>量化分析报告</b>\n"
            f"├ 预期比分：<code>{analysis['best_score']}</code>\n"
            f"├ 预期进球：主 <code>{xg_home}</code> 客 <code>{xg_away}</code>\n"
            f"├ 胜平负：<code>{analysis['win_prob']:.1%}</code> | "
            f"<code>{analysis['draw_prob']:.1%}</code> | "
            f"<code>{analysis['loss_prob']:.1%}</code>\n"
        )
        
        if odds:
            msg += f"├ 赔率：<code>{odds:.2f}</code>\n"
            msg += f"└ 价值度：<code>{value_bet:+.2%}</code> "
            msg += "🚀 <b>Value!</b>" if value_bet > 0.05 else ""
            msg += "\n"
        
        msg += (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>建议</b>：{strategy}\n"
            f"💎 <b>信心</b>：{confidence}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        
        return msg

    @staticmethod
    def format_h2h(h2h_data, home_name, away_name):
        """格式化 H2H 历史对阵"""
        if not h2h_data:
            return "❌ 暂无历史对阵数据"
        
        home_wins = 0
        away_wins = 0
        draws = 0
        total_goals_home = 0
        total_goals_away = 0
        
        for match in h2h_data[:10]:
            if match["goals"]["home"] > match["goals"]["away"]:
                home_wins += 1
            elif match["goals"]["home"] < match["goals"]["away"]:
                away_wins += 1
            else:
                draws += 1
            
            total_goals_home += match["goals"]["home"]
            total_goals_away += match["goals"]["away"]
        
        games = len(h2h_data[:10])
        avg_goals = (total_goals_home + total_goals_away) / games if games > 0 else 0
        
        msg = (
            f"📊 <b>历史对阵（近10场）</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏆 {home_name} 胜：<code>{home_wins}场</code>\n"
            f"🤝 平局：<code>{draws}场</code>\n"
            f"💔 {away_name} 胜：<code>{away_wins}场</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚽ 总进球：<code>{total_goals_home + total_goals_away}</code>\n"
            f"📈 平均进球：<code>{avg_goals:.2f}</code>"
        )
        
        return msg

    @staticmethod
    def format_odds_trend(fixture_data):
        """格式化赔率走势"""
        if not fixture_data:
            return "❌ 暂无赔率数据"
        
        odds = fixture_data.get("odds", {})
        if not odds:
            return "❌ 暂无赔率数据"
        
        # 提取主流博彩公司赔率
        msg = f"📉 <b>赔率走势</b>\n━━━━━━━━━━━━━━━━━━\n"
        
        bookmakers = [
            ("1xbet", "1xBet"),
            ("betfair", "Betfair"),
            ("pinnacle", "Pinnacle")
        ]
        
        for book_key, book_name in bookmakers:
            if book_key in odds:
                bookmaker_odds = odds[book_key].get("bets", [])
                if bookmaker_odds:
                    bet = bookmaker_odds[0]
                    values = bet.get("values", [])
                    if len(values) >= 3:
                        msg += (
                            f"<b>{book_name}</b>\n"
                            f"├ 主胜：<code>{values[0]['odd']}</code>\n"
                            f"├ 平：<code>{values[1]['odd']}</code>\n"
                            f"└ 客胜：<code>{values[2]['odd']}</code>\n"
                        )
        
        msg += "━━━━━━━━━━━━━━━━━━"
        return msg

    @staticmethod
    def format_deep_analysis(analysis):
        """格式化深度分析"""
        msg = (
            f"🔬 <b>深度分析</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>比分概率矩阵 (前6行6列)</b>\n"
        )
        
        # 显示概率矩阵的一部分
        prob_matrix = analysis.get("prob_matrix", [])
        for h in range(min(3, len(prob_matrix))):
            row_str = " | ".join(f"{prob_matrix[h][a]:.3f}" for a in range(min(4, len(prob_matrix[h]))))
            msg += f"<code>{h}: {row_str}</code>\n"
        
        msg += f"━━━━━━━━━━━━━━━━━━\n"
        msg += (
            f"📊 统计信息\n"
            f"├ 主队预期进球(λ)：<code>{analysis['lambda_home']:.3f}</code>\n"
            f"├ 客队预期进球(λ)：<code>{analysis['lambda_away']:.3f}</code>\n"
            f"└ 模型：泊松分布\n"
        )
        
        return msg

    @staticmethod
    def get_strategy(analysis, value):
        """根据分析结果生成策略建议"""
        win_prob = analysis.get("win_prob", 0)
        
        if value > 0.1:
            return "🚀 <b>强势主胜推荐</b>"
        elif value > 0.05:
            return "👍 <b>主胜价值推荐</b>"
        elif win_prob > 0.65:
            return "✅ <b>主队不败(1X)</b>"
        elif win_prob > 0.5:
            return "📊 <b>主队微弱优势</b>"
        elif analysis.get("draw_prob", 0) > 0.4:
            return "🤝 <b>平局可能性大</b>"
        else:
            return "⚠️ <b>观望/小注策略</b>"

    @staticmethod
    def get_confidence(prob):
        """根据概率等级确定信心指数"""
        if prob > 0.75:
            return "⭐⭐⭐⭐⭐ (极高置信度)"
        elif prob > 0.65:
            return "⭐⭐⭐⭐ (高置信度)"
        elif prob > 0.5:
            return "⭐⭐⭐ (中等置信度)"
        elif prob > 0.35:
            return "⭐⭐ (低置信度)"
        else:
            return "⭐ (极低置信度)"

    @staticmethod
    def get_main_keyboard():
        """获取主菜单键盘"""
        keyboard = [
            [
                InlineKeyboardButton("🔍 深度分析", callback_data="deep_analysis"),
                InlineKeyboardButton("📊 H2H对阵", callback_data="h2h")
            ],
            [
                InlineKeyboardButton("📉 赔率走势", callback_data="odds_trend"),
                InlineKeyboardButton("🔄 刷新数据", callback_data="refresh")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_error_message(error_type, error_detail=""):
        """生成错误消息"""
        errors = {
            "api": f"❌ API 错误：{error_detail}",
            "stats": "❌ 数据获取失败，请重试",
            "odds": "❌ 赔率数据暂不可用",
            "general": f"❌ 出错：{error_detail}"
        }
        return errors.get(error_type, "❌ 未知错误")

