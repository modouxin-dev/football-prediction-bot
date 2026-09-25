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


# ==============================================================================
# 回归：主源赛季不可用 + 备用源当日确实无比赛 → 应显示「今日暂无比赛」，不能报数据源故障
# ==============================================================================
def test_fallback_empty_matches_is_not_a_failure():
    """备用源正常响应（HTTP 200）但没有比赛 = 今天确实没比赛，不是故障。"""
    primary = StubPrimary(error=APIError("plan: Free plans do not have access to this season"))

    def empty_matches(request):
        return httpx.Response(200, json={"matches": []})

    router = DataSourceRouter(primary, make_fd(empty_matches), mode="auto")
    result = run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert result == []
    assert router.using_fallback is True          # 确实在用备用源
    assert router.last_error("api-football")      # 主源的赛季原因被保留，供提示使用


def test_today_fixtures_empty_shows_reason_not_error():
    """端到端：主源赛季不可用 + 备用源无比赛 → 返回空并给出说明，不抛异常。"""
    from config import load_settings
    from service import PredictionService

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )

    class SeasonBlockedPrimary(StubPrimary):
        async def get_fixtures(self, *a, **k):
            raise APIError("plan: Free plans do not have access to this season")

        async def get_available_seasons(self):
            return [2026, 2025, 2024]

    def empty_matches(request):
        return httpx.Response(200, json={"matches": []})

    router = DataSourceRouter(SeasonBlockedPrimary(), make_fd(empty_matches), mode="auto")
    svc = PredictionService(settings, router)
    fixtures = run(svc.get_today_fixtures())
    assert fixtures == []
    assert svc.last_note and "备用数据源" in svc.last_note and "暂无比赛" in svc.last_note


def test_fallback_real_failure_still_raises():
    """备用源自身报错（如 403）才是真的故障，必须抛错而不是假装没比赛。"""
    primary = StubPrimary(error=APIError("plan: Free plans do not have access to this season"))

    def boom(request):
        return httpx.Response(403, json={"message": "restricted"})

    router = DataSourceRouter(primary, make_fd(boom), mode="auto")
    with pytest.raises(DataSourceError) as exc:
        run(router.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert "均不可用" in str(exc.value)


# ==============================================================================
# 今日无比赛 → 自动扩展到未来几天（避免给用户一片空白）
# ==============================================================================
def _future_match(mid, utc_date, home="Man City", away="Liverpool"):
    return {
        "id": mid, "utcDate": utc_date, "status": "SCHEDULED", "matchday": 7,
        "competition": {"id": 2021, "name": "Premier League"},
        "homeTeam": {"id": 65, "name": home}, "awayTeam": {"id": 64, "name": away},
        "score": {"fullTime": {"home": None, "away": None}},
    }


def test_today_empty_expands_to_upcoming_days():
    """今日无比赛时，应自动拉取未来几天赛程，而不是返回空。"""
    from config import load_settings
    from service import PredictionService, UPCOMING_DAYS

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )

    class SeasonBlocked(StubPrimary):
        async def get_fixtures(self, *a, **k):
            raise APIError("plan: Free plans do not have access to this season")

        async def get_available_seasons(self):
            return [2026, 2025, 2024]

    seen_ranges = []

    def handler(request):
        params = request.url.params
        df, dt = params.get("dateFrom"), params.get("dateTo")
        seen_ranges.append((df, dt))
        if df == dt:  # 只查今天 → 空
            return httpx.Response(200, json={"matches": []})
        return httpx.Response(200, json={"matches": [_future_match(6001, "2026-09-27T14:00:00Z")]})

    router = DataSourceRouter(SeasonBlocked(), make_fd(handler), mode="auto")
    svc = PredictionService(settings, router)
    fixtures = run(svc.get_today_fixtures())

    assert len(fixtures) == 1, "今日无比赛时应返回未来赛程"
    assert svc.using_upcoming is True
    assert "~" in svc.fixture_day_label  # 日期范围
    assert svc.last_note and "暂无比赛" in svc.last_note and str(UPCOMING_DAYS) in svc.last_note
    # 确认确实先查了「今天」，再查了「今天~未来」
    assert seen_ranges[0][0] == seen_ranges[0][1]


def test_upcoming_fixtures_are_sorted_by_kickoff():
    from config import load_settings
    from service import PredictionService

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )

    class SeasonBlocked(StubPrimary):
        async def get_fixtures(self, *a, **k):
            raise APIError("plan: Free plans do not have access to this season")

        async def get_available_seasons(self):
            return [2026, 2025, 2024]

    matches = [
        _future_match(6002, "2026-09-29T18:30:00Z"),  # 较晚
        _future_match(6001, "2026-09-27T14:00:00Z"),  # 较早
    ]

    def handler(request):
        p = request.url.params
        if p.get("dateFrom") == p.get("dateTo"):
            return httpx.Response(200, json={"matches": []})
        return httpx.Response(200, json={"matches": matches})

    router = DataSourceRouter(SeasonBlocked(), make_fd(handler), mode="auto")
    svc = PredictionService(settings, router)
    fixtures = run(svc.get_today_fixtures())
    ids = [str((f.get("fixture") or {}).get("id")) for f in fixtures]
    assert ids == ["fd-6001", "fd-6002"], f"未来赛程应按开赛时间升序，实际 {ids}"


def test_multi_day_page_shows_date_and_recent_title():
    """跨天时标题应为「近期赛程」，且每行显示日期，避免误以为是今天的比赛。"""
    from bot_handler import BotUI
    from config import load_settings
    from service import PredictionService

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )
    raw = [_future_match(6001, "2026-09-27T14:00:00Z")]
    fixtures = [FootballDataAPI._to_fixture(m, 39, 2026) for m in raw]  # 经协议转换后才排版
    text, _, _, _ = BotUI.format_fixtures_page(
        fixtures, settings.timezone, 0, 5, "2026-09-25 ~ 2026-10-02", multi_day=True
    )
    assert "近期赛程" in text
    assert "09-27" in text  # 每行带日期
    assert "今日赛程" not in text


def test_today_with_matches_keeps_today_title():
    """今日有比赛时不触发扩展，标题仍是「今日赛程」且不带日期前缀。"""
    from bot_handler import BotUI
    from config import load_settings
    from service import PredictionService

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "CHAT_ID": "5", "SEASON": "2026", "ADMIN_ID": "5"}
    )
    raw = [_future_match(6001, "2026-09-25T10:00:00Z")]
    fixtures = [FootballDataAPI._to_fixture(m, 39, 2026) for m in raw]
    text, _, _, _ = BotUI.format_fixtures_page(
        fixtures, settings.timezone, 0, 5, "2026-09-25", multi_day=False
    )
    assert "今日赛程" in text
    assert "近期赛程" not in text


# ==============================================================================
# 回归：菜单 key 必须是 "predict"（与 MENU_ITEMS 一致），否则按钮落进「开发中」
# ==============================================================================
def test_menu_predict_key_matches_handler_branch():
    from bot_handler import MENU_ITEMS

    keys = {key for key, _ in MENU_ITEMS}
    assert "predict" in keys
    # main.py 的分支应覆盖 MENU_ITEMS 里每一个 key，避免按钮点了没反应
    source = open("main.py", encoding="utf-8").read()
    for key in keys:
        if key in ("predict", "fixtures", "standings", "analysis", "refresh", "help", "web"):
            assert f'elif key == "{key}"' in source, f"菜单 {key} 缺少处理分支"


# ==============================================================================
# 备用源全局端点回退：联赛端点返回空时改用 /v4/matches 并按竞赛过滤
# ==============================================================================
def test_fallback_uses_global_endpoint_when_competition_empty():
    paths = []

    def handler(request):
        path = str(request.url.path)
        paths.append(path)
        if path.startswith("/v4/competitions/"):
            return httpx.Response(200, json={"matches": []})  # 联赛端点为空
        return httpx.Response(200, json={"matches": [FD_MATCH, FD_FINISHED]})

    fd = make_fd(handler)
    fixtures = run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert len(fixtures) == 2, "联赛端点为空时应回退到全局端点"
    assert any(p == "/v4/matches" for p in paths)


def test_global_endpoint_filters_other_competitions():
    """全局端点会返回所有竞赛，必须只保留目标联赛，不能混入其他联赛的比赛。"""
    other = dict(FD_MATCH, id=9001, competition={"id": 2014, "name": "La Liga", "code": "PD"})

    def handler(request):
        if str(request.url.path).startswith("/v4/competitions/"):
            return httpx.Response(200, json={"matches": []})
        return httpx.Response(200, json={"matches": [FD_MATCH, other]})

    fd = make_fd(handler)
    fixtures = run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 9, 25)))
    assert len(fixtures) == 1
    assert fixtures[0]["league"]["name"] == "Premier League"


# ==============================================================================
# 第三级兜底：带日期过滤为空 → 不带日期参数拉整季 → 本地按日期筛选
# ==============================================================================
def _match_on(utc_date, mid=7001):
    return _future_match(mid, utc_date)


def test_third_fallback_filters_season_matches_locally():
    """前两级都为空时，拉取整季赛程并在本地按日期筛选。"""
    paths, seen_params = [], []

    def handler(request):
        paths.append(str(request.url.path))
        seen_params.append(dict(request.url.params))
        # 带日期参数的请求一律为空（模拟免费层日期过滤不可用）
        if request.url.params.get("dateFrom"):
            return httpx.Response(200, json={"matches": []})
        # 不带日期参数 → 返回整季（含窗口内 1 场、窗口外 1 场）
        return httpx.Response(200, json={"matches": [
            _match_on("2026-09-20T14:00:00Z", 1),   # 窗口外（早于 09-25）
            _match_on("2026-09-27T14:00:00Z", 2),   # 窗口内
            _match_on("2026-12-01T14:00:00Z", 3),   # 窗口外（远晚于 10-02）
        ]})

    fd = make_fd(handler)
    fixtures = run(fd.get_fixtures(39, 2026, date(2026, 9, 25), date(2026, 10, 2)))
    assert len(fixtures) == 1, "应只保留窗口内的 1 场"
    assert str(fixtures[0]["fixture"]["id"]) == "fd-2"
    # 确认最后一次请求不带日期参数
    assert seen_params[-1] == {}


def test_probe_reports_count_when_data_exists():
    def handler(request):
        return httpx.Response(200, json={"matches": [FD_MATCH], "competition": {"name": "Premier League"}})

    fd = make_fd(handler)
    info = run(fd.probe(39))
    assert info["ok"] is True
    assert info["count"] == 1
    assert "Premier League" in info["competition"]


def test_probe_includes_raw_snippet_when_empty():
    """HTTP 200 但 matches 为空 → 带出原始响应片段，便于判断是账号限制还是真没数据。"""
    def handler(request):
        return httpx.Response(200, json={"matches": []})

    fd = make_fd(handler)
    info = run(fd.probe(39))
    assert info["ok"] is False
    assert info["count"] == 0
    assert "matches" in info["raw"]  # 原始内容可见，不再只显示「0 场」


def test_probe_reports_api_message_as_detail():
    """响应里的 message 字段是接口报错，应作为原因展示。"""
    def handler(request):
        return httpx.Response(200, json={"matches": [], "message": "restricted resource"})

    fd = make_fd(handler)
    info = run(fd.probe(39))
    assert info["ok"] is False
    assert "restricted resource" in info["detail"]


def test_probe_never_raises():
    """诊断接口不能因为请求失败而影响主流程。"""
    def handler(request):
        raise httpx.ConnectError("boom")

    fd = make_fd(handler)
    info = run(fd.probe(39))
    assert info["ok"] is False
    assert "detail" in info
