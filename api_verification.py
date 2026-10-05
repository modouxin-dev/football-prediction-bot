"""API端点验证"""
import asyncio
from backtester_with_db import BacktesterWithDB
from data_analysis import DataAnalyzer

async def verify_apis():
    """验证所有API端点"""
    print("\n🔍 API 端点验证\n")
    
    # 1. 回测API验证
    print("1️⃣ /api/backtest 验证")
    try:
        backtester = BacktesterWithDB(season=2024)
        results = await backtester.run_backtest()
        print(f"   ✅ 回测成功: {results['accuracy']:.2%} 准确率")
    except Exception as e:
        print(f"   ❌ 回测失败: {e}")
    
    # 2. 数据分析API验证
    print("\n2️⃣ /api/analysis/team-form 验证")
    try:
        analyzer = DataAnalyzer()
        fixtures = [
            {"home_team": "Team A", "away_team": "Team B", 
             "home_goals": 2, "away_goals": 1, "status": "FINISHED"},
        ]
        analysis = await analyzer.analyze_team_form(fixtures)
        print(f"   ✅ 分析成功: {len(analysis)} 支球队")
    except Exception as e:
        print(f"   ❌ 分析失败: {e}")
    
    # 3. 特征提取API验证
    print("\n3️⃣ /api/features 验证")
    try:
        from feature_extractor import FeatureExtractor, create_feature_vector
        vector = create_feature_vector("Team A", "Team B", {}, {})
        print(f"   ✅ 特征提取成功: {len(vector)} 维特征向量")
    except Exception as e:
        print(f"   ❌ 特征提取失败: {e}")
    
    print("\n✨ API 验证完成\n")

if __name__ == "__main__":
    asyncio.run(verify_apis())
