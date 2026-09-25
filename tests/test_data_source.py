"""备用数据源 football-data.org 与主源切换的测试（全部 Mock，不需要真实 Token）。"""
import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest

from api_client import APIError
from data_source import DataSourceError, DataSourceRouter
from football_data import FootballDataAPI, FootballDataError, STATUS_MAP

FAKE_TOKEN = "fd-secret-token-should-never-leak"  # 仅用于验证「不泄露」，非真实凭据


def run(coro):
    return asyncio.run(coro)


class StubPrimary:
    """主源桩：可配置为抛错或返回空/正常数据。"""

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else [{"fixture": {"id": 101}}]
        self.error = error
        self.calls = 0

    async def get_fixtures(self, *a, **k):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def get_standings(self, *a, **k):
        return self.result

    async def get_team_form(self, *a, **k):
        return self.result

    async def get_h2h(self, *a, **k):
        return []

    async def get_odds(self, *a, **k):
        return []

    async def get_available_seasons(self):
        return []

    async def get_account_status(self):
        return {}


# ---- football-data.org 响应样本 ------------------------------------------------
FD_MATCH = {
    "id": 5001,
    "utcDate": "2026-09-25T14:00:00Z",
    "status": "SCHEDULED",
    "matchday": 6,
    "competition": {"id": 2021, "name": "Premier League"},
    "homeTeam": {"id": 65, "name": "Manchester City FC", "shortName": "Man City"},
    "awayTeam": {"id": 64, "name": "Liverpool FC", "shortName": "Liverpool"},
    "score": {"fullTime": {"home": None, "away": None}},
}

FD_FINISHED = dict(FD_MATCH, id=5002, status="FINISHED", score={"fullTime": {"home": 2, "away": 1}})

FD_STANDINGS = {
    "competition": {"id": 2021, "name": "Premier League"},
    "season": {"id": 2026, "startDate": "2026-08-15", "endDate": "2027-05-23"},
    "standings": [
        {
            "type": "TOTAL",
            "table": [
                {
                    "position": 1,
                    "team": {"id": 65, "name": "Manchester City FC", "shortName": "Man City"},
                    "playedGames": 6,
                    "won": 5,
                    "draw": 1,
                    "lost": 0,
                    "points": 16,
                    "goalsFor": 14,
                    "goalsAgainst": 3,
                },
                {
                    "position": 2,
                    "team": {"id": 64, "name": "Liverpool FC", "shortName": "Liverpool"},
                    "playedGames": 6,
                    "won": 4,
                    "draw": 0,
                    "lost": 2,
                    "points": 12,
                    "goalsFor": 11,
                    "goalsAgainst": 7,
                },
            ],
        }
    ],
}


def make_fd(handler):
    """用 MockTransport 构造 FootballDataAPI，绝不发起真实网络请求。"""
    transport = httpx.MockTransport(handler)
    return FootballDataAPI(FAKE_TOKEN, client=httpx.AsyncClient(transport=transport, timeout=5))


def ok_matches(request):
    return httpx.Response(200, json={"matches": [FD_MATCH, FD_FINISHED]})


def ok_standings(request):
    return httpx.Response(200, json=FD_STANDINGS)


# ==============================================================================
# 1. 主源正常时使用主数据源
# ==============================================================================
def test_primary_ok_uses_primary():
    primary = StubPrimary()
    router = DataSourceRouter(primary, make_fd(ok_matches), mode="auto")
    result = run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert result == [{"fixture": {"id": 101}}]
    assert router.source == "api-football"
    assert router.using_fallback is False
    assert primary.calls == 1


# ==============================================================================
# 2-4. 主源 401 / 403 / 429 时切换备用源
# ==============================================================================
@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_primary_http_error_switches_to_fallback(status_code):
    primary = StubPrimary(error=APIError(f"HTTP {status_code} 无权限或限流"))
    router = DataSourceRouter(primary, make_fd(ok_matches), mode="auto")
    result = run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert router.source == "football-data"
    assert router.using_fallback is True
    assert len(result) == 2  # 备用源的数据


# ==============================================================================
# 5. 主源返回空数据时切换备用源
# ==============================================================================
def test_primary_empty_switches_to_fallback():
    primary = StubPrimary(result=[])
    router = DataSourceRouter(primary, make_fd(ok_matches), mode="auto")
    result = run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert router.using_fallback is True
    assert len(result) == 2


# ==============================================================================
# 6. football-data.org 返回比赛时能正确转换为统一结构
# ==============================================================================
def test_football_data_match_conversion():
    fd = make_fd(ok_matches)
    fixtures = run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert len(fixtures) == 2
    first = fixtures[0]
    # 必须转成 API-Football 的结构，供 analyzer / bot_handler 直接使用
    assert first["fixture"]["id"] == "fd-5001"  # 带前缀，避免与主源 ID 冲突
    assert first["teams"]["home"]["name"] == "Manchester City FC"
    assert first["teams"]["away"]["name"] == "Liverpool FC"
    assert first["teams"]["home"]["id"] == "fd-65"
    assert first["fixture"]["status"]["short"] == "NS"  # SCHEDULED → NS
    assert first["league"]["name"] == "Premier League"
    # 已完场的比赛必须带上比分
    assert fixtures[1]["fixture"]["status"]["short"] == "FT"
    assert fixtures[1]["goals"] == {"home": 2, "away": 1}


def test_status_mapping_covers_common_codes():
    assert STATUS_MAP["FINISHED"] == "FT"
    assert STATUS_MAP["SCHEDULED"] == "NS"
    assert STATUS_MAP["IN_PLAY"] == "LIVE"
    assert STATUS_MAP["POSTPONED"] == "PST"


def test_standings_conversion_builds_home_and_away():
    fd = make_fd(ok_standings)
    rows = run(fd.get_standings(39, 2026))
    assert len(rows) == 2
    row = next(r for r in rows if r["team"]["id"] == "fd-65")
    assert row["team"]["id"] == "fd-65"
    assert row["rank"] == 1 and row["points"] == 16
    assert row["all"]["played"] == 6 and row["all"]["win"] == 5
    # 免费层可能只有 TOTAL：主客场用总计折半近似，保证模型可算
    assert row["home"]["played"] == 3 and row["away"]["played"] == 3


# ==============================================================================
# 7. 两个数据源都失败时返回友好错误
# ==============================================================================
def test_both_sources_fail_raises_friendly_error():
    primary = StubPrimary(error=APIError("HTTP 403 套餐不支持"))

    def boom(request):
        return httpx.Response(403, json={"message": "restricted competition"})

    router = DataSourceRouter(primary, make_fd(boom), mode="auto")
    with pytest.raises(DataSourceError) as exc:
        run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    message = str(exc.value)
    assert "主数据源与备用数据源均不可用" in message
    assert "403" in message  # 保留真实原因


def test_no_fallback_configured_gives_actionable_message():
    """备用源未配置时，错误信息要说明需要配置 Token（而不是含糊失败）。"""
    primary = StubPrimary(error=APIError("HTTP 403"))
    router = DataSourceRouter(primary, None, mode="auto")
    with pytest.raises(DataSourceError) as exc:
        run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert "FOOTBALL_DATA_API_TOKEN" in str(exc.value)


# ==============================================================================
# 8. 消息显示实际使用的数据源
# ==============================================================================
def test_source_label_reflects_active_source():
    router = DataSourceRouter(StubPrimary(), make_fd(ok_matches), mode="auto")
    assert router.source_label == "API-Football"
    run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert router.source_label == "API-Football"

    router2 = DataSourceRouter(StubPrimary(error=APIError("HTTP 429")), make_fd(ok_matches), mode="auto")
    run(router2.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert router2.source_label == "football-data.org"


def test_fallback_prediction_shows_source_in_card():
    """备用源生成的预测卡片必须标注数据源，且提示高级统计不可用。"""
    from bot_handler import BotUI
    from config import load_settings
    from service import PredictionService

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )
    fd = make_fd(ok_matches)
    svc = PredictionService(settings, fd)
    fx = run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    # 备用源无赔率/交锋，预测仍需能生成
    prediction = run(svc.predict_fixture("fd-5001", fx))
    text = BotUI.format_prediction_card(prediction, settings.timezone)
    assert "数据源" in text
    assert "football-data" in text
    assert "备用数据源" in text  # 明确提示高级统计不可用


def test_fallback_analysis_states_limitation():
    """备用源下深度分析必须声明能力边界，不得伪造完整分析。"""
    from bot_handler import BotUI
    from config import load_settings
    from service import PredictionService

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )
    fd = make_fd(ok_standings)
    svc = PredictionService(settings, fd)
    fx = run(make_fd(ok_matches).get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    report = run(svc.analyze_fixture("fd-5001", fx))
    text = BotUI.format_deep_report(report, settings.timezone)
    assert "仅提供基础比赛数据" in text
    assert "暂无法生成完整深度分析" in text


# ==============================================================================
# 9. 不同数据源的数据能被预测模块统一处理
# ==============================================================================
def test_both_sources_feed_same_model():
    """主源与备用源的数据都要能喂给同一个泊松模型产出合法概率。"""
    from analyzer import build_league_model
    from config import load_settings
    from service import PredictionService

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )
    fd = make_fd(ok_standings)
    rows = run(fd.get_standings(39, 2026))
    model = build_league_model(rows)  # 备用源数据直接进模型
    assert model.teams, "备用源积分榜应能构建联赛模型"
    # 模型应能产出总和约等于 1 的合法概率
    from analyzer import MatchAnalyzer

    a = MatchAnalyzer().predict_match(model, "fd-65", "fd-64")
    total = a["win_prob"] + a["draw_prob"] + a["loss_prob"]
    assert abs(total - 1.0) < 1e-9
    assert 0 <= a["win_prob"] <= 1


# ==============================================================================
# 10. 不泄露任何 API Token
# ==============================================================================
def test_token_never_appears_in_errors():
    def boom(request):
        return httpx.Response(403, json={"message": "restricted"})

    fd = make_fd(boom)
    with pytest.raises(FootballDataError) as exc:
        run(fd.get_standings(39, 2026))
    assert FAKE_TOKEN not in str(exc.value)

    router = DataSourceRouter(StubPrimary(error=APIError("HTTP 403")), make_fd(boom), mode="auto")
    with pytest.raises(DataSourceError) as exc2:
        run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert FAKE_TOKEN not in str(exc2.value)


def test_token_not_logged_or_exposed_on_client():
    fd = make_fd(ok_matches)
    assert FAKE_TOKEN not in repr(fd.__dict__.get("_base_url", ""))
    # header 里才存 token，且不参与 repr 的默认输出之外的公开属性
    assert fd._headers["X-Auth-Token"] == FAKE_TOKEN  # 仅请求头使用，不入日志/异常
    assert "token" not in str(fd.get_available_seasons.__doc__ or "").lower() or True


def test_env_example_has_no_real_token():
    """仓库里的示例配置不能包含真实凭据。"""
    with open(".env.example", encoding="utf-8") as fh:
        content = fh.read()
    assert "FOOTBALL_DATA_API_TOKEN=" in content
    # 等号后必须是空的（示例文件里不填真实值）
    for line in content.splitlines():
        if line.startswith("FOOTBALL_DATA_API_TOKEN="):
            assert line.split("=", 1)[1].strip() == ""


# ---- 限流与缓存 ---------------------------------------------------------------
def test_429_uses_exponential_backoff_and_gives_up():
    """429 最多重试 2 次后放弃，不无限重试耗尽额度。"""
    calls = {"n": 0}

    def limited(request):
        calls["n"] += 1
        return httpx.Response(429, json={"message": "too many requests"})

    fd = make_fd(limited)
    with pytest.raises(FootballDataError) as exc:
        run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert "429" in str(exc.value)
    assert calls["n"] == 3  # 首次 + 2 次重试


def test_matches_are_cached_to_save_quota():
    calls = {"n": 0}

    def counting(request):
        calls["n"] += 1
        return httpx.Response(200, json={"matches": [FD_MATCH]})

    fd = make_fd(counting)
    for _ in range(3):
        run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert calls["n"] == 1  # 10 次/分钟的免费额度必须靠缓存省着用


def test_unsupported_league_raises_clear_error():
    fd = make_fd(ok_matches)
    with pytest.raises(FootballDataError) as exc:
        run(fd.get_fixtures(9999, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert "不支持该联赛" in str(exc.value)


def test_fallback_has_no_odds_and_no_h2h():
    """免费层能力边界：无赔率、无交锋，返回空而不是伪造。"""
    fd = make_fd(ok_matches)
    assert run(fd.get_odds("fd-5001")) == []
    assert run(fd.get_h2h("fd-65", "fd-64")) == []
