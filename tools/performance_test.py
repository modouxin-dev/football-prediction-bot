#!/usr/bin/env python3
"""性能基准测试 - 验证系统可以满足生产要求
运行并发测试、延迟分析、资源监控
"""
from __future__ import annotations

import asyncio
import time
import logging
from statistics import mean, stdev, quantiles
from typing import Callable

import httpx

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class PerformanceTester:
    """性能测试工具"""

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.results: dict = {
            "response_times": [],
            "errors": 0,
            "successes": 0,
        }

    async def test_api_endpoint(
        self,
        endpoint: str,
        concurrency: int = 50,
        requests_per_user: int = 10,
        timeout: float = 10.0,
    ) -> dict:
        """并发 API 测试"""
        log.info(f"开始测试: {endpoint} (并发: {concurrency}, 请求数: {requests_per_user})")

        async def single_request():
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    start = time.time()
                    response = await client.get(f"{self.base_url}{endpoint}")
                    elapsed = time.time() - start

                    if response.status_code == 200:
                        self.results["successes"] += 1
                        self.results["response_times"].append(elapsed * 1000)  # ms
                        return {"success": True, "time": elapsed}
                    else:
                        self.results["errors"] += 1
                        return {"success": False, "status": response.status_code}
            except Exception as exc:
                self.results["errors"] += 1
                log.warning(f"请求失败: {exc}")
                return {"success": False, "error": str(exc)}

        # 创建并发任务
        tasks = [single_request() for _ in range(concurrency * requests_per_user)]

        # 执行所有任务
        start_time = time.time()
        results = await asyncio.gather(*tasks)
        total_time = time.time() - start_time

        # 分析结果
        times = self.results["response_times"]
        if times:
            analysis = {
                "endpoint": endpoint,
                "concurrency": concurrency,
                "total_requests": len(tasks),
                "successful": self.results["successes"],
                "failed": self.results["errors"],
                "success_rate": f"{self.results['successes'] / len(tasks):.2%}",
                "total_time_s": f"{total_time:.2f}",
                "requests_per_second": f"{len(tasks) / total_time:.2f}",
                "response_times_ms": {
                    "min": f"{min(times):.2f}",
                    "max": f"{max(times):.2f}",
                    "mean": f"{mean(times):.2f}",
                    "stdev": f"{stdev(times) if len(times) > 1 else 0:.2f}",
                    "p50": f"{sorted(times)[len(times)//2]:.2f}",
                    "p95": f"{quantiles(times, n=20)[18]:.2f}" if len(times) > 20 else "N/A",
                    "p99": f"{quantiles(times, n=100)[98]:.2f}" if len(times) > 100 else "N/A",
                }
            }
        else:
            analysis = {
                "endpoint": endpoint,
                "error": "No successful responses",
                "failed": self.results["errors"],
            }

        return analysis

    def print_results(self, analysis: dict) -> None:
        """打印测试结果"""
        log.info("=" * 60)
        log.info(f"测试结果: {analysis.get('endpoint', 'Unknown')}")
        log.info("=" * 60)

        if "error" in analysis:
            log.error(f"错误: {analysis['error']}")
            return

        log.info(f"总请求数: {analysis['total_requests']}")
        log.info(f"成功: {analysis['successful']}")
        log.info(f"失败: {analysis['failed']}")
        log.info(f"成功率: {analysis['success_rate']}")
        log.info(f"总耗时: {analysis['total_time_s']}s")
        log.info(f"吞吐量: {analysis['requests_per_second']} req/s")
        log.info("")
        log.info("响应时间统计 (ms):")
        for stat, value in analysis["response_times_ms"].items():
            log.info(f"  {stat:8s}: {value}")
        log.info("=" * 60)


async def run_performance_tests() -> None:
    """运行完整的性能测试套件"""
    tester = PerformanceTester()

    # 测试场景 1: 健康检查
    log.info("\n【测试场景 1】健康检查端点")
    result = await tester.test_api_endpoint("/health", concurrency=100, requests_per_user=10)
    tester.print_results(result)

    # 测试场景 2: 赛程列表
    tester.results = {"response_times": [], "errors": 0, "successes": 0}
    log.info("\n【测试场景 2】赛程列表端点")
    result = await tester.test_api_endpoint("/api/fixtures", concurrency=50, requests_per_user=5)
    tester.print_results(result)

    # 测试场景 3: 预测数据
    tester.results = {"response_times": [], "errors": 0, "successes": 0}
    log.info("\n【测试场景 3】预测数据端点")
    result = await tester.test_api_endpoint("/api/predictions/1", concurrency=50, requests_per_user=5)
    tester.print_results(result)

    # 测试场景 4: 积分榜
    tester.results = {"response_times": [], "errors": 0, "successes": 0}
    log.info("\n【测试场景 4】积分榜端点")
    result = await tester.test_api_endpoint("/api/standings/39", concurrency=30, requests_per_user=3)
    tester.print_results(result)

    log.info("\n✅ 所有性能测试完成!")


if __name__ == "__main__":
    asyncio.run(run_performance_tests())

