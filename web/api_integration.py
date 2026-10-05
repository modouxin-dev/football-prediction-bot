"""回测API集成到 web/api.py"""
from fastapi import APIRouter, Query
from backtester import Backtester
from data_analysis import DataAnalyzer
from feature_extractor import FeatureExtractor
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["analysis"])

class BacktestResponse(BaseModel):
    season: int
    total: int
    correct: int
    accuracy: float
    home_accuracy: float
    draw_accuracy: float
    away_accuracy: float
    predictions: list

@router.get("/backtest", response_model=BacktestResponse)
async def backtest(season: int = Query(2024, ge=2020, le=2025)):
    """运行历史回测"""
    backtester = Backtester(season=season)
    results = await backtester.backtest()
    return BacktestResponse(
        season=season,
        total=results["total_predictions"],
        correct=results["correct_predictions"],
        accuracy=results["accuracy"],
        home_accuracy=results["accuracy_by_result_type"].get("home", {}).get("accuracy", 0),
        draw_accuracy=results["accuracy_by_result_type"].get("draw", {}).get("accuracy", 0),
        away_accuracy=results["accuracy_by_result_type"].get("away", {}).get("accuracy", 0),
        predictions=results["predictions"][:100]
    )

@router.get("/analysis/team-form")
async def team_form_analysis():
    """球队形态分析"""
    analyzer = DataAnalyzer()
    # 这里应该加载历史数据
    return {"status": "success"}

@router.get("/features/{home_team}/{away_team}")
async def extract_features(home_team: str, away_team: str):
    """提取比赛特征"""
    # TODO: 从数据库加载球队统计
    return {"home_team": home_team, "away_team": away_team}

# 在 web/api.py 中添加:
# app.include_router(api_integration.router)

