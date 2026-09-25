"""阶段五：⚽ 单场比赛预测（不联网，使用仿真数据）。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from api_client import APIError
from bot_handler import BotUI
from config import load_settings
from service import MODEL_VERSION, PredictionService
from api_client import flatten_standings
from tests.sample_data import FakeAPI, default_fixtures, fixture, odds_response, standings_response

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


class FullAPI(FakeAPI):
    """带积分榜与赔率的仿真 API。standings 可控为空，用于验证「无数据不虚构」。"""

    def __init__(self, fixtures=None, standings=None, odds=None):
        super().__init__({})
        self.items = fixtures if fixtures is not None else default_fixtures()
        self.standings = standings if standings is not None else flatten_standings(standings_response())
        self.odds = odds if odds is not None else odds_response([("Bet365", 2.1, 3.3, 3.5)])
        self.standings_calls = 0

    async def get_fixtures(self, league_id, season, date_from, date_to):
        return self.items

    async def get_standings(self, league_id, season):
        self.standings_calls += 1
        return self.standings

    async def get_odds(self, fixture_id, fresh=False):
        return self.odds


def now_fixtures(count=2):
    base = datetime.now(timezone.utc)
    return [
        fixture(1001 + i, 1, f"主队{i + 1}", 2, f"客队{i + 1}", base + timedelta(hours=i + 1), status="NS")
        for i in range(count)
    ]


# ---- 概率合法性（禁止虚构 / 必须约等于 100%） ---------------------------------------
def test_probabilities_sum_to_one():
    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = run(svc.predict_fixture(1001, now_fixtures()))
    assert abs(p.prob_sum - 1.0) < 1e-9


def test_probabilities_within_zero_and_one():
    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = run(svc.predict_fixture(1001, now_fixtures()))
    for key in ("win_prob", "draw_prob", "loss_prob"):
        assert 0.0 <= p.analysis[key] <= 1.0


def test_no_team_data_marks_incomplete_not_fabricated():
    """积分榜为空时：必须标为「无数据」，且概率退化为联赛平均，不能假装是真实分析。"""
    api = FullAPI(now_fixtures(), standings=[])
    svc = PredictionService(SETTINGS, api)
    p = run(svc.predict_fixture(1001, now_fixtures()))
    assert p.has_team_data is False
    assert p.data_completeness == "无数据"
    assert "无数据" in BotUI.format_prediction_card(p, SETTINGS.timezone)
    assert abs(p.prob_sum - 1.0) < 1e-9  # 即便无数据，三项仍是合法概率


def test_unknown_team_marks_no_data():
    """积分榜里有别的球队，但没有这两支 → 同样算无数据。"""
    api = FullAPI(now_fixtures(1), standings=flatten_standings(standings_response()))
    svc = PredictionService(SETTINGS, api)
    fx = [fixture(9999, 77777, "陌生队A", 88888, "陌生队B", datetime.now(timezone.utc) + timedelta(hours=2))]
    p = run(svc.predict_fixture(9999, fx))
    assert p.has_team_data is False
    assert p.data_completeness == "无数据"


def test_low_sample_marks_partial():
    standings = [
        {"team": {"id": 1, "name": "主队1"},
         "home": {"played": 2, "goals": {"for": 3, "against": 2}},
         "away": {"played": 2, "goals": {"for": 2, "against": 3}}},
        {"team": {"id": 2, "name": "客队1"},
         "home": {"played": 2, "goals": {"for": 2, "against": 2}},
         "away": {"played": 2, "goals": {"for": 1, "against": 4}}},
    ]
    api = FullAPI(now_fixtures(1), standings=standings)
    svc = PredictionService(SETTINGS, api)
    p = run(svc.predict_fixture(1001, now_fixtures(1)))
    assert p.has_team_data is True
    assert p.low_sample is True
    assert p.data_completeness == "部分"


def test_missing_fixture_raises_key_error():
    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    with pytest.raises(KeyError):
        run(svc.predict_fixture(12345, now_fixtures()))


def test_missing_kickoff_raises_api_error():
    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    bad = [{"fixture": {"id": 1}, "teams": {"home": {"id": 1}, "away": {"id": 100}}}]
    with pytest.raises(APIError):
        run(svc.predict_fixture(1, bad))


# ---- 卡片内容（规格要求的字段齐全） ---------------------------------------------------
def test_card_contains_all_required_fields():
    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = run(svc.predict_fixture(1001, now_fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    # 新模板：顶部比赛 → 中部预测 → 下部依据 → 脚注来源
    for field in ("FOOTBALL INSIGHT", "主队1", "客队1", "比赛预测", "主胜", "平局", "客胜",
                  "预测结果", "预计比分", "信心等级", "数据完整性", "SEASON", "SOURCE"):
        assert field in text, field
    assert MODEL_VERSION in text
    # 少文字原则：不能回到堆字段的老样式
    assert "使用赛季：" not in text and "数据更新时间：" not in text


def test_card_has_risk_warning_and_disclaimer():
    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = run(svc.predict_fixture(1001, now_fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    assert "⚠️" in text
    assert "不构成投注建议" in text


@pytest.mark.parametrize("banned", ["必胜", "稳赢", "100%准确", "确定中奖"])
def test_card_never_claims_certainty(banned):
    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = run(svc.predict_fixture(1001, now_fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    assert banned not in text


def test_risk_lines_mention_missing_odds():
    api = FullAPI(now_fixtures(), odds=[])
    svc = PredictionService(SETTINGS, api)
    p = run(svc.predict_fixture(1001, now_fixtures()))
    assert p.odds is None
    assert any("赔率" in line for line in BotUI.risk_lines(p))


def test_confidence_text_for_no_data():
    api = FullAPI(now_fixtures(), standings=[])
    svc = PredictionService(SETTINGS, api)
    p = run(svc.predict_fixture(1001, now_fixtures()))
    assert "低" in BotUI.confidence_text(p)


def test_prediction_keyboard_links_to_existing_tabs_and_back():
    """按钮必须指向真实处理器，且提供返回入口。"""
    markup = BotUI.prediction_keyboard(1001)
    flat = [b for row in markup.inline_keyboard for b in row]
    data = {b.callback_data for b in flat}
    assert {"deep:1001", "refresh:1001", "chart:prob:1001"} <= data
    assert "menu:fixtures" in data and "menu:home" in data
    # 每个按钮都必须指向已实现的路由前缀，不能是死按钮
    for cb in data:
        assert cb.startswith(("deep:", "refresh:", "chart:", "menu:")), cb


# ---- handler 集成 ------------------------------------------------------------------
class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers, self.edits = [], []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
        self.edits.append((text, parse_mode, reply_markup))


def make_ctx(api, fx_cache=None):
    service = PredictionService(SETTINGS, api)
    bot_data = {"settings": SETTINGS, "service": service, "api": api}
    if fx_cache is not None:
        bot_data["fx_cache"] = fx_cache
    app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data=bot_data,
        job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []),
    )
    return SimpleNamespace(application=app, user_data={}), service


def q_update(data, user_id=555):
    q = FakeQuery(data)
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=user_id), effective_message=None), q


def test_fx_callback_renders_prediction_card():
    api = FullAPI(now_fixtures())
    ctx, _ = make_ctx(api, fx_cache={"date": "x", "items": now_fixtures()})
    update, q = q_update("fx:1001")
    run(main.on_predict_fixture(update, ctx))
    assert q.edits and "比赛预测" in q.edits[0][0]
    assert "主队1" in q.edits[0][0]


def test_fx_callback_reuses_cache_without_refetching_fixtures():
    class CountingAPI(FullAPI):
        def __init__(self):
            super().__init__(now_fixtures())
            self.fx_calls = 0

        async def get_fixtures(self, *a, **k):
            self.fx_calls += 1
            return self.items

    api = CountingAPI()
    ctx, _ = make_ctx(api, fx_cache={"date": "x", "items": now_fixtures()})
    update, _ = q_update("fx:1001")
    run(main.on_predict_fixture(update, ctx))
    assert api.fx_calls == 0  # 命中赛程缓存，不再重复拉赛程


def test_fx_callback_unknown_fixture_shows_hint():
    api = FullAPI(now_fixtures())
    ctx, _ = make_ctx(api, fx_cache={"date": "x", "items": now_fixtures()})
    update, q = q_update("fx:44444")
    run(main.on_predict_fixture(update, ctx))
    assert q.edits and "不在今日赛程" in q.edits[0][0]


def test_fx_callback_shows_api_error():
    class BoomAPI(FullAPI):
        async def get_standings(self, league_id, season):
            raise APIError("Free plans do not have access to this season")

    ctx, _ = make_ctx(BoomAPI(now_fixtures()), fx_cache={"date": "x", "items": now_fixtures()})
    update, q = q_update("fx:1001")
    run(main.on_predict_fixture(update, ctx))
    text = q.edits[0][0]
    assert "生成预测失败" in text and "套餐" in text


def test_fx_callback_duplicate_click_blocked():
    """重复点击不应产生并发请求（第二次直接被拦下，不再渲染）。"""
    class SlowAPI(FullAPI):
        async def get_standings(self, league_id, season):
            await asyncio.sleep(0)
            return self.standings

    ctx, _ = make_ctx(SlowAPI(now_fixtures()), fx_cache={"date": "x", "items": now_fixtures()})
    update, q = q_update("fx:1001")
    run(main.on_predict_fixture(update, ctx))
    first_edits = len(q.edits)
    # 模拟并发：任务未结束时再次点击
    ctx.application.bot_data.setdefault("pending", set()).add((555, "pred:1001"))
    update2, q2 = q_update("fx:1001")
    run(main.on_predict_fixture(update2, ctx))
    assert len(q2.edits) == 0  # 被拦下，没有重复渲染
    assert first_edits == 1


def test_fx_callback_unexpected_error_does_not_crash():
    class BrokenAPI(FullAPI):
        async def get_standings(self, league_id, season):
            raise RuntimeError("boom")

    ctx, _ = make_ctx(BrokenAPI(now_fixtures()), fx_cache={"date": "x", "items": now_fixtures()})
    update, q = q_update("fx:1001")
    run(main.on_predict_fixture(update, ctx))
    assert q.edits and "生成预测失败" in q.edits[0][0]


def test_prediction_is_remembered_for_tab_buttons():
    """生成后要能复用现有 4 个标签页按钮（深度分析/交锋/赔率）。"""
    api = FullAPI(now_fixtures())
    ctx, service = make_ctx(api, fx_cache={"date": "x", "items": now_fixtures()})
    update, _ = q_update("fx:1001")
    run(main.on_predict_fixture(update, ctx))
    assert service.get(1001).fixture_id == 1001
    assert service.cached_predictions >= 1
