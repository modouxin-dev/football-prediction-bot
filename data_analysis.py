"""数据分析和挖掘 - 从缓存数据提取有用特征
统计历史规律、分析特殊关系、计算球队周期性
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)


class DataAnalyzer:
    """数据分析引擎"""

    def __init__(self):
        self.team_stats: dict[str, Any] = {}
        self.h2h_records: dict[str, Any] = {}
        self.seasonal_trends: dict[str, Any] = {}

    async def analyze_team_form(self, fixtures: list[dict]) -> dict:
        """分析球队形态"""
        log.info("分析球队形态...")
        
        team_records = defaultdict(lambda: {
            "played": 0,
            "won": 0,
            "drawn": 0,
            "lost": 0,
            "goals_for": 0,
            "goals_against": 0,
            "recent_form": [],  # 最近5场结果
            "goal_streak": 0,  # 连续进球场数
        })
        
        # 统计每支球队的成绩
        for fixture in fixtures:
            if fixture.get("status") != "FINISHED":
                continue
            
            home = fixture.get("home_team", "")
            away = fixture.get("away_team", "")
            home_goals = fixture.get("home_goals", 0)
            away_goals = fixture.get("away_goals", 0)
            
            # 主队统计
            team_records[home]["played"] += 1
            team_records[home]["goals_for"] += home_goals
            team_records[home]["goals_against"] += away_goals
            
            if home_goals > away_goals:
                team_records[home]["won"] += 1
                team_records[home]["recent_form"].append("W")
            elif home_goals == away_goals:
                team_records[home]["drawn"] += 1
                team_records[home]["recent_form"].append("D")
            else:
                team_records[home]["lost"] += 1
                team_records[home]["recent_form"].append("L")
            
            # 客队统计
            team_records[away]["played"] += 1
            team_records[away]["goals_for"] += away_goals
            team_records[away]["goals_against"] += home_goals
            
            if away_goals > home_goals:
                team_records[away]["won"] += 1
                team_records[away]["recent_form"].append("W")
            elif away_goals == home_goals:
                team_records[away]["drawn"] += 1
                team_records[away]["recent_form"].append("D")
            else:
                team_records[away]["lost"] += 1
                team_records[away]["recent_form"].append("L")
        
        # 清理最近5场数据
        for team in team_records:
            team_records[team]["recent_form"] = team_records[team]["recent_form"][-5:]
        
        self.team_stats = dict(team_records)
        return dict(team_records)

    def analyze_h2h(self, fixtures: list[dict]) -> dict:
        """历史交锋分析"""
        log.info("分析历史交锋...")
        
        h2h_records = defaultdict(lambda: {
            "matches": 0,
            "team_a_wins": 0,
            "team_b_wins": 0,
            "draws": 0,
            "avg_goals_a": 0.0,
            "avg_goals_b": 0.0,
        })
        
        # 统计交锋记录
        for fixture in fixtures:
            if fixture.get("status") != "FINISHED":
                continue
            
            home = fixture.get("home_team", "")
            away = fixture.get("away_team", "")
            
            # 创建规范化的对阵记录
            pair = tuple(sorted([home, away]))
            
            home_goals = fixture.get("home_goals", 0)
            away_goals = fixture.get("away_goals", 0)
            
            h2h = h2h_records[pair]
            h2h["matches"] += 1
            h2h["avg_goals_a"] = (h2h["avg_goals_a"] * (h2h["matches"] - 1) + 
                                  (home_goals if home == pair[0] else away_goals)) / h2h["matches"]
            h2h["avg_goals_b"] = (h2h["avg_goals_b"] * (h2h["matches"] - 1) + 
                                  (away_goals if away == pair[1] else home_goals)) / h2h["matches"]
            
            if home_goals > away_goals:
                if home == pair[0]:
                    h2h["team_a_wins"] += 1
                else:
                    h2h["team_b_wins"] += 1
            elif home_goals == away_goals:
                h2h["draws"] += 1
            else:
                if away == pair[0]:
                    h2h["team_a_wins"] += 1
                else:
                    h2h["team_b_wins"] += 1
        
        self.h2h_records = dict(h2h_records)
        return dict(h2h_records)

    def calculate_team_strength(self) -> dict[str, float]:
        """计算球队实力指数"""
        log.info("计算球队实力...")
        
        strengths = {}
        
        for team, stats in self.team_stats.items():
            if stats["played"] == 0:
                strengths[team] = 0.0
                continue
            
            # 简单实力指数:  (胜场*3 + 平场) / 总场数
            points = stats["won"] * 3 + stats["drawn"]
            strength = points / stats["played"]
            
            # 再加入进球差
            goal_diff = (stats["goals_for"] - stats["goals_against"]) / max(stats["played"], 1)
            strength = (strength + goal_diff) / 2
            
            strengths[team] = round(strength, 2)
        
        return strengths

    def analyze_seasonal_trend(self, fixtures: list[dict]) -> dict:
        """赛季阶段性表现分析"""
        log.info("分析赛季阶段性表现...")
        
        # 按月份统计
        monthly_stats = defaultdict(lambda: {
            "matches": 0,
            "avg_goals": 0.0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
        })
        
        for fixture in fixtures:
            if fixture.get("status") != "FINISHED":
                continue
            
            # 解析日期
            date_str = fixture.get("date", "")
            try:
                month = int(date_str.split("-")[1]) if "-" in date_str else 1
            except (ValueError, IndexError):
                month = 1
            
            home_goals = fixture.get("home_goals", 0)
            away_goals = fixture.get("away_goals", 0)
            total_goals = home_goals + away_goals
            
            stats = monthly_stats[month]
            stats["matches"] += 1
            stats["avg_goals"] = (stats["avg_goals"] * (stats["matches"] - 1) + total_goals) / stats["matches"]
            
            if home_goals > away_goals:
                stats["wins"] += 1
            elif home_goals == away_goals:
                stats["draws"] += 1
            else:
                stats["losses"] += 1
        
        self.seasonal_trends = dict(monthly_stats)
        return dict(monthly_stats)

    def get_summary(self) -> dict:
        """获取分析总结"""
        strengths = self.calculate_team_strength()
        
        return {
            "teams_analyzed": len(self.team_stats),
            "h2h_records": len(self.h2h_records),
            "top_teams": sorted(strengths.items(), key=lambda x: x[1], reverse=True)[:10],
            "seasonal_stats": self.seasonal_trends,
        }


async def run_analysis() -> None:
    """运行数据分析示例"""
    analyzer = DataAnalyzer()
    
    # 示例数据
    fixtures = [
        {
            "id": 1,
            "home_team": "Team A",
            "away_team": "Team B",
            "home_goals": 2,
            "away_goals": 1,
            "status": "FINISHED",
            "date": "2024-01-15"
        }
    ]
    
    # 运行分析
    await analyzer.analyze_team_form(fixtures)
    analyzer.analyze_h2h(fixtures)
    analyzer.analyze_seasonal_trend(fixtures)
    
    print("\n" + "="*80)
    print("📊 数据分析结果")
    print("="*80)
    
    summary = analyzer.get_summary()
    print(f"\n分析的球队数: {summary['teams_analyzed']}")
    print(f"历史交锋记录: {summary['h2h_records']} 对")
    
    print("\n🏆 球队实力排名 (TOP 10):")
    for team, strength in summary['top_teams']:
        print(f"  {team}: {strength:.2f}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_analysis())

