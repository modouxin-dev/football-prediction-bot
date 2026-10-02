"""多联赛建模层测试：每场比赛只能用它所属联赛的积分榜建模。

核心保证（防串味）：
    西甲的比赛绝不能拿英超积分榜去算攻防强度，否则强度完全失真。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from config import load_settings
from service import PredictionService

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}


def run(coro):
    return asyncio.run(coro)


def settings_of(league_ids=None, league_id=39):
    env = dict(ENV)
    env["LEAGUE_ID"] = str(league_id)
    if league_ids is not None:
        env["LEAGUE_IDS"] = ",".join(str(i) for i in league_ids)
    return load_settings(env)


def fx(fid, league_id, home_id, away_id, hours):
    """一场未开赛的比赛，明确带上所属联赛 ID。"""
    kickoff = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {
        "fixture": {"id": fid, "date": kickoff.isoformat(),
                    "status": {"short": "NS"}, "venue": {"name": "G"}},
        "league": {"id": league_id, "name": f"L{league_id}", "round": "Regular Season - 1"},
        "teams": {"home": {"id": home_id, "name": f"H{home_id}"},
                  "away": {"id": away_id, "name": f"A{away_id}"}},
    }


def standings_of(league_id, *team_ids):
    def row(team_id, rank):
        return {
            "rank": rank,
            "team": {"id": team_id, "name": f"T{team_id}"},
            "points": 30 - rank * 3,
            "goalsDiff": 10 - rank,
            "all": {"played": 10, "win": 5, "draw": 2, "lose": 3,
                    "goals": {"for": 15 - rank, "against": 10 + rank}},
            "home": {"played": 5, "win": 3, "draw": 1, "lose": 1,
                     "goals": {"for": 8, "against": 5}},
            "away": {"played": 5, "win": 2, "draw": 1, "lose": 2,
                     "goals": {"for": 7, "against": 6}},
        }
    rows = [row(t, i + 1) for i, t in enumerate(team_ids)]
    return [{"league": {"id": league_id, "name": f"L{league_id}",
                        "season": 2026, "standings": [rows]}}]


class RecordingAPI:
    """记录 get_standings / get_fixtures 收到哪些联赛 ID。"""

    def __init__(self, fixtures_by_league=None, standings_by_league=None,
                 fail_leagues=(), season_by_league=None):
        self._fx = fixtures_by_league or {}
        self._st = standings_by_league or {}
        self._fail = set(fail_leagues)
        self._season_by_league = season_by_league or {}
        self.standings_calls: list[tuple[int, int]] = []
        self.fixtures_calls: list[int] = []
        self.source_label = "api-football"

    async def get_fixtures(self, league_id, season, date_from, date_to):
        lid = int(league_id)
        self.fixtures_calls.append(lid)
        if lid in self._fail:
            from api_client import APIError
            raise APIError(f"联赛 {lid} 不可用")
        return list(self._fx.get(lid, []))

    async def get_standings(self, league_id, season):
        lid = int(league_id)
        self.standings_calls.append((lid, int(season)))
        if lid in self._fail:
            from api_client import APIError
            raise APIError(f"联赛 {lid} 无积分榜")
        return list(self._st.get(lid, standings_of(lid, 1, 2, 3, 4)))

    async def get_odds(self, fixture_id, fresh=False):
        return []

    async def get_team_form(self, team_id, season, limit):
        return []

    async def get_h2h(self, home_id, away_id, limit):
        return []


# ---- 联赛归属识别 -------------------------------------------------------------
def test_league_id_of_uses_fixture_league():
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    assert svc._league_id_of(fx(1, 140, 10, 20, 5)) == 140


def test_league_id_of_falls_back_to_primary():
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    bare = {"fixture": {"id": 1}, "league": {}, "teams": {}}
    assert svc._league_id_of(bare) == 39


def test_league_id_of_tolerates_bad_value():
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    bad = {"fixture": {"id": 1}, "league": {"id": "abc"}, "teams": {}}
    assert svc._league_id_of(bad) == 39


# ---- 建模不串味 ---------------------------------------------------------------
def test_build_predictions_models_each_league_with_own_standings():
    """西甲比赛只能用西甲积分榜：get_standings 必须按比赛所属联赛分别调用。"""
    api = RecordingAPI(
        fixtures_by_league={39: [fx(1, 39, 1, 2, 3)], 140: [fx(2, 140, 10, 20, 5)]},
    )
    svc = PredictionService(settings_of([39, 140]), api)
    preds = run(svc.build_predictions(lookahead_hours=48, limit=10))

    assert len(preds) == 2
    called = {lid for lid, _ in api.standings_calls}
    assert called == {39, 140}  # 两个联赛各自的积分榜都被取过


def test_build_predictions_reuses_standings_within_league():
    """同一联赛多场比赛，积分榜只请求一次（省配额）。"""
    api = RecordingAPI(fixtures_by_league={
        39: [fx(1, 39, 1, 2, 3), fx(3, 39, 3, 4, 6)],
        140: [fx(2, 140, 10, 20, 5)],
    })
    svc = PredictionService(settings_of([39, 140]), api)
    run(svc.build_predictions(lookahead_hours=48, limit=10))

    assert api.standings_calls.count((39, 2026)) == 1
    assert api.standings_calls.count((140, 2026)) == 1


def test_build_predictions_single_league_unchanged():
    """单联赛配置下行为与旧版一致：只请求主联赛积分榜。"""
    api = RecordingAPI(fixtures_by_league={39: [fx(1, 39, 1, 2, 3)]})
    svc = PredictionService(settings_of(None), api)
    run(svc.build_predictions(lookahead_hours=48))

    assert [lid for lid, _ in api.standings_calls] == [39]
    assert api.fixtures_calls == [39]


def test_predict_fixture_uses_own_league_standings():
    api = RecordingAPI(fixtures_by_league={140: [fx(2, 140, 10, 20, 5)]})
    svc = PredictionService(settings_of([39, 140]), api)
    run(svc.predict_fixture(2, [fx(2, 140, 10, 20, 5)]))

    assert [lid for lid, _ in api.standings_calls] == [140]


def test_analyze_fixture_uses_own_league_standings():
    api = RecordingAPI(fixtures_by_league={140: [fx(2, 140, 10, 20, 5)]})
    svc = PredictionService(settings_of([39, 140]), api)
    report = run(svc.analyze_fixture(2, [fx(2, 140, 10, 20, 5)]))

    assert [lid for lid, _ in api.standings_calls] == [140]
    assert report["league"] == "L140"


# ---- 多联赛赛程合并 -----------------------------------------------------------
def test_fetch_multi_merges_and_sorts_by_kickoff():
    api = RecordingAPI(fixtures_by_league={
        39: [fx(1, 39, 1, 2, 9)],
        140: [fx(2, 140, 10, 20, 2), fx(3, 140, 30, 40, 6)],
    })
    svc = PredictionService(settings_of([39, 140]), api)
    fixtures, season, note = run(svc._fetch_fixtures_multi(None, None))

    assert [f["fixture"]["id"] for f in fixtures] == [2, 3, 1]  # 按开赛时间升序
    assert season == 2026


def test_fetch_multi_one_league_failure_does_not_kill_others():
    api = RecordingAPI(fixtures_by_league={39: [fx(1, 39, 1, 2, 3)]}, fail_leagues={140})
    svc = PredictionService(settings_of([39, 140]), api)
    fixtures, _, _ = run(svc._fetch_fixtures_multi(None, None))

    assert [f["fixture"]["id"] for f in fixtures] == [1]  # 西甲挂了，英超照常


def test_fetch_multi_all_failure_raises_real_reason():
    from api_client import APIError
    api = RecordingAPI(fail_leagues={39, 140})
    svc = PredictionService(settings_of([39, 140]), api)
    try:
        run(svc._fetch_fixtures_multi(None, None))
    except APIError as exc:
        assert "39" in str(exc)
    else:
        raise AssertionError("全部联赛失败时应抛出真实原因")


def test_fetch_multi_records_season_per_league():
    """各联赛解析出的赛季分别记录，建模时按联赛取，不能统一套主联赛的。"""
    api = RecordingAPI(fixtures_by_league={39: [fx(1, 39, 1, 2, 3)],
                                           140: [fx(2, 140, 10, 20, 5)]})
    svc = PredictionService(settings_of([39, 140]), api)
    run(svc._fetch_fixtures_multi(None, None))

    assert svc._season_by_league.get(39) == 2026
    assert svc._season_by_league.get(140) == 2026
