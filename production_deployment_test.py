"""生产部署完整测试"""
import asyncio
import time
import logging
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

class ProductionDeploymentTest:
    """生产部署完整测试"""
    
    async def stress_test_concurrent(self, num_concurrent: int = 100):
        """并发压力测试"""
        print(f"\n⚡ 并发压力测试 ({num_concurrent} 并发)")
        
        from realtime_api_fixed import RealtimeAPIFixed
        api = RealtimeAPIFixed()
        
        start = time.time()
        tasks = []
        
        for i in range(num_concurrent):
            tasks.append(api.fetch_odds(i % 10))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.time() - start
        
        success = sum(1 for r in results if isinstance(r, dict) and r.get('status') == 'success')
        
        print(f"  总请求: {num_concurrent}")
        print(f"  成功: {success}")
        print(f"  总耗时: {elapsed:.3f}s")
        print(f"  吞吐量: {num_concurrent/elapsed:.0f} req/s")
        
        return {
            "total": num_concurrent,
            "success": success,
            "time": elapsed,
            "throughput": num_concurrent/elapsed,
        }
    
    async def endurance_test(self, duration: int = 10, rate: int = 10):
        """耐久性测试"""
        print(f"\n⏱️  耐久性测试 ({duration}秒, {rate}req/s)")
        
        from backtester_with_db import BacktesterWithDB
        backtester = BacktesterWithDB()
        
        start = time.time()
        count = 0
        errors = 0
        
        while time.time() - start < duration:
            try:
                await backtester.run_backtest()
                count += 1
            except Exception as e:
                errors += 1
                log.error(f"回测失败: {e}")
            
            await asyncio.sleep(1.0 / rate)
        
        elapsed = time.time() - start
        
        print(f"  总请求: {count}")
        print(f"  成功: {count - errors}")
        print(f"  失败: {errors}")
        print(f"  耗时: {elapsed:.1f}s")
        print(f"  成功率: {(count-errors)/max(count,1)*100:.1f}%")
        
        return {
            "total": count,
            "success": count - errors,
            "errors": errors,
            "time": elapsed,
            "success_rate": (count-errors)/max(count,1),
        }
    
    async def memory_stability_test(self, iterations: int = 50):
        """内存稳定性测试"""
        print(f"\n💾 内存稳定性测试 ({iterations} 次迭代)")
        
        import sys
        from backtester_with_db import BacktesterWithDB
        
        backtester = BacktesterWithDB()
        
        sizes = []
        for i in range(iterations):
            # 获取对象大小
            result = await backtester.run_backtest()
            size = sys.getsizeof(result)
            sizes.append(size)
        
        avg_size = sum(sizes) / len(sizes)
        max_size = max(sizes)
        min_size = min(sizes)
        
        print(f"  平均大小: {avg_size:.0f} bytes")
        print(f"  最大大小: {max_size} bytes")
        print(f"  最小大小: {min_size} bytes")
        print(f"  波动: {(max_size-min_size)/avg_size*100:.1f}%")
        
        # 检查是否有内存泄漏迹象
        if (max_size - min_size) / avg_size > 0.2:
            print(f"  ⚠️  可能存在内存波动")
        else:
            print(f"  ✅ 内存稳定")
        
        return {
            "avg_size": avg_size,
            "max_size": max_size,
            "min_size": min_size,
            "stability": (max_size - min_size) / avg_size < 0.2,
        }
    
    async def accuracy_comprehensive_test(self, seasons: list = [2024, 2023]):
        """准确率综合验证"""
        print(f"\n📊 准确率综合验证 ({len(seasons)} 赛季)")
        
        from backtester_with_db import BacktesterWithDB
        
        results = {}
        for season in seasons:
            backtester = BacktesterWithDB(season=season)
            backtest_result = await backtester.run_backtest()
            accuracy = backtest_result['accuracy']
            results[season] = accuracy
            print(f"  {season}赛季: {accuracy:.2%}")
        
        avg_accuracy = sum(results.values()) / len(results)
        print(f"  平均准确率: {avg_accuracy:.2%}")
        
        return results
    
    async def run_all_tests(self):
        """运行所有测试"""
        print("\n" + "="*70)
        print("🚀 生产部署完整测试套件")
        print("="*70)
        
        test_results = {}
        
        # 1. 并发测试
        try:
            test_results["并发测试"] = await self.stress_test_concurrent(100)
        except Exception as e:
            log.error(f"并发测试失败: {e}")
            test_results["并发测试"] = {"error": str(e)}
        
        # 2. 耐久性测试
        try:
            test_results["耐久性测试"] = await self.endurance_test(10, 5)
        except Exception as e:
            log.error(f"耐久性测试失败: {e}")
            test_results["耐久性测试"] = {"error": str(e)}
        
        # 3. 内存稳定性
        try:
            test_results["内存稳定性"] = await self.memory_stability_test(50)
        except Exception as e:
            log.error(f"内存测试失败: {e}")
            test_results["内存稳定性"] = {"error": str(e)}
        
        # 4. 准确率验证
        try:
            test_results["准确率验证"] = await self.accuracy_comprehensive_test([2024])
        except Exception as e:
            log.error(f"准确率测试失败: {e}")
            test_results["准确率验证"] = {"error": str(e)}
        
        # 总结
        print("\n" + "="*70)
        print("📋 测试总结")
        print("="*70)
        print(f"✅ 并发吞吐: {test_results.get('并发测试', {}).get('throughput', 0):.0f} req/s")
        print(f"✅ 耐久成功率: {test_results.get('耐久性测试', {}).get('success_rate', 0)*100:.1f}%")
        print(f"✅ 内存稳定: {test_results.get('内存稳定性', {}).get('stability', False)}")
        print(f"✅ 准确率: {list(test_results.get('准确率验证', {}).values())[0]*100:.2f}%")
        print("="*70)
        
        return test_results

async def main():
    tester = ProductionDeploymentTest()
    results = await tester.run_all_tests()
    return results

if __name__ == "__main__":
    asyncio.run(main())
