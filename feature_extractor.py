"""特征工程 - 为机器学习模型提取有用特征
从历史数据提取特征,用于改进预测准确率
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class FeatureExtractor:
    """特征提取引擎"""

    def __init__(self, team_stats: dict = None, h2h_records: dict = None):
        self.team_stats = team_stats or {}
        self.h2h_records = h2h_records or {}
        self.features: dict[str, Any] = {}

    def extract_features(self, home_team: str, away_team: str) -> dict:
        """为单场比赛提取特征"""
        features = {}
        
        # 1. 球队实力特征
        features["home_strength"] = self._get_team_strength(home_team)
        features["away_strength"] = self._get_team_strength(away_team)
        features["strength_diff"] = features["home_strength"] - features["away_strength"]
        
        # 2. 进球能力特征
        features["home_goals_for"] = self._get_avg_goals_for(home_team)
        features["away_goals_for"] = self._get_avg_goals_for(away_team)
        features["home_goals_against"] = self._get_avg_goals_against(home_team)
        features["away_goals_against"] = self._get_avg_goals_against(away_team)
        
        # 3. 主客优势特征
        features["home_advantage"] = self._calculate_home_advantage(home_team)
        features["away_disadvantage"] = self._calculate_away_disadvantage(away_team)
        
        # 4. 历史交锋特征
        h2h = self._get_h2h_stats(home_team, away_team)
        features["h2h_home_wins"] = h2h.get("home_wins", 0)
        features["h2h_away_wins"] = h2h.get("away_wins", 0)
        features["h2h_draws"] = h2h.get("draws", 0)
        features["h2h_home_avg_goals"] = h2h.get("home_avg_goals", 0.0)
        features["h2h_away_avg_goals"] = h2h.get("away_avg_goals", 0.0)
        
        # 5. 形态特征
        features["home_recent_form"] = self._calculate_recent_form(home_team)
        features["away_recent_form"] = self._calculate_recent_form(away_team)
        
        self.features = features
        return features

    def _get_team_strength(self, team: str) -> float:
        """获取球队实力指数"""
        if team not in self.team_stats:
            return 0.0
        
        stats = self.team_stats[team]
        if stats.get("played", 0) == 0:
            return 0.0
        
        points = stats.get("won", 0) * 3 + stats.get("drawn", 0)
        return points / stats.get("played", 1)

    def _get_avg_goals_for(self, team: str) -> float:
        """获取平均进球数"""
        if team not in self.team_stats:
            return 0.0
        
        stats = self.team_stats[team]
        played = stats.get("played", 0)
        if played == 0:
            return 0.0
        
        return stats.get("goals_for", 0) / played

    def _get_avg_goals_against(self, team: str) -> float:
        """获取平均失球数"""
        if team not in self.team_stats:
            return 0.0
        
        stats = self.team_stats[team]
        played = stats.get("played", 0)
        if played == 0:
            return 0.0
        
        return stats.get("goals_against", 0) / played

    def _calculate_home_advantage(self, team: str) -> float:
        """计算主场优势"""
        # 简化版:主场相比客场多赢的百分比
        if team not in self.team_stats:
            return 0.0
        
        # 实际应该分别统计主客场,这里简化处理
        return 0.1  # 占位值,代表主队通常有10%的优势

    def _calculate_away_disadvantage(self, team: str) -> float:
        """计算客场劣势"""
        return -0.08  # 占位值,客队通常有8%的劣势

    def _get_h2h_stats(self, home_team: str, away_team: str) -> dict:
        """获取历史交锋统计"""
        pair = tuple(sorted([home_team, away_team]))
        
        if pair not in self.h2h_records:
            return {
                "home_wins": 0,
                "away_wins": 0,
                "draws": 0,
                "home_avg_goals": 0.0,
                "away_avg_goals": 0.0,
            }
        
        h2h = self.h2h_records[pair]
        
        # 确定主客队的胜负
        if home_team == pair[0]:
            home_wins = h2h.get("team_a_wins", 0)
            away_wins = h2h.get("team_b_wins", 0)
            home_avg = h2h.get("avg_goals_a", 0.0)
            away_avg = h2h.get("avg_goals_b", 0.0)
        else:
            home_wins = h2h.get("team_b_wins", 0)
            away_wins = h2h.get("team_a_wins", 0)
            home_avg = h2h.get("avg_goals_b", 0.0)
            away_avg = h2h.get("avg_goals_a", 0.0)
        
        return {
            "home_wins": home_wins,
            "away_wins": away_wins,
            "draws": h2h.get("draws", 0),
            "home_avg_goals": home_avg,
            "away_avg_goals": away_avg,
        }

    def _calculate_recent_form(self, team: str) -> float:
        """计算最近形态 (0-1 范围)"""
        if team not in self.team_stats:
            return 0.5
        
        recent = self.team_stats[team].get("recent_form", [])
        if not recent:
            return 0.5
        
        # 最近5场:胜=1, 平=0.5, 负=0
        form_score = 0.0
        for result in recent[-5:]:
            if result == "W":
                form_score += 1.0
            elif result == "D":
                form_score += 0.5
        
        return form_score / min(5, len(recent))

    def get_feature_importance(self) -> dict[str, float]:
        """获取特征重要性 (示例)"""
        # 实际应该从训练的模型中获取,这里是占位符
        return {
            "strength_diff": 0.25,
            "home_recent_form": 0.15,
            "away_recent_form": 0.15,
            "h2h_home_wins": 0.10,
            "home_advantage": 0.10,
            "home_goals_for": 0.08,
            "away_goals_against": 0.07,
            "other": 0.10,
        }

    def get_feature_summary(self) -> dict:
        """获取特征总结"""
        return {
            "features_extracted": len(self.features),
            "feature_names": list(self.features.keys()),
            "feature_values": self.features,
            "feature_importance": self.get_feature_importance(),
        }


def create_feature_vector(home_team: str, away_team: str, 
                         team_stats: dict, h2h_records: dict) -> list[float]:
    """创建特征向量用于机器学习"""
    extractor = FeatureExtractor(team_stats, h2h_records)
    features = extractor.extract_features(home_team, away_team)
    
    # 按顺序构建特征向量
    vector = [
        features.get("home_strength", 0.0),
        features.get("away_strength", 0.0),
        features.get("strength_diff", 0.0),
        features.get("home_goals_for", 0.0),
        features.get("away_goals_for", 0.0),
        features.get("home_goals_against", 0.0),
        features.get("away_goals_against", 0.0),
        features.get("home_advantage", 0.0),
        features.get("away_disadvantage", 0.0),
        features.get("h2h_home_wins", 0.0),
        features.get("h2h_away_wins", 0.0),
        features.get("h2h_draws", 0.0),
        features.get("h2h_home_avg_goals", 0.0),
        features.get("h2h_away_avg_goals", 0.0),
        features.get("home_recent_form", 0.5),
        features.get("away_recent_form", 0.5),
    ]
    
    return vector


if __name__ == "__main__":
    # 测试特征提取
    extractor = FeatureExtractor({
        "Team A": {
            "played": 10,
            "won": 7,
            "drawn": 2,
            "lost": 1,
            "goals_for": 20,
            "goals_against": 8,
            "recent_form": ["W", "W", "D", "W", "L"],
        },
        "Team B": {
            "played": 10,
            "won": 5,
            "drawn": 3,
            "lost": 2,
            "goals_for": 15,
            "goals_against": 10,
            "recent_form": ["W", "D", "L", "W", "W"],
        }
    })
    
    features = extractor.extract_features("Team A", "Team B")
    
    print("\n" + "="*80)
    print("🔍 特征提取结果")
    print("="*80)
    
    for feature_name, value in features.items():
        print(f"{feature_name:30s}: {value:10.4f}")
    
    print("\n📊 特征重要性:")
    for feature, importance in extractor.get_feature_importance().items():
        print(f"{feature:30s}: {importance:6.2%}")

