"""P0-3：预测可信度 / Prediction trustworthiness.

核心目标：预测必须可追溯、可解释，数据不足时绝不给虚假结论。
"""
from datetime import datetime, timezone, timedelta

import pytest

from analyzer import MatchAnalyzer, calculate_prediction_level, validate_probabilities
from api_client import APIError, flatten_standings
from service import PredictionService
from tests.sample_data import fixture, standings_response

ENV = {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "1", "SEASON": "2026", "ADMIN_ID": "1"}


def run(coro):
    import asyncio
    return asyncio.run(coro)


def now_dt(hours=2):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class API:
    """可配置的数据源桩：控制积分榜是否包含目标球队。"""

    def __init__(self, standings=None, include_teams=True):
        # 注意：真实 get_standings 返回的是 flatten 后的行列表，不是原始包装响应
        raw = standings if standings is not None else standings_response()
        self.standings = flatten_standings(raw)
        self.include_teams = include_teams
        self.season_in_use = 2026

    async def get_fixtures(self, *a, **k):
        return [fixture(1001, 1, "曼城", 2, "利物浦", now_dt())]

    async def get_standings(self, *a, **k):
        if not self.include_teams:
            return []  # 积分榜为空 → 数据不足
        return self.standings

    async def get_odds(self, *a, **k):
        return []

    async def get_h2h(self, *a, **k):
        return []

    async def get_team_form(self, *a, **k):
        return []


def svc(api):
    from config import load_settings

    return PredictionService(load_settings(ENV), api, MatchAnalyzer())


# ---- 无数据禁止预测 -----------------------------------------------------------
def test_missing_kickoff_raises():
    """缺少开赛时间 → 禁止生成预测。"""
    fx = fixture(1001, 1, "A", 2, "B", now_dt())
    fx["fixture"]["date"] = None
    with pytest.raises(APIError, match="开赛时间"):
        run(svc(API()).predict_fixture(1001, [fx]))


def test_missing_teams_raises():
    """缺少参赛队伍 → 禁止生成预测。"""
    fx = fixture(1001, 1, "A", 2, "B", now_dt())
    fx["teams"] = {}
    with pytest.raises(APIError, match="队伍"):
        run(svc(API()).predict_fixture(1001, [fx]))


def test_unknown_fixture_raises_keyerror():
    with pytest.raises(KeyError):
        run(svc(API()).predict_fixture(9999, []))


# ---- 输入数据快照 -------------------------------------------------------------
def test_prediction_records_inputs():
    """预测必须保存输入数据、模型版本、生成时间，便于追溯。"""
    p = run(svc(API()).predict_fixture(1001, None))
    assert p.inputs["model_version"]
    assert p.inputs["season"] == 2026
    assert p.inputs["standings_rows"] > 0
    assert p.inputs["generated_at"]
    assert p.inputs["has_home_data"] and p.inputs["has_away_data"]
    assert p.model_version == p.inputs["model_version"]


def test_prediction_created_at_is_utc():
    p = run(svc(API()).predict_fixture(1001, None))
    assert p.created_at.tzinfo is not None


# ---- 数据不足 ---------------------------------------------------------------
def test_insufficient_when_standings_missing_teams():
    """积分榜不含这两队 → 标记数据不足，不给虚假结论。"""
    p = run(svc(API(include_teams=False)).predict_fixture(1001, None))
    assert p.insufficient is True
    assert p.has_team_data is False
    assert p.data_completeness == "无数据"


def test_sufficient_when_standings_has_teams():
    p = run(svc(API()).predict_fixture(1001, None))
    assert p.insufficient is False
    assert p.has_team_data is True


def test_insufficient_prediction_card_warns():
    """数据不足的预测卡片必须显示警告，不能与正常预测同等呈现。"""
    from bot_handler import BotUI
    from config import load_settings

    p = run(svc(API(include_teams=False)).predict_fixture(1001, None))
    text = BotUI.format_prediction_card(p, load_settings(ENV).timezone)
    assert "数据不足" in text
    assert "不代表双方真实实力" in text


def test_normal_prediction_card_no_insufficient_warning():
    from bot_handler import BotUI
    from config import load_settings

    p = run(svc(API()).predict_fixture(1001, None))
    text = BotUI.format_prediction_card(p, load_settings(ENV).timezone)
    assert "数据不足" not in text


# ---- 概率有效性 --------------------------------------------------------------
def test_probabilities_sum_to_one():
    p = run(svc(API()).predict_fixture(1001, None))
    assert abs(p.prob_sum - 1.0) < 1e-9


def test_probabilities_in_range_and_valid():
    p = run(svc(API()).predict_fixture(1001, None))
    validate_probabilities({"home_win": p.analysis["win_prob"],
                            "draw": p.analysis["draw_prob"],
                            "away_win": p.analysis["loss_prob"]})


# ---- 信心等级阈值 ------------------------------------------------------------
def test_level_thresholds():
    """高：≥65% 且领先≥15%；中：≥50% 且领先≥8%；其余为低。"""
    assert calculate_prediction_level({"home_win": .75, "draw": .15, "away_win": .10})["key"] == "high"
    assert calculate_prediction_level({"home_win": .55, "draw": .30, "away_win": .15})["key"] == "medium"
    assert calculate_prediction_level({"home_win": .42, "draw": .32, "away_win": .26})["key"] == "low"


def test_level_boundary_high():
    """边界：恰好 65% 应为高（领先优势必然 ≥15%，因三项归一）。"""
    r = calculate_prediction_level({"home_win": .65, "draw": .25, "away_win": .10})
    assert r["key"] == "high"
    assert r["probability"] == pytest.approx(.65)


def test_level_boundary_medium_edge():
    """边界：50% 但领先不足 8% → 低（差距门槛才是真正起作用的边界）。"""
    assert calculate_prediction_level({"home_win": .50, "draw": .44, "away_win": .06})["key"] == "low"
    # 50% 且领先 10% → 中
    assert calculate_prediction_level({"home_win": .50, "draw": .30, "away_win": .20})["key"] == "medium"


def test_validate_probabilities_rejects_bad_total():
    with pytest.raises(ValueError, match="概率总和"):
        validate_probabilities({"home_win": .5, "draw": .5, "away_win": .5})


def test_validate_probabilities_rejects_out_of_range():
    with pytest.raises(ValueError, match="超出范围"):
        validate_probabilities({"home_win": 1.5, "draw": 0, "away_win": 0})


def test_validate_probabilities_requires_keys():
    with pytest.raises(ValueError, match="缺少概率字段"):
        validate_probabilities({"home_win": .5})
