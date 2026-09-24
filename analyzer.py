import math
import logging

logger = logging.getLogger(__name__)


class MatchAnalyzer:
    """量化足球分析引擎 - 基于泊松分布"""
    
    # 联赛平均进球数（可根据实际联赛调整）
    LEAGUE_AVERAGES = {
        "premier_league": 2.8,
        "la_liga": 2.7,
        "serie_a": 2.5,
        "bundesliga": 3.0,
        "ligue_1": 2.6,
        "default": 2.65
    }

    @staticmethod
    def poisson_prob(lmbda, x):
        """泊松分布计算公式"""
        if lmbda < 0 or x < 0:
            return 0
        try:
            return (math.exp(-lmbda) * (lmbda**x)) / math.factorial(x)
        except (ValueError, OverflowError):
            logger.warning(f"Poisson calculation error: lambda={lmbda}, x={x}")
            return 0

    @staticmethod
    def extract_stats(team_stats_dict):
        """
        从 API 响应的球队统计数据中提取关键指标
        返回: {'attack': float, 'defense': float}
        """
        if not team_stats_dict:
            return {"attack": 1.0, "defense": 1.0}
        
        try:
            # 从 API 统计数据中提取
            goals_for = team_stats_dict.get("goals", {}).get("for", {}).get("total", 1)
            goals_against = team_stats_dict.get("goals", {}).get("against", {}).get("total", 1)
            games = team_stats_dict.get("fixtures", {}).get("played", {}).get("total", 1)
            
            # 计算平均进攻和防守强度
            attack_strength = goals_for / max(games, 1)
            defense_strength = goals_against / max(games, 1)
            
            # 归一化（相对于平均值 2.65）
            league_avg = 2.65
            attack = attack_strength / league_avg if attack_strength > 0 else 1.0
            defense = defense_strength / league_avg if defense_strength > 0 else 1.0
            
            return {
                "attack": max(attack, 0.5),  # 防止异常值
                "defense": max(defense, 0.5)
            }
        except (KeyError, TypeError) as e:
            logger.warning(f"Stats extraction error: {e}")
            return {"attack": 1.0, "defense": 1.0}

    def calculate_prediction(self, home_stats, away_stats, league_avg_goals=None):
        """
        核心量化分析
        Args:
            home_stats: {'attack': float, 'defense': float}
            away_stats: {'attack': float, 'defense': float}
            league_avg_goals: 联赛平均进球数（可选）
        
        Returns:
            分析结果字典
        """
        if league_avg_goals is None:
            league_avg_goals = 2.65
            
        # 计算预期进球数 lambda
        lambda_home = home_stats["attack"] * away_stats["defense"] * league_avg_goals
        lambda_away = away_stats["attack"] * home_stats["defense"] * league_avg_goals

        # 计算 0-6 球的比分矩阵
        prob_matrix = []
        for h in range(7):
            row = []
            for a in range(7):
                row.append(self.poisson_prob(lambda_home, h) * self.poisson_prob(lambda_away, a))
            prob_matrix.append(row)

        # 汇总胜平负概率
        win_prob = sum(prob_matrix[h][a] for h in range(7) for a in range(h))
        draw_prob = sum(prob_matrix[i][i] for i in range(7))
        loss_prob = sum(prob_matrix[h][a] for h in range(7) for a in range(h + 1, 7))

        # 寻找最可能比分
        max_val = -1
        best_score = "0-0"
        for h in range(7):
            for a in range(7):
                if prob_matrix[h][a] > max_val:
                    max_val = prob_matrix[h][a]
                    best_score = f"{h}-{a}"

        return {
            "win_prob": win_prob,
            "draw_prob": draw_prob,
            "loss_prob": loss_prob,
            "best_score": best_score,
            "lambda_home": lambda_home,
            "lambda_away": lambda_away,
            "prob_matrix": prob_matrix,
        }

    @staticmethod
    def analyze_value(model_prob, odds):
        """计算价值偏差 (Value Bet)"""
        if odds <= 0:
            return 0
        implied_prob = 1 / odds
        value = model_prob - implied_prob
        return value

    def get_top_scorelines(self, prob_matrix, top_n=5):
        """获取最可能的前 N 个比分"""
        scorelines = []
        for h in range(len(prob_matrix)):
            for a in range(len(prob_matrix[h])):
                scorelines.append((f"{h}-{a}", prob_matrix[h][a]))
        
        scorelines.sort(key=lambda x: x[1], reverse=True)
        return scorelines[:top_n]

