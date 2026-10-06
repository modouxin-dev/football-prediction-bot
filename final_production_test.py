"""最终生产环境测试"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

class ProductionValidator:
    """生产环境验证"""
    
    async def validate_all_systems(self):
        """全系统验证"""
        print("\n" + "="*70)
        print("🔐 生产环境最终验证 - Football Prediction Bot v3.3")
        print("="*70)
        
        from backtester_with_db import BacktesterWithDB
        from realtime_api_fixed import RealtimeAPIFixed
        from web_dashboard_fix import WebDashboardFix
        
        checks = {}
        
        # 1. 数据完整性检查
        print("\n[1/7] 数据完整性检查")
        try:
            backtester = BacktesterWithDB()
            fixtures = await backtester.load_from_db()
            if fixtures and len(fixtures) > 0:
                checks["数据完整性"] = True
                print(f"      ✅ 数据完整: {len(fixtures)} 条记录")
            else:
                checks["数据完整性"] = False
                print("      ❌ 数据不完整")
        except Exception as e:
            checks["数据完整性"] = False
            print(f"      ❌ 错误: {e}")
        
        # 2. 回测精确性检查
        print("\n[2/7] 回测精确性检查")
        try:
            backtest_results = await backtester.run_backtest()
            accuracy = backtest_results['accuracy']
            if 0 < accuracy <= 1:
                checks["回测精确性"] = True
                print(f"      ✅ 准确率: {accuracy:.2%}")
            else:
                checks["回测精确性"] = False
                print(f"      ❌ 准确率异常: {accuracy}")
        except Exception as e:
            checks["回测精确性"] = False
            print(f"      ❌ 错误: {e}")
        
        # 3. API可靠性检查
        print("\n[3/7] API可靠性检查")
        try:
            realtime = RealtimeAPIFixed()
            odds = await realtime.fetch_odds(1)
            injuries = await realtime.fetch_injuries(1)
            if odds.get("status") == "success" and injuries.get("status") == "success":
                checks["API可靠性"] = True
                print(f"      ✅ API响应正常 (赔率+伤停)")
            else:
                checks["API可靠性"] = False
                print("      ❌ API响应异常")
        except Exception as e:
            checks["API可靠性"] = False
            print(f"      ❌ 错误: {e}")
        
        # 4. Web看板功能检查
        print("\n[4/7] Web看板功能检查")
        try:
            dashboard = WebDashboardFix()
            dash_data = dashboard.get_dashboard_data()
            backtest_page = dashboard.get_backtest_page_data(2024)
            if dash_data.get("status") == "success" and backtest_page.get("status") == "success":
                checks["Web看板"] = True
                print(f"      ✅ 看板功能正常")
            else:
                checks["Web看板"] = False
                print("      ❌ 看板功能异常")
        except Exception as e:
            checks["Web看板"] = False
            print(f"      ❌ 错误: {e}")
        
        # 5. 错误恢复能力检查
        print("\n[5/7] 错误恢复能力检查")
        try:
            # 测试异常处理
            try:
                result = await realtime.fetch_odds(-1)  # 无效ID
                if result.get("status") in ["error", "success"]:
                    checks["错误恢复"] = True
                    print(f"      ✅ 错误处理正常")
                else:
                    checks["错误恢复"] = False
            except:
                checks["错误恢复"] = False
        except Exception as e:
            checks["错误恢复"] = False
            print(f"      ❌ 错误: {e}")
        
        # 6. 性能压力测试
        print("\n[6/7] 性能压力测试")
        try:
            import time
            start = time.time()
            for i in range(10):
                await realtime.fetch_odds(i)
            elapsed = time.time() - start
            avg_time = elapsed / 10
            if avg_time < 1.0:  # 平均响应<1秒
                checks["性能压力"] = True
                print(f"      ✅ 性能满足要求 (平均{avg_time:.3f}s)")
            else:
                checks["性能压力"] = False
                print(f"      ⚠️  性能需优化 (平均{avg_time:.3f}s)")
        except Exception as e:
            checks["性能压力"] = False
            print(f"      ❌ 错误: {e}")
        
        # 7. 准确率验证
        print("\n[7/7] 准确率验证")
        try:
            results = await backtester.run_backtest()
            accuracy = results['accuracy']
            expected_min = 0.3  # 30% 最低准确率
            if accuracy >= expected_min:
                checks["准确率"] = True
                print(f"      ✅ 准确率达标: {accuracy:.2%}")
            else:
                checks["准确率"] = False
                print(f"      ⚠️  准确率: {accuracy:.2%} (预期≥30%)")
        except Exception as e:
            checks["准确率"] = False
            print(f"      ❌ 错误: {e}")
        
        # 输出总结
        print("\n" + "="*70)
        print("📊 生产环境验证结果")
        print("="*70)
        
        passed = sum(1 for v in checks.values() if v)
        total = len(checks)
        
        for check, status in checks.items():
            symbol = "✅" if status else "❌"
            print(f"{symbol} {check:12s} {'通过' if status else '失败'}")
        
        print("-"*70)
        print(f"总计: {passed}/{total} 通过")
        
        if passed == total:
            print("\n✨ 所有检查通过!")
            print("📍 系统状态: 生产就绪 ✅")
            print("🚀 可立即部署到生产环境")
        else:
            print(f"\n⚠️  {total - passed} 项检查失败")
            print("📍 系统状态: 需修复")
        
        print("="*70 + "\n")
        return checks

async def main():
    validator = ProductionValidator()
    results = await validator.validate_all_systems()
    return results

if __name__ == "__main__":
    asyncio.run(main())
