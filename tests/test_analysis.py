"""阶段七：📊 深度分析 + 🏆 联赛排名（不联网，使用仿真数据）。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from api_client import APIError, flatten_standings
from bot_handler import NO_DATA, BotUI
from config import load_settings
from service import PredictionService, form_stats, h2h_stats
from tests.sample_data import FakeAPI, fixture, standings_response

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


def now_fixture(fid=1001, home_id=1, away_id=2, hours=5):
    return fixture(fid, home_id, f"主队{home_id}", away_id, f"客队{away_id}",
                   datetime.now(timezone.utc) + timedelta(hours=hours), status="NS")


def finished_match(team_id, opponent_id, our, their, is_home=True, days_ago=3):
    """构造一场已完场的比赛（用于近期战绩 / 历史交锋）。"""
    home_id, away_id = (team_id, opponent_id) if is_home else (opponent_id, team_id)
    hg, ag = (our, their) if is_home else (their, our)
    return {
        "fixture": {
            "id": abs(hash((team_id, opponent_id, days_ago))) % 100000,
            "date": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
            "status": {"short": "FT"},
        },
        "league": {"id": 39, "name": "PL", "round": "Regular Season - 1"},
        "teams": {"home": {"id": home_id, "name": f"T{home_id}"}, "away": {"id": away_id, "name": f"T{away_id}"}},
        "goals": {"home": hg, "away": ag},
    }


def standings_with_points():
    """带 points / all 字段的积分榜（更接近真实 API 结构）。"""
    def row(team_id, rank, points, w, d, l, gf, ga):
        return {
            "rank": rank,
            "team": {"id": team_id, "name": f"T{team_id}"},
            "points": points,
            "goalsDiff": gf - ga,
            "all": {"played": w + d + l, "win": w, "draw": d, "lose": l, "goals": {"for": gf, "against": ga}},
            "home": {"played": w, "win": w, "draw": 0, "lose": 0, "goals": {"for": gf // 2, "against": ga // 2}},
            "away": {"played": d + l, "win": 0, "draw": d, "lose": l, "goals": {"for": gf // 2, "against": ga // 2}},
        }
    return [row(1, 1, 18, 6, 0, 1, 16, 5), row(2, 4, 11, 3, 2, 2, 10, 9)]


class AnalysisAPI(FakeAPI):
    def __init__(self, fixtures=None, standings=None, form=None, h2h=None, standings_error=None):
        super().__init__({}, form=form or {}, h2h=h2h if h2h is not None else [])
        self.items = fixtures if fixtures is not None else [now_fixture()]
        self.standings_rows = standings if standings is not None else flatten_standings(standings_response())
        self.standings_error = standings_error

    async def get_fixtures(self, league_id, season, date_from, date_to):
        return self.items

    async def get_standings(self, league_id, season):
        if self.standings_error:
            raise self.standings_error
        return self.standings_rows


# ---- 统计函数 -----------------------------------------------------------------
def test_form_stats_counts_results_and_goals():
    matches = [
        finished_match(1, 9, 2, 1, is_home=True, days_ago=1),   # 胜
        finished_match(1, 8, 1, 1, is_home=False, days_ago=5),  # 平
        finished_match(1, 7, 0, 2, is_home=True, days_ago=9),   # 负
    ]
    stats = form_stats(matches, 1)
    assert stats["played"] == 3
    assert (stats["win"], stats["draw"], stats["lose"]) == (1, 1, 1)
    assert stats["goals_for"] == 3 and stats["goals_against"] == 4
    assert len(stats["matches"]) == 3


def test_form_stats_home_away_split():
    matches = [
        finished_match(1, 9, 2, 0, is_home=True),   # 主场胜
        finished_match(1, 8, 0, 1, is_home=False),  # 客场负
    ]
    stats = form_stats(matches, 1)
    assert stats["home"]["win"] == 1 and stats["away"]["lose"] == 1


def test_form_stats_ignores_unfinished():
    """未开赛 / 进行中的比赛不能算进近期状态。"""
    pending = finished_match(1, 9, 3, 0)
    pending["fixture"]["status"]["short"] = "NS"
    stats = form_stats([pending], 1)
    assert stats["played"] == 0
    assert stats["matches"] == []


def test_form_stats_empty_is_zero_not_fabricated():
    stats = form_stats([], 1)
    assert stats["played"] == 0 and stats["avg_for"] is None


def test_h2h_stats_from_home_perspective():
    matches = [
        finished_match(1, 2, 2, 1, is_home=True),   # 主队视角胜
        finished_match(2, 1, 3, 0, is_home=True),   # 1 是客队，0-3 负
        finished_match(1, 2, 1, 1, is_home=False),  # 平
    ]
    stats = h2h_stats(matches, 1)
    assert stats["played"] == 3
    assert (stats["win"], stats["draw"], stats["lose"]) == (1, 1, 1)
    assert stats["goals_for"] == 3 and stats["goals_against"] == 5


# ---- 报告聚合 -----------------------------------------------------------------
def test_analyze_fixture_returns_full_report():
    form = {1: [finished_match(1, 9, 2, 0, is_home=True)], 2: [finished_match(2, 8, 1, 1, is_home=False)]}
    api = AnalysisAPI(form=form, h2h=[finished_match(1, 2, 2, 1, is_home=True)])
    svc = PredictionService(SETTINGS, api)
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    assert report["home"] and report["away"]
    assert report["home_form"]["played"] == 1
    assert report["away_form"]["played"] == 1
    assert report["h2h"]["played"] == 1
    assert report["home_row"] is not None and report["away_row"] is not None
    assert report["errors"] == {"home_form": None, "away_form": None, "h2h": None}


def test_analyze_fixture_missing_fixture_raises_key_error():
    svc = PredictionService(SETTINGS, AnalysisAPI())
    with pytest.raises(KeyError):
        run(svc.analyze_fixture(9999, [now_fixture()]))


def test_analyze_fixture_standings_error_propagates_real_reason():
    """积分榜取不到（套餐/赛季）必须抛真实错误，不能静默降级。"""
    api = AnalysisAPI(standings_error=APIError("Free plans do not have access to this season"))
    svc = PredictionService(SETTINGS, api)
    with pytest.raises(APIError) as exc:
        run(svc.analyze_fixture(1001, [now_fixture()]))
    assert "season" in str(exc.value).lower()


def test_analyze_fixture_form_failure_keeps_real_reason():
    """近期战绩属于可选数据：失败不阻断，但要保留真实原因，不能写成“没有数据”。"""

    class BoomForm(AnalysisAPI):
        async def get_team_form(self, team_id, season, last=5):
            raise APIError("Rate limit reached")

    svc = PredictionService(SETTINGS, BoomForm())
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    assert report["errors"]["home_form"] and "Rate limit" in report["errors"]["home_form"]
    assert report["home_form"]["played"] == 0


# ---- 报告排版：缺数据必须明说，不得虚构 ----------------------------------------------
def test_deep_report_contains_all_sections():
    form = {1: [finished_match(1, 9, 2, 0, is_home=True)], 2: [finished_match(2, 8, 1, 1, is_home=False)]}
    api = AnalysisAPI(form=form, h2h=[finished_match(1, 2, 2, 1, is_home=True)], standings=standings_with_points())
    svc = PredictionService(SETTINGS, api)
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    text = BotUI.format_deep_report(report, SETTINGS.timezone)
    for section in ("深度分析", "近期状态", "联赛信息", "历史交锋", "模型因素", "分析总结", "模型倾向"):
        assert section in text, section


def test_deep_report_no_form_data_shows_no_data():
    """没有近期比赛：显示「暂无可靠数据」，不得编造战绩。"""
    api = AnalysisAPI(form={}, h2h=[], standings=standings_with_points())
    svc = PredictionService(SETTINGS, api)
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    text = BotUI.format_deep_report(report, SETTINGS.timezone)
    assert "暂无可靠数据" in text
    assert NO_DATA.strip("。")[:8] in text


def test_deep_report_no_h2h_shows_no_data():
    api = AnalysisAPI(h2h=[], standings=standings_with_points())
    svc = PredictionService(SETTINGS, api)
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    text = BotUI.format_deep_report(report, SETTINGS.timezone)
    assert "暂无可靠数据" in text


def test_deep_report_no_team_data_marks_incomplete():
    """积分榜里没有这两队 → 数据完整性为「无数据」，分析总结归入不确定。"""
    api = AnalysisAPI(standings=[])
    svc = PredictionService(SETTINGS, api)
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    assert report["has_team_data"] is False
    text = BotUI.format_deep_report(report, SETTINGS.timezone)
    assert "无数据" in text
    assert "不确定" in text


def test_deep_report_shows_error_reason_when_optional_data_fails():
    class BoomForm(AnalysisAPI):
        async def get_team_form(self, team_id, season, last=5):
            raise APIError("Rate limit reached")

    svc = PredictionService(SETTINGS, BoomForm(standings=standings_with_points()))
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    text = BotUI.format_deep_report(report, SETTINGS.timezone)
    assert "获取失败" in text and "Rate limit" in text


def test_deep_report_has_no_forbidden_claims():
    api = AnalysisAPI(standings=standings_with_points())
    svc = PredictionService(SETTINGS, api)
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    text = BotUI.format_deep_report(report, SETTINGS.timezone)
    for banned in ("必胜", "稳赢", "100%准确", "确定中奖"):
        assert banned not in text


def test_deep_report_includes_injury_note():
    api = AnalysisAPI(standings=standings_with_points())
    svc = PredictionService(SETTINGS, api)
    report = run(svc.analyze_fixture(1001, [now_fixture()]))
    text = BotUI.format_deep_report(report, SETTINGS.timezone)
    assert "伤停" in text


# ---- 联赛排名 ------------------------------------------------------------------
def test_standings_page_formats_rows():
    rows = standings_with_points()
    text = BotUI.format_standings_page(rows, SETTINGS.timezone, league_label="英超 · 2026")
    assert "联赛排名" in text
    assert "T1" in text and "18分" in text
    assert "6-0-1" in text


def test_standings_page_empty_shows_no_data():
    text = BotUI.format_standings_page([], SETTINGS.timezone)
    assert "暂无可靠数据" in text


def test_standings_row_missing_points_does_not_crash():
    """缺 points / all 字段的行不能让机器人崩溃，用「-」占位。"""
    rows = [{"rank": 7, "team": {"id": 5, "name": "缺字段队"}}]
    text = BotUI.format_standings_page(rows, SETTINGS.timezone)
    assert "缺字段队" in text
    assert "None" not in text


def test_standings_respects_limit():
    rows = standings_with_points() * 5
    text = BotUI.format_standings_page(rows, SETTINGS.timezone, limit=3)
    assert text.count("T1") + text.count("T2") <= 3


# ---- handler 集成 --------------------------------------------------------------
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


def test_fa_callback_renders_deep_report():
    api = AnalysisAPI(standings=standings_with_points())
    ctx, _ = make_ctx(api, fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("fa:1001")
    run(main.on_analysis_fixture(update, ctx))
    assert q.edits and "深度分析" in q.edits[0][0]


def test_fa_callback_unknown_fixture_shows_hint():
    ctx, _ = make_ctx(AnalysisAPI(), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("fa:44444")
    run(main.on_analysis_fixture(update, ctx))
    assert q.edits and "不在今日赛程" in q.edits[0][0]


def test_fa_callback_shows_api_error():
    api = AnalysisAPI(standings_error=APIError("Free plans do not have access to this season"))
    ctx, _ = make_ctx(api, fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("fa:1001")
    run(main.on_analysis_fixture(update, ctx))
    text = q.edits[0][0]
    assert "生成深度分析失败" in text and "套餐" in text


def test_fa_callback_duplicate_click_blocked():
    ctx, _ = make_ctx(AnalysisAPI(standings=standings_with_points()), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("fa:1001")
    run(main.on_analysis_fixture(update, ctx))
    ctx.application.bot_data.setdefault("pending", set()).add((555, "analysis:1001"))
    update2, q2 = q_update("fa:1001")
    run(main.on_analysis_fixture(update2, ctx))
    assert len(q2.edits) == 0


def test_fa_callback_unexpected_error_does_not_crash():
    class Broken(AnalysisAPI):
        async def get_standings(self, league_id, season):
            raise RuntimeError("boom")

    ctx, _ = make_ctx(Broken(), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("fa:1001")
    run(main.on_analysis_fixture(update, ctx))
    assert q.edits and "生成深度分析失败" in q.edits[0][0]


def test_menu_standings_renders_table():
    ctx, _ = make_ctx(AnalysisAPI(standings=standings_with_points()))
    update, q = q_update("menu:standings")
    run(main.on_menu(update, ctx))
    assert q.edits and "联赛排名" in q.edits[0][0]
    assert "18分" in q.edits[0][0]


def test_menu_standings_shows_api_error():
    api = AnalysisAPI(standings_error=APIError("Free plans do not have access to this season"))
    ctx, _ = make_ctx(api)
    update, q = q_update("menu:standings")
    run(main.on_menu(update, ctx))
    assert "获取联赛排名失败" in q.edits[0][0] and "套餐" in q.edits[0][0]


def test_menu_analysis_shows_fixtures_to_choose():
    """深度分析入口：先列出赛程让用户选比赛。"""
    ctx, _ = make_ctx(AnalysisAPI(), fx_cache=None)
    update, q = q_update("menu:analysis")
    run(main.on_menu(update, ctx))
    assert q.edits and "今日赛程" in q.edits[0][0]


def test_standings_highlights_top_three():
    """前三名必须是「队名+积分」卡片并带副行，不能和其余行一样挤成表格。"""
    rows = [
        {"team": {"name": f"T{i}"}, "rank": i, "points": 30 - i * 2,
         "all": {"win": 10 - i, "draw": 0, "lose": i}}
        for i in range(1, 6)
    ]
    text = BotUI.format_standings_page(rows, SETTINGS.timezone, league_label="英超 · 2026")
    assert "🥇" in text and "🥈" in text and "🥉" in text
    assert "28分" in text  # 榜首积分
    assert "└" in text  # 前三副行战绩
    # 前三之外不再用奖牌，改用紧凑序号
    assert text.count("🥇") == 1


def test_standings_omits_bare_unit_when_points_missing():
    """没有积分时不能渲染出孤立的「分」字。"""
    rows = [{"team": {"name": "T1"}, "rank": 1, "points": None, "all": {}}]
    text = BotUI.format_standings_page(rows, SETTINGS.timezone)
    assert "-分" not in text
