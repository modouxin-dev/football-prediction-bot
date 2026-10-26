import asyncio
import dataclasses
from datetime import timedelta

import pytest

from api_client import APIError
from bot_handler import DISCLAIMER, BotUI
from config import load_settings
from service import PredictionService, round_label
from tests.sample_data import NOW, FakeAPI, default_fixtures, fixture

ENV = {"TELEGRAM_TOKEN": "t", "RAPID_API_KEY": "k", "CHAT_ID": "1", "SEASON": "2026", "TIMEZONE": "Asia/Shanghai"}
SETTINGS = load_settings(ENV)
TZ = SETTINGS.timezone
ui = BotUI()


def run(coro):
    return asyncio.run(coro)


def build(api=None, settings=SETTINGS, **kwargs):
    api = api or FakeAPI({2026: default_fixtures()})
    service = PredictionService(settings, api)
    return service, api, run(service.build_predictions(now=NOW, **kwargs))


def test_only_upcoming_matches_in_window_are_predicted_earliest_first():
    _, api, preds = build()
    assert [p.fixture_id for p in preds] == [102, 101]  # 已结束的 103 和超出窗口的 104 被排除
    assert api.standings_calls == [2026] and len(api.odds_calls) == 2


def test_limit_and_max_matches_are_respected():
    assert [p.fixture_id for p in build(limit=1)[2]] == [102]
    small = dataclasses.replace(SETTINGS, max_matches=1)
    assert len(build(settings=small)[2]) == 1


def test_no_fixtures_returns_empty_and_does_not_spend_extra_requests():
    service, api, preds = build(FakeAPI({2026: []}))
    assert preds == [] and api.standings_calls == [] and api.odds_calls == []


def test_stale_season_variable_falls_back_to_the_current_season():
    """回归：SEASON 仍是 2025 时，旧版会一直取不到 2026 赛季的赛程。"""
    stale = dataclasses.replace(SETTINGS, season=SETTINGS.expected_season - 1)
    api = FakeAPI({stale.expected_season: default_fixtures()})
    service, _, preds = build(api, settings=stale)
    assert api.fixture_calls == [stale.season, stale.expected_season]
    assert len(preds) == 2 and service.season_in_use == stale.expected_season


def test_predictions_use_real_team_strength():
    preds = build(FakeAPI({2026: [fixture(1, 1, "Alpha FC", 4, "Delta", NOW + timedelta(hours=2)),
                  fixture(2, 4, "Delta", 1, "Alpha FC", NOW + timedelta(hours=3))]}))[2]
    strong_home, weak_home = preds
    # 展示概率已换成市场口径（见 apply_market_adjustment），不再直接反映球队强度；
    # 「模型确实用了真实球队强度」这一性质由 poisson_probs（内部基准）继续守。
    assert strong_home.analysis["poisson_probs"]["home"] > 0.6 > weak_home.analysis["poisson_probs"]["home"]
    assert strong_home.analysis["market_used"] is True
    assert strong_home.odds["n"] == 2 and strong_home.best is not None


def test_odds_failure_means_no_recommendation():
    """赔率取不到 → 没有亚盘 → 按策略不予推荐（不再是「照常出预测」）。

    策略变更（项目方向：核心标准是准确率）：
    过去是「没有赔率也照常出预测，展示层写暂无」；
    现在无亚盘就没有「模型 vs 市场」的偏差参照，推了无法验证也无法复盘，
    因此整场不推荐、不入库。

    这里守的是**降级仍然可见**：不是静默消失，而是把原因说清楚。
    """
    svc, _, preds = build(FakeAPI({2026: default_fixtures()},
                                  odds_error=APIError("boom")))
    assert preds == [], "无亚盘时不应产生任何推荐"
    # 全被跳过时必须留痕，否则用户看到「一场不推」会以为机器人坏了
    assert svc.no_handicap_count == 2
    assert "未开盘亚洲盘" in (svc.last_note or "")


def test_refresh_refetches_odds_bypassing_cache():
    service, api, preds = build()
    refreshed = run(service.refresh(preds[0].fixture_id))
    assert api.odds_calls[-1] == (preds[0].fixture_id, True)
    assert service.get(preds[0].fixture_id) is refreshed


def test_store_lookup_and_eviction():
    service, _, preds = build()
    assert service.get(101).home == "Alpha FC"
    with pytest.raises(KeyError):
        service.get(999)


def test_round_label():
    assert round_label("Regular Season - 6") == "第 6 轮"
    assert round_label("Round of 16") == "Round of 16" and round_label(None) == ""


# ---- 模板 -------------------------------------------------------------------
def test_main_message_is_beautified_escaped_and_uses_local_time():
    preds = build()[2]
    text = ui.format_prediction(preds[0], TZ)  # 102：Beta & Sons vs Gamma
    assert "Beta &amp; Sons" in text and "Beta & Sons" not in text  # HTML 转义，否则 Telegram 会拒绝发送
    assert "▰" in text and "▱" in text and "第 6 轮" in text and "Main Ground" in text
    assert "09-24 17:00" in text and "UTC+8" in text  # 09:00 UTC 开球 → UTC+8 显示 17:00
    assert "模型差异" in text and "Value Bet" not in text  # Value Bet 已按实测移除
    assert DISCLAIMER in text and "重仓" not in text


def test_all_views_render_without_errors_and_stay_within_telegram_limit():
    from tests.sample_data import h2h_matches

    p = build()[2][1]  # 101：Alpha FC vs Delta
    views = [
        ui.format_prediction(p, TZ),
        ui.format_deep_analysis(p, TZ),
        ui.format_odds_detail(p, TZ),
        ui.format_h2h(p, h2h_matches(), TZ),
    ]
    assert all(0 < len(v) < 4096 for v in views)
    assert "最可能比分" in views[1] and "球队强度" in views[1]
    assert "庄家抽水" in views[2] and "Bet A" in views[2]
    assert "1 胜 1 平 1 负" in views[3]  # Alpha 视角：主场 3-0 胜、客场 1-1 平、客场 0-2 负


def test_odds_and_h2h_views_handle_missing_data():
    """展示层在无赔率/无交锋时仍要说清楚，不能空白或报错。

    注意：无赔率的场次现在**不会进入推荐列表**（无盘不推荐），
    所以这里必须绕开 build_predictions 的过滤，直接取 Prediction 本体
    来测展示层——被测的是「渲染会不会崩」，不是「推不推」。
    """
    svc, api, _ = build(FakeAPI({2026: default_fixtures()}, odds=[]))
    p = run(svc._predict_one(run(svc._model_for(39)),
                             NOW + timedelta(hours=2), default_fixtures()[0]))
    assert "暂无赔率" in ui.format_odds_detail(p, TZ)
    assert "暂无历史交锋" in ui.format_h2h(p, [], TZ)


def test_keyboard_marks_active_tab_and_carries_fixture_id():
    kb = ui.get_main_keyboard(101, "deep")
    buttons = [b for row in kb.inline_keyboard for b in row]
    # 只比对与本场相关的按钮：键盘末尾另有出口行（返回赛程/主菜单），
    # 那是导航按钮，不属于 tab 集合。
    tabs = [b for b in buttons if b.callback_data.endswith(":101")]
    assert sorted(b.callback_data for b in tabs) == ["deep:101", "h2h:101", "home:101", "odds:101", "refresh:101"]
    assert [b.text for b in buttons if b.text.startswith("●")] == ["● 🔍 深度分析"]
    assert all(len(b.callback_data) <= 64 for b in buttons)
    # 二级视图必须有出口，否则切进 tab 后无法离开
    assert "menu:home" in [b.callback_data for b in buttons]


def test_strategy_thresholds():
    p = build()[2][0]
    p.best = ("home", {"edge": 0.10, "prob": 0.5, "odds": 2.0, "ev": 0.1})
    assert "模型高于市场" in ui.get_strategy(p) and "价值" not in ui.get_strategy(p)
    p.best = ("away", {"edge": 0.04, "prob": 0.3, "odds": 3.0, "ev": 0.1})
    assert "略高于市场" in ui.get_strategy(p)
    p.best = ("draw", {"edge": -0.02, "prob": 0.2, "odds": 3.0, "ev": -0.1})
    assert "观望" in ui.get_strategy(p)
