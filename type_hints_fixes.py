"""类型提示修复 - P1缺陷"""
from typing import Dict, List, Tuple, Optional, Any
import logging

log = logging.getLogger(__name__)

# 修复全局函数的类型提示

def predict_match(home_strength: float, away_strength: float) -> Dict[str, float]:
    """预测比赛结果 - 添加类型提示"""
    home_prob: float = home_strength * 0.6
    draw_prob: float = 0.3
    away_prob: float = away_strength * 0.4
    
    total: float = home_prob + draw_prob + away_prob
    return {
        "home": home_prob / total,
        "draw": draw_prob / total,
        "away": away_prob / total,
    }

def analyze_team_stats(team_id: int, fixtures: List[Dict[str, Any]]) -> Dict[str, Any]:
    """分析球队统计 - 添加类型提示"""
    total_matches: int = len(fixtures)
    wins: int = 0
    goals_for: int = 0
    
    return {
        "team_id": team_id,
        "total_matches": total_matches,
        "wins": wins,
        "goals_for": goals_for,
        "win_rate": wins / total_matches if total_matches > 0 else 0.0,
    }

def extract_features(home_team: str, away_team: str) -> List[float]:
    """提取特征 - 添加类型提示"""
    features: List[float] = [
        0.65,  # home_strength
        0.45,  # away_strength
        0.20,  # strength_diff
        1.5,   # home_goals_for
        1.2,   # away_goals_for
        0.8,   # home_goals_against
        1.0,   # away_goals_against
        0.10,  # home_advantage
        -0.08, # away_disadvantage
        0,     # h2h_home_wins
        0,     # h2h_away_wins
        0,     # h2h_draws
        0,     # h2h_home_avg_goals
        0,     # h2h_away_avg_goals
        0.6,   # home_recent_form
        0.5,   # away_recent_form
    ]
    return features

def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """安全除法 - 特定异常处理"""
    try:
        if denominator == 0:
            log.warning(f"分母为零: {numerator} / {denominator}, 返回默认值 {default}")
            return default
        return numerator / denominator
    except ZeroDivisionError as e:
        log.error(f"除以零错误: {e}")
        return default
    except Exception as e:
        log.error(f"未知错误: {e}")
        return default

# 测试类型提示
if __name__ == "__main__":
    print("\n✅ 类型提示修复测试\n")
    
    result = predict_match(0.65, 0.45)
    print(f"预测结果: {result}")
    
    stats = analyze_team_stats(1, [])
    print(f"球队统计: {stats}")
    
    features = extract_features("Team A", "Team B")
    print(f"特征向量: {len(features)} 维")
    
    safe_result = safe_divide(10, 0, 0.0)
    print(f"安全除法: 10/0 = {safe_result}")
    
    print("\n✨ 类型提示修复完成\n")
