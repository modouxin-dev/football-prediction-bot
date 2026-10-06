"""最终系统集成测试"""
import asyncio

async def run_final_test():
    """运行最终集成测试"""
    print("\n" + "="*70)
    print("🚀 最终系统集成测试 - Football Prediction Bot v3.3")
    print("="*70)
    
    # 导入所有模块
    from backtester_with_db import BacktesterWithDB
    from data_analysis import DataAnalyzer
    from realtime_api_fixed import RealtimeAPIFixed
    from web_dashboard_fix import WebDashboardFix
    from feature_extractor import FeatureExtractor
    
    results = {
        "回测系统": False,
        "数据分析": False,
        "实时API": False,
        "Web看板": False,
        "特征工程": False,
    }
    
    try:
        # 1. 回测系统
        print("\n[1/5] 回测系统验证")
        backtester = BacktesterWithDB(season=2024)
        backtest_result = await backtester.run_backtest()
        print(f"      ✅ 准确率: {backtest_result['accuracy']:.2%}")
        results["回测系统"] = True
        
        # 2. 数据分析
        print("\n[2/5] 数据分析验证")
        analyzer = DataAnalyzer()
        analysis = await analyzer.analyze_team_form([])
        print(f"      ✅ 分析完成")
        results["数据分析"] = True
        
        # 3. 实时API
        print("\n[3/5] 实时API验证")
        realtime = RealtimeAPIFixed()
        odds = await realtime.fetch_odds(1)
        print(f"      ✅ 赔率: home={odds['home_odds']}")
        results["实时API"] = True
        
        # 4. Web看板
        print("\n[4/5] Web看板验证")
        dashboard = WebDashboardFix()
        dash_data = dashboard.get_dashboard_data()
        print(f"      ✅ 预测数: {dash_data['data']['current_predictions']}")
        results["Web看板"] = True
        
        # 5. 特征工程
        print("\n[5/5] 特征工程验证")
        extractor = FeatureExtractor()
        features = extractor.extract_features("A", "B")
        print(f"      ✅ 特征维: {len(features)}")
        results["特征工程"] = True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return results
    
    # 输出总结
    print("\n" + "="*70)
    print("📊 最终测试结果")
    print("="*70)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for component, status in results.items():
        symbol = "✅" if status else "❌"
        print(f"{symbol} {component:12s} {'通过' if status else '失败'}")
    
    print("-"*70)
    print(f"总计: {passed}/{total} 通过")
    
    if passed == total:
        print("\n✨ 所有组件通过验证!")
        print("📍 系统状态: 就绪 ✅")
        print("🚀 可进行版本发布")
    else:
        print("\n⚠️  部分组件失败")
        print("📍 系统状态: 需修复")
    
    print("="*70 + "\n")
    return results

if __name__ == "__main__":
    asyncio.run(run_final_test())
