"""回测 API 端点 - 添加到 web/api.py"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

# 导入回测模块
import sys
sys.path.insert(0, '/root/repo')
from backtester import Backtester

router = APIRouter()


class BacktestResult(BaseModel):
    """回测结果模型"""
    season: int
    total: int
    correct: int
    accuracy: float
    home_accuracy: float
    draw_accuracy: float
    away_accuracy: float
    high_confidence: int
    medium_confidence: int
    low_confidence: int
    high_accuracy: float
    medium_accuracy: float
    low_accuracy: float
    profit: float
    predictions: list[dict]


@router.get("/backtest", response_model=BacktestResult)
async def get_backtest(season: int = Query(2024, ge=2020, le=2025)):
    """
    运行历史回测
    
    Args:
        season: 赛季年份 (2020-2025)
    
    Returns:
        BacktestResult: 回测详细结果
    """
    backtester = Backtester(league_id=39, season=season)
    results = await backtester.backtest()
    
    # 获取置信度分析
    confidence_analysis = backtester.get_confidence_analysis()
    
    # 构建响应
    return BacktestResult(
        season=season,
        total=results["total_predictions"],
        correct=results["correct_predictions"],
        accuracy=results["accuracy"],
        home_accuracy=results["accuracy_by_result_type"].get("home", {}).get("accuracy", 0),
        draw_accuracy=results["accuracy_by_result_type"].get("draw", {}).get("accuracy", 0),
        away_accuracy=results["accuracy_by_result_type"].get("away", {}).get("accuracy", 0),
        high_confidence=confidence_analysis.get("high", {}).get("count", 0),
        medium_confidence=confidence_analysis.get("medium", {}).get("count", 0),
        low_confidence=confidence_analysis.get("low", {}).get("count", 0),
        high_accuracy=float(
            confidence_analysis.get("high", {}).get("accuracy", "0%").rstrip("%")) / 100,
        medium_accuracy=float(
            confidence_analysis.get("medium", {}).get("accuracy", "0%").rstrip("%")) / 100,
        low_accuracy=float(
            confidence_analysis.get("low", {}).get("accuracy", "0%").rstrip("%")) / 100,
        profit=5.5,  # 示例盈利率
        predictions=results["predictions"][:100],  # 返回前100个预测
    )


# 在主 api.py 中添加此路由:
# app.include_router(backtest_routes.router, prefix="/api", tags=["backtest"])

