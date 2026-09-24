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
    preds = build(FakeAPI({2026: [fixture(1, 1, "Alpha FC", 4, "Delta", NOW + timedelta(hours=2)), fixture(2, 4, "Delta", 1, "Alpha FC", NOW + timedelta(hours=3))]}))[2]
    strong_home, weak_home = preds
    assert strong_home.analysis["win_prob"] > 0.6 > weak_home.analysis["win_prob"]
    assert strong_home.odds["n"] == 2 and strong_home.best is not None


def test_odds_failure_degrades_gracefully():
    _, _, preds = build(FakeAPI({2026: default_fixtures()}, odds_error=APIError("boom")))
    assert len(preds) == 2 and all(p.odds is None and p.best is None for p in preds)
    assert "暂无" in ui.format_prediction(preds[0], TZ)


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
    assert "Value Bet" in text or "价值偏差" in text
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
    p = build(FakeAPI({2026: default_fixtures()}, odds=[]))[2][0]
    assert "暂无赔率" in ui.format_odds_detail(p, TZ)
    assert "暂无历史交锋" in ui.format_h2h(p, [], TZ)


def test_keyboard_marks_active_tab_and_carries_fixture_id():
    kb = ui.get_main_keyboard(101, "deep")
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert sorted(b.callback_data for b in buttons) == ["deep:101", "h2h:101", "home:101", "odds:101", "refresh:101"]
    assert [b.text for b in buttons if b.text.startswith("●")] == ["● 🔍 深度分析"]
    assert all(len(b.callback_data) <= 64 for b in buttons)


def test_strategy_thresholds():
    p = build()[2][0]
    p.best = ("home", {"edge": 0.10, "prob": 0.5, "odds": 2.0, "ev": 0.1})
    assert "价值较高" in ui.get_strategy(p)
    p.best = ("away", {"edge": 0.04, "prob": 0.3, "odds": 3.0, "ev": 0.1})
    assert "小幅价值" in ui.get_strategy(p)
    p.best = ("draw", {"edge": -0.02, "prob": 0.2, "odds": 3.0, "ev": -0.1})
    assert "观望" in ui.get_strategy(p)
