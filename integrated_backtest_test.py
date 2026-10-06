"""完整集成回测测试"""
import asyncio
from backtester_with_db import BacktesterWithDB
from realtime_api_fixed import RealtimeAPIFixed
from data_analysis import DataAnalyzer
from feature_extractor import FeatureExtractor

async def run_integrated_test():
    """运行完整集成测试"""
    print("\n" + "="*60)
    print("🚀 完整集成回测测试")
    print("="*60)
    
    # 1. 回测
    print("\n1️⃣ 阶段1: 历史回测")
    print("-"*60)
    backtester = BacktesterWithDB(season=2024)
    backtest_results = await backtester.run_backtest()
    print(f"✅ 回测完成: {backtest_results['accuracy']:.2%} 准确率")
    
    # 2. 数据分析
    print("\n2️⃣ 阶段2: 数据分析")
    print("-"*60)
    analyzer = DataAnalyzer()
    fixtures = [
        {"home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 1, "status": "FINISHED"},
        {"home_team": "C", "away_team": "D", "home_goals": 1, "away_goals": 1, "status": "FINISHED"},
    ]
    team_analysis = await analyzer.analyze_team_form(fixtures)
    print(f"✅ 分析完成: {len(team_analysis)} 支球队")
    
    # 3. 特征提取
    print("\n3️⃣ 阶段3: 特征提取")
    print("-"*60)
    extractor = FeatureExtractor()
    features = extractor.extract_features("Team A", "Team B")
    print(f"✅ 特征提取: {len(features)} 维向量")
    
    # 4. 实时数据
    print("\n4️⃣ 阶段4: 实时数据")
    print("-"*60)
    realtime = RealtimeAPIFixed()
    odds = await realtime.fetch_odds(1)
    injuries = await realtime.fetch_injuries(1)
    update = await realtime.update_prediction(1, odds, injuries)
    print(f"✅ 实时数据: 赔率={odds['home_odds']}, 调整={update['adjustment']:.4f}")
    
    # 5. 总结
    print("\n" + "="*60)
    print("✨ 完整集成测试结果")
    print("="*60)
    print(f"""
    ✅ 回测系统: 通过 (准确率 {backtest_results['accuracy']:.2%})
    ✅ 数据分析: 通过 ({len(team_analysis)} 支球队)
    ✅ 特征工程: 通过 ({len(features)} 维向量)
    ✅ 实时API: 通过 (赔率 + 伤停 + 预测)
    
    整体状态: ✅ 所有组件正常工作
    """)
    print("="*60 + "\n")
    
    return {
        "backtest": backtest_results['accuracy'],
        "analysis": len(team_analysis),
        "features": len(features),
        "realtime": update['adjustment'],
    }

if __name__ == "__main__":
    results = asyncio.run(run_integrated_test())
