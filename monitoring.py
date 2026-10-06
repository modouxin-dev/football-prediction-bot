"""监控系统 - 实时性能监控、告警和日志
支持 Prometheus metrics、自定义指标、告警规则
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)


class MetricsCollector:
    """指标收集器"""

    def __init__(self):
        self.metrics: dict[str, Any] = {
            "memory_usage_mb": 0,
            "cpu_usage_percent": 0,
            "api_requests": 0,
            "api_errors": 0,
            "predictions_made": 0,
            "predictions_correct": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "response_times": [],
        }
        self.last_reset = datetime.now(timezone.utc)

    def record_api_request(self, success: bool = True, response_time: float = 0) -> None:
        """记录 API 请求"""
        self.metrics["api_requests"] += 1
        if not success:
            self.metrics["api_errors"] += 1
        if response_time > 0:
            self.metrics["response_times"].append(response_time)

    def record_prediction(self, is_correct: bool = False) -> None:
        """记录预测"""
        self.metrics["predictions_made"] += 1
        if is_correct:
            self.metrics["predictions_correct"] += 1

    def record_cache_hit(self, is_hit: bool) -> None:
        """记录缓存命中"""
        if is_hit:
            self.metrics["cache_hits"] += 1
        else:
            self.metrics["cache_misses"] += 1

    def get_accuracy(self) -> float:
        """获取预测准确率"""
        if self.metrics["predictions_made"] == 0:
            return 0.0
        return self.metrics["predictions_correct"] / self.metrics["predictions_made"]

    def get_error_rate(self) -> float:
        """获取 API 错误率"""
        if self.metrics["api_requests"] == 0:
            return 0.0
        return self.metrics["api_errors"] / self.metrics["api_requests"]

    def get_cache_hit_rate(self) -> float:
        """获取缓存命中率"""
        total = self.metrics["cache_hits"] + self.metrics["cache_misses"]
        if total == 0:
            return 0.0
        return self.metrics["cache_hits"] / total

    def get_avg_response_time(self) -> float:
        """获取平均响应时间(毫秒)"""
        if not self.metrics["response_times"]:
            return 0.0
        return sum(self.metrics["response_times"]) / len(self.metrics["response_times"]) * 1000

    def get_report(self) -> dict[str, Any]:
        """获取指标报告"""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api_requests": self.metrics["api_requests"],
            "api_errors": self.metrics["api_errors"],
            "error_rate": f"{self.get_error_rate():.2%}",
            "predictions_made": self.metrics["predictions_made"],
            "accuracy": f"{self.get_accuracy():.2%}",
            "cache_hit_rate": f"{self.get_cache_hit_rate():.2%}",
            "avg_response_time_ms": f"{self.get_avg_response_time():.0f}",
            "uptime_minutes": (datetime.now(timezone.utc) - self.last_reset).total_seconds() / 60,
        }

    def reset_daily(self) -> None:
        """每日重置指标"""
        self.metrics = {
            "memory_usage_mb": self.metrics.get("memory_usage_mb", 0),
            "cpu_usage_percent": self.metrics.get("cpu_usage_percent", 0),
            "api_requests": 0,
            "api_errors": 0,
            "predictions_made": 0,
            "predictions_correct": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "response_times": [],
        }
        self.last_reset = datetime.now(timezone.utc)


class AlertManager:
    """告警管理器"""

    def __init__(self):
        self.alerts: list[dict[str, Any]] = []
        self.rules: dict[str, Callable] = {
            "high_error_rate": lambda m: m.get_error_rate() > 0.1,  # 错误率 > 10%
            "low_accuracy": lambda m: m.get_accuracy() < 0.5,  # 准确率 < 50%
            "high_response_time": lambda m: m.get_avg_response_time() > 5000,  # 响应时间 > 5s
            "low_cache_hit": lambda m: m.get_cache_hit_rate() < 0.5,  # 缓存命中 < 50%
        }

    def check_alerts(self, collector: MetricsCollector) -> list[dict[str, Any]]:
        """检查告警规则"""
        triggered_alerts = []
        
        for rule_name, rule_func in self.rules.items():
            try:
                if rule_func(collector):
                    alert = {
                        "rule": rule_name,
                        "severity": "warning",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "message": self._get_alert_message(rule_name, collector),
                    }
                    triggered_alerts.append(alert)
                    log.warning(f"告警触发: {rule_name} - {alert['message']}")
            except Exception as exc:
                log.error(f"告警检查失败 ({rule_name}): {exc}")

        self.alerts.extend(triggered_alerts)
        return triggered_alerts

    @staticmethod
    def _get_alert_message(rule_name: str, collector: MetricsCollector) -> str:
        """生成告警消息"""
        messages = {
            "high_error_rate": f"API 错误率过高: {collector.get_error_rate():.2%}",
            "low_accuracy": f"预测准确率过低: {collector.get_accuracy():.2%}",
            "high_response_time": f"平均响应时间过长: {collector.get_avg_response_time():.0f}ms",
            "low_cache_hit": f"缓存命中率过低: {collector.get_cache_hit_rate():.2%}",
        }
        return messages.get(rule_name, f"未知告警: {rule_name}")

    def get_alerts(self, hours: int = 1) -> list[dict[str, Any]]:
        """获取指定时间内的告警"""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [
            a for a in self.alerts
            if datetime.fromisoformat(a["timestamp"]) > cutoff_time
        ]


class PerformanceMonitor:
    """性能监控器"""

    def __init__(self):
        self.collector = MetricsCollector()
        self.alert_manager = AlertManager()
        self.monitoring_active = False

    async def start_monitoring(self, interval: int = 60) -> None:
        """启动监控"""
        self.monitoring_active = True
        log.info(f"性能监控已启动 (间隔: {interval}s)")

        while self.monitoring_active:
            try:
                # 检查告警
                self.alert_manager.check_alerts(self.collector)

                # 记录日志
                report = self.collector.get_report()
                log.info(f"性能指标: {report}")

                # 每日重置 (UTC 0点)
                now = datetime.now(timezone.utc)
                if now.hour == 0 and now.minute == 0:
                    self.collector.reset_daily()
                    log.info("每日指标已重置")

                await asyncio.sleep(interval)
            except Exception as exc:
                log.error(f"监控错误: {exc}")
                await asyncio.sleep(interval)

    async def stop_monitoring(self) -> None:
        """停止监控"""
        self.monitoring_active = False
        log.info("性能监控已停止")

    def get_metrics(self) -> dict[str, Any]:
        """获取当前指标"""
        return self.collector.get_report()

    def get_alerts(self, hours: int = 1) -> list[dict[str, Any]]:
        """获取告警列表"""
        return self.alert_manager.get_alerts(hours)


# 全局监控实例
monitor = PerformanceMonitor()


async def start_monitoring_service() -> None:
    """启动监控服务"""
    await monitor.start_monitoring(interval=60)


async def stop_monitoring_service() -> None:
    """停止监控服务"""
    await monitor.stop_monitoring()

