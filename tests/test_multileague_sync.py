"""多联赛同步层测试：逐联赛拉取、按联赛分区保存、单联赛失败不拖垮整体。

核心保证（防串味）：
    西甲的比赛只能存进西甲分区，绝不能存进英超分区。
"""
import asyncio
from datetime import date

from sync import MatchSync


def run(coro):
    return asyncio.run(coro)


class FakeSettings:
    def __init__(self, league_id, league_ids=None):
        self.league_id = league_id
        if league_ids is None:
            # 老配置：根本没有 league_ids 字段
            self.__dict__.pop("league_ids", None)
        else:
            self.league_ids = league_ids


class FakeSettingsNoAttr:
    """模拟升级前老版本 Settings：没有 league_ids 字段。"""

    def __init__(self, league_id):
        self.league_id = league_id


class FakeService:
    def __init__(self, league_id, league_ids, fixtures_by_league=None,
                 season_fixtures=None, fail_leagues=()):
        self.settings = FakeSettings(league_id, league_ids)
        self.source_label = "api-football"
        self._fixtures_by_league = fixtures_by_league or {}
        self._season_fixtures = season_fixtures or {}
        self._fail_leagues = set(fail_leagues)
        self.calls: list[tuple] = []

    async def _fetch_fixtures(self, date_from, date_to, league_id=None):
        lid = self.settings.league_id if league_id is None else int(league_id)
        self.calls.append(("window", lid))
        if lid in self._fail_leagues:
            raise RuntimeError(f"联赛 {lid} 不可用")
        return list(self._fixtures_by_league.get(lid, [])), 2026, None

    class _API:
        def __init__(self, outer):
            self._outer = outer

        async def get_fixtures_by_season(self, league_id, season):
            self._outer.calls.append(("season", int(league_id), season))
            if int(league_id) in self._outer._fail_leagues:
                raise RuntimeError(f"联赛 {league_id} 该赛季无数据")
            return list(self._outer._season_fixtures.get(int(league_id), []))

    @property
    def api(self):
        return self._API(self)


class FakeRepo:
    def __init__(self):
        self.saved: list[tuple[str, list]] = []
        self.logs: list[tuple] = []

    def save_matches(self, competition, matches, source="", http_status="", message=""):
        self.saved.append((competition, list(matches)))
        return len(matches)

    def log_sync(self, *args):
        self.logs.append(args)


def _fx(mid, name):
    return {"fixture": {"id": mid}, "teams": {"home": {"name": name}}}


# ---- 分区映射 -------------------------------------------------------------
def test_competitions_map_each_league_to_own_partition():
    svc = FakeService(39, (39, 140, 78))
    sync = MatchSync(svc, FakeRepo())
    assert sync.league_ids == (39, 140, 78)
    assert sync.competitions == ("PL", "PD", "BL1")


def test_competition_property_is_primary_league():
    svc = FakeService(39, (39, 140))
    sync = MatchSync(svc, FakeRepo())
    assert sync.competition == "PL"


def test_legacy_settings_without_league_ids_falls_back_to_single():
    """升级前老配置没有 league_ids 字段 → 退化为单联赛，行为不变。"""
    svc = FakeService(39, None)
    sync = MatchSync(svc, FakeRepo())
    assert sync.league_ids == (39,)
    assert sync.competitions == ("PL",)


# ---- 窗口同步 -------------------------------------------------------------
def test_window_single_league_keeps_original_shape():
    svc = FakeService(39, (39,), {39: [_fx(1, "A")]})
    repo = FakeRepo()
    res = run(MatchSync(svc, repo).sync_window(date(2026, 10, 1), date(2026, 10, 2)))

    assert res["competition"] == "PL"
    assert res["received"] == 1 and res["saved"] == 1
    assert res["ok"] is True
    assert [c[1] for c in svc.calls] == [39]


def test_window_multi_league_saves_into_own_partition():
    """防串味：西甲比赛存 PD，英超比赛存 PL。"""
    svc = FakeService(39, (39, 140),
                      {39: [_fx(1, "Arsenal"), _fx(2, "Chelsea")], 140: [_fx(3, "Barca")]})
    repo = FakeRepo()
    res = run(MatchSync(svc, repo).sync_window(date(2026, 10, 1), date(2026, 10, 2)))

    assert res["received"] == 3 and res["saved"] == 3
    saved_map = {comp: [m["fixture"]["id"] for m in ms] for comp, ms in repo.saved}
    assert saved_map == {"PL": [1, 2], "PD": [3]}
    assert len(res["per_league"]) == 2
    assert [i["league_id"] for i in res["per_league"]] == [39, 140]


def test_window_partial_failure_still_syncs_other_leagues():
    """单个联赛失败不拖垮整体：英超挂了，西甲照样入库。"""
    svc = FakeService(39, (39, 140), {140: [_fx(3, "Barca")]}, fail_leagues=(39,))
    repo = FakeRepo()
    res = run(MatchSync(svc, repo).sync_window(date(2026, 10, 1), date(2026, 10, 2)))

    assert res["saved"] == 1
    assert res["ok"] is True
    assert "部分联赛失败" in res["message"]
    assert [comp for comp, _ in repo.saved] == ["PD"]


def test_window_all_leagues_fail_marks_failure():
    svc = FakeService(39, (39, 140), fail_leagues=(39, 140))
    res = run(MatchSync(svc, FakeRepo()).sync_window(date(2026, 10, 1), date(2026, 10, 2)))

    assert res["ok"] is False
    assert res["saved"] == 0
    assert res["http_status"] == "RuntimeError"


def test_window_zero_fixtures_is_not_an_error():
    svc = FakeService(39, (39, 140))
    res = run(MatchSync(svc, FakeRepo()).sync_window(date(2026, 10, 1), date(2026, 10, 2)))

    assert res["received"] == 0
    assert res["ok"] is True
    assert "暂无比赛" in res["message"]


# ---- 赛季回填 -------------------------------------------------------------
def test_season_backfill_multi_league_partitions():
    svc = FakeService(39, (39, 140), season_fixtures={39: [_fx(1, "A")], 140: [_fx(2, "B")]})
    repo = FakeRepo()
    res = run(MatchSync(svc, repo).sync_season(2024))

    assert res["received"] == 2 and res["saved"] == 2 and res["ok"] is True
    saved_map = {comp: [m["fixture"]["id"] for m in ms] for comp, ms in repo.saved}
    assert saved_map == {"PL": [1], "PD": [2]}
    # 每个联赛都用自己的 league_id 请求，不带其他联赛的
    assert sorted((c[1], c[2]) for c in svc.calls) == [(39, 2024), (140, 2024)]


def test_season_backfill_empty_is_not_an_error():
    svc = FakeService(39, (39,))
    res = run(MatchSync(svc, FakeRepo()).sync_season(2024))
    assert res["ok"] is True
    assert "暂无数据" in res["message"]


def test_season_backfill_all_fail_marks_failure():
    svc = FakeService(39, (39, 140), fail_leagues=(39, 140))
    res = run(MatchSync(svc, FakeRepo()).sync_season(2024))
    assert res["ok"] is False
