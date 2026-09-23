import math

class MatchAnalyzer:
    @staticmethod
    def poisson_prob(lmbda, x):
        """泊松分布计算公式"""
        return (math.exp(-lmbda) * (lmbda**x)) / math.factorial(x)

    def calculate_prediction(self, home_stats, away_stats, league_avg_goals=1.35):
        """
        核心量化分析：
        home_stats: {'attack_strength': 1.2, 'defense_strength': 0.8}
        """
        # 计算预期进球 lambda
        lambda_home = home_stats['attack'] * away_stats['defense'] * league_avg_goals
        lambda_away = away_stats['attack'] * home_stats['defense'] * league_avg_goals

        # 计算 0-5 球的比分矩阵
        prob_matrix = []
        for h in range(6):
            row = []
            for a in range(6):
                row.append(self.poisson_prob(lambda_home, h) * self.poisson_prob(lambda_away, a))
            prob_matrix.append(row)

        # 汇总胜平负概率
        win_prob = sum(prob_matrix[h][a] for h in range(6) for a in range(h))
        draw_prob = sum(prob_matrix[i][i] for i in range(6))
        loss_prob = sum(prob_matrix[h][a] for h in range(6) for a in range(h+1, 6))
        
        # 寻找最可能比分
        max_val = -1
        best_score = "0-0"
        for h in range(6):
            for a in range(6):
                if prob_matrix[h][a] > max_val:
                    max_val = prob_matrix[h][a]
                    best_score = f"{h}-{a}"

        return {
            "win_prob": win_prob,
            "draw_prob": draw_prob,
            "loss_prob": loss_prob,
            "best_score": best_score,
            "lambda_home": lambda_home,
            "lambda_away": lambda_away
        }

    def analyze_value(self, model_prob, odds):
        """计算价值偏差 (Value Bet)"""
        implied_prob = 1 / odds
        value = model_prob - implied_prob
        return value
