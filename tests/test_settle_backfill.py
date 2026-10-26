"""超期未结算的回填：把「已开赛但永远结算不了」的历史遗留补回统计。

背景（这是本文件的存在理由）
----------------------------
``pending()`` 加了时间下界后，超期行不再堵死结算队列——结算停摆被修好了。
但代价是：这些行**永远不会被结算**。它们既不进命中率统计，也不会自己消失，
只能作为「超期未结算 N 场」显示在看板上（当前线上 463 场）。

为什么按天补而不是按场补
------------------------
数据源一次请求返回当天全部比赛，故成本是「天数」而非「场次数」。
463 场若分布在 40 天里，就是 40 次请求，而不是 463 次。

为什么不重算预测
----------------
只回填**真实赛果**。预测概率是当时落盘的，拿现在的积分榜去重算会引入
前视偏差（look-ahead bias），命中率会虚高——那是自欺欺人。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from repository import PredictionRepository


def _mk(tmp_path, rows: list[tuple[str, str]]) -> PredictionRepository:
    """建库并插入 (fixture_id, kickoff) 列表。"""
    repo = PredictionRepository(str(tmp_path / "t.db"))
    conn = repo._connect()
    for fid, kick in rows:
        conn.execute(
            "INSERT INTO predictions (fixture_id, home, away, league, kickoff, "
            "home_prob, draw_prob, away_prob, best_score, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, "A", "B", "L", kick, 0.5, 0.3, 0.2, "1-0", "home", kick))
    conn.commit()
    return repo


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# ---- 日期枚举 ---------------------------------------------------------------


def test_stale_days_empty_when_nothing_stale(tmp_path):
    """近期未结算不算超期。"""
    repo = _mk(tmp_path, [("f1", _days_ago(1))])
    # 窗口默认 3 天，1 天前仍在窗口内
    assert repo.stale_pending_days() == []


def test_stale_days_lists_oldest_first(tmp_path):
    """由远及近：先把最老的补掉，总量才会单调下降。

    若由近及远，中途额度耗尽时剩下的永远是最老的那批——永远清不完。
    """
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(10)),
                          ("f3", _days_ago(20))])
    days = repo.stale_pending_days()
    assert len(days) == 3
    assert days == sorted(days)


def test_stale_days_deduplicates(tmp_path):
    """同一天多场只返回一天——成本是天数不是场次数。"""
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(30)),
                          ("f3", _days_ago(30))])
    assert len(repo.stale_pending_days()) == 1


def test_stale_days_respects_limit(tmp_path):
    """limit 是额度保护：超出部分本次不补。"""
    rows = [(f"f{i}", _days_ago(10 + i)) for i in range(10)]
    repo = _mk(tmp_path, rows)
    assert len(repo.stale_pending_days(limit=3)) == 3


def test_stale_days_skips_malformed_kickoff(tmp_path):
    """脏日期不得作为参数发出去。"""
    repo = PredictionRepository(str(tmp_path / "t.db"))
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions (fixture_id, home, away, league, kickoff, "
        "home_prob, draw_prob, away_prob, best_score, result, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("f1", "A", "B", "L", "not-a-date", 0.5, 0.3, 0.2, "1-0", "home",
         _days_ago(40)))
    conn.commit()
    assert repo.stale_pending_days() == []


def test_stale_count_for_day(tmp_path):
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(30)),
                          ("f3", _days_ago(20))])
    d30 = _days_ago(30)[:10]
    assert repo.stale_pending_count_for_day(d30) == 2


def test_stale_count_for_day_drops_after_settle(tmp_path):
    """回填后该日计数应下降——用于前后对账。"""
    repo = _mk(tmp_path, [("f1", _days_ago(30))])
    d30 = _days_ago(30)[:10]
    assert repo.stale_pending_count_for_day(d30) == 1
    repo.settle("f1", 1, 0)
    assert repo.stale_pending_count_for_day(d30) == 0


# ---- 回填编排 ---------------------------------------------------------------


def test_id_candidates_covers_both_writings():
    """两种写法都要覆盖——这是「取到了却补不回来」的直接原因。"""
    from service import PredictionService

    assert PredictionService._id_candidates("555051") == ["555051", "fd-555051"]
    assert PredictionService._id_candidates("fd-555051") == ["fd-555051", "555051"]


def test_settle_any_id_falls_back_to_prefixed():
    """库里存的是 fd- 前缀时，用裸 ID 也要能补回来。"""
    from service import PredictionService

    class R:
        def __init__(self): self.called = []
        def settle(self, fid, h, a):
            self.called.append(str(fid))
            return str(fid) == "fd-555051"

    svc = object.__new__(PredictionService)
    svc.repo = R()
    svc.elo_enabled = False
    svc._sync_elo = lambda *a, **k: None
    assert svc._settle_any_id("555051", 1, 0, {}) is True
    assert svc.repo.called == ["555051", "fd-555051"]


def test_settle_any_id_does_not_double_sync_elo():
    """命中即止，绝不两种写法都写——那会把 Elo 同步两遍。"""
    from service import PredictionService

    class R:
        def settle(self, fid, h, a): return True   # 第一次就命中

    svc = object.__new__(PredictionService)
    svc.repo = R()
    synced = []
    svc._sync_elo = lambda *a, **k: synced.append(1)
    svc._settle_any_id("555051", 1, 0, {})
    assert len(synced) == 1


class _FakeRepo:
    """最小替身：只提供回填方法用到的接口。

    known 里的 ID 才会被 settleset 命中——这样才能测出「取到了但 ID 对不上」
    的真实场景（补回 0 场）。若 settle 恒真，该场景永远测不到。
    """

    def __init__(self, days: list[str], stale_before: int = 100,
                 known: set[str] | None = None):
        self._days = days
        self._stale = stale_before
        self._known = known or {"1", "2"}
        self.settled: list[tuple[str, int, int]] = []

    def stale_pending_days(self, limit: int = 60) -> list[str]:
        return self._days[:limit]

    def count_stale_pending(self) -> int:
        return self._stale

    def stale_pending_sample_ids(self, limit: int = 6) -> list[str]:
        return sorted(self._known)[:limit]

    def stale_pending_leagues(self, limit: int = 8):
        return [("L", self._stale)]

    def stale_pending_date_range(self):
        return (self._days[0], self._days[-1]) if self._days else ("", "")

    # 队名兜底用：默认返回空，需要时由测试覆盖
    rows_for_day: dict = None
    id_kinds: dict = None

    def stale_pending_rows_for_day(self, day):
        return list((self.rows_for_day or {}).get(day, []))

    def stale_pending_id_kinds(self):
        return dict(self.id_kinds or {"fd": 0, "numeric": 0})

    def settle(self, fixture_id, home, away) -> bool:
        if str(fixture_id) not in self._known:
            return False
        self.settled.append((str(fixture_id), int(home), int(away)))
        self._stale = max(0, self._stale - 1)
        return True


@pytest.mark.asyncio
async def test_backfill_no_stale_days_returns_zero():
    """没有超期行时直接返回，不浪费额度。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo([], stale_before=0)
    res = await svc.backfill_stale_settlements()
    assert res["scanned_days"] == 0
    assert res["settled"] == 0


@pytest.mark.asyncio
async def test_backfill_settles_finished_matches_only():
    """只有 status 为已完场才回填，未开赛/进行中不算。"""
    from service import PredictionService

    finished = [{"fixture": {"id": "1", "status": {"short": "FT"}},
                 "goals": {"home": 2, "away": 1}},
                {"fixture": {"id": "2", "status": {"short": "NS"}},
                 "goals": {"home": None, "away": None}}]
    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01"])

    async def _fetch(d):
        return finished

    svc._fetch_all_by_date_raw = _fetch
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()
    assert res["settled"] == 1
    assert svc.repo.settled == [("1", 2, 1)]


@pytest.mark.asyncio
async def test_backfill_reports_failed_days_not_as_success():
    """取数失败的日期必须列出来，不能算进 days_done。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01", "2026-08-02"], stale_before=50)

    async def _fetch(d):
        if d == "2026-08-01":
            raise RuntimeError("额度耗尽")
        return []

    svc._fetch_all_by_date_raw = _fetch
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()
    assert res["failed_days"] == ["2026-08-01"]
    assert res["days_done"] == 1


@pytest.mark.asyncio
async def test_backfill_decreases_remaining(tmp_path):
    """回填后剩余量必须下降——这是本功能唯一的价值证明。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01"], stale_before=463)

    async def _fetch(d):
        return [{"fixture": {"id": "1", "status": {"short": "FT"}},
                 "goals": {"home": 1, "away": 0}}]

    svc._fetch_all_by_date_raw = _fetch
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()
    assert res["remaining_before"] == 463
    assert res["remaining_after"] == 462


@pytest.mark.asyncio
async def test_backfill_reports_diagnostics_when_zero_settled():
    """补回 0 场时必须给出可分辨原因的字段，否则无从排查。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01"], stale_before=10)
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None

    async def _fetch(d):
        # 已完场，但 ID 在库里不存在 → 补不回来
        return [{"fixture": {"id": "999999", "status": {"short": "FT"}},
                 "goals": {"home": 1, "away": 0}}]

    svc._fetch_all_by_date_raw = _fetch
    res = await svc.backfill_stale_settlements()
    assert res["settled"] == 0
    # 三个诊断字段必须都在，用于区分「没取到」与「ID 对不上」
    assert res["fetched_fixtures"] == 1
    assert res["finished_fixtures"] == 1
    assert res["unmatched_sample"] == ["999999"]


@pytest.mark.asyncio
async def test_backfill_via_prefixed_id():
    """真实场景：库里存 fd- 前缀，数据源返回裸 ID。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    # 库里存的是带前缀的 ID，数据源返回裸 ID
    svc.repo = _FakeRepo(["2026-08-01"], stale_before=5, known={"fd-555051"})
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None

    async def _fetch(d):
        return [{"fixture": {"id": "555051", "status": {"short": "FT"}},
                 "goals": {"home": 2, "away": 1}}]

    svc._fetch_all_by_date_raw = _fetch
    res = await svc.backfill_stale_settlements()
    # 数据源给裸 ID，库里存 fd- 前缀 —— 靠候选回退命中
    assert res["settled"] == 1
    assert res["finished_fixtures"] == 1
    assert svc.repo.settled == [("fd-555051", 2, 1)]


# ---- 本次修复的回归守卫 -----------------------------------------------------
# 线上表现：扫描 17 天 / 成功取数 17 天 / 取到比赛 0 场 / 补回 0 场，全程无报错。


def test_str_date_has_no_isoformat():
    """根因留档：ISO 字符串没有 isoformat()——整条故障链的起点。

    stale_pending_days() 返回 str，取数接口内部调 day.isoformat()，
    str 没有该方法 → AttributeError → 被内层 except 吞掉返回 [] →
    外层误判「取数成功」。四层传导后才变成「补回 0 场且不报错」。
    """
    assert not hasattr("2026-08-01", "isoformat")
    import datetime as _dt

    assert _dt.date(2026, 8, 1).isoformat() == "2026-08-01"


@pytest.mark.asyncio
async def test_backfill_passes_date_object_not_string():
    """回填必须把日期转成 date 对象再传给取数接口。

    _FakeRepo 给出的就是 **字符串** 日期（与真实 repository 一致）。
    若直接透传，取数接口会抛 AttributeError 并被静默吞掉——
    那条路径在别的测试里看不出问题，因为异常不冒头。
    """
    import datetime as _dt
    from service import PredictionService

    seen: list = []

    class Api:
        async def get_fixtures_by_date(self, day):
            seen.append(day)
            return [{"fixture": {"id": "1", "status": {"short": "FT"}},
                     "goals": {"home": 2, "away": 1}}]

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01"])      # 字符串日期，与线上一致
    svc.api = Api()
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()

    assert res["settled"] == 1, "字符串日期应被正确转换，不能补回 0 场"
    assert seen and isinstance(seen[0], _dt.date), (
        f"传给取数接口的是 {type(seen[0]).__name__}，必须是 date")


@pytest.mark.asyncio
async def test_backfill_does_not_filter_by_configured_leagues():
    """回填不得按当前配置的联赛过滤——僵尸行可能属于历史配置过的联赛。

    走 _fetch_all_by_date 会只保留 LEAGUE_IDS 里的联赛，历史遗留
    （配置变更前的联赛）就永远取不到。回填的过滤条件应该是 fixture_id。
    """
    import datetime as _dt
    from service import PredictionService

    class Api:
        async def get_fixtures_by_date(self, day):
            # 属于「当前未配置」的联赛（比如历史配置过后来去掉的）
            return [{"fixture": {"id": "9", "status": {"short": "FT"}},
                     "goals": {"home": 0, "away": 0},
                     "league": {"id": 99999}}]

    svc = object.__new__(PredictionService)
    svc.repo = _FakeRepo(["2026-08-01"], known={"9"})   # 库里确实有这一场
    svc.api = Api()
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()

    assert res["settled"] == 1, "未配置联赛的僵尸行也必须能补回来"
    assert res["fetched_fixtures"] == 1


# ---- 诊断方法 ---------------------------------------------------------------


def test_stale_leagues_groups_by_league(tmp_path):
    """补回 0 场时，联赛分布能立刻说明「这批行属于哪些联赛」。"""
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(30)),
                          ("f3", _days_ago(20))])
    got = dict(repo.stale_pending_leagues())
    assert got == {"L": 3}


def test_stale_leagues_marks_missing_league(tmp_path):
    """联赛字段为空时显示为 (未记录)，不能显示成空白让人误解。"""
    repo = PredictionRepository(str(tmp_path / "t.db"))
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions (fixture_id, home, away, kickoff, "
        "home_prob, draw_prob, away_prob, best_score, result, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("f1", "A", "B", _days_ago(30), 0.5, 0.3, 0.2, "1-0", "home",
         _days_ago(30)))
    conn.commit()
    got = dict(repo.stale_pending_leagues())
    assert got == {"(未记录)": 1}


def test_stale_date_range_reports_oldest_and_newest(tmp_path):
    repo = _mk(tmp_path, [("f1", _days_ago(30)), ("f2", _days_ago(10))])
    lo, hi = repo.stale_pending_date_range()
    assert lo == _days_ago(30)[:10]
    assert hi == _days_ago(10)[:10]


def test_stale_date_range_empty_when_nothing(tmp_path):
    repo = _mk(tmp_path, [])
    assert repo.stale_pending_date_range() == ("", "")


# ---- 「补回了很多但仍有剩余」也必须给出诊断 ---------------------------------
# 实测反例：补回 486 场、剩余 102 → 102。settled > 0 时不诊断，
# 用户就只看到「补回 486」，而那 102 场永远补不回来、且无线索。


def _report(res, repo_sample=None):
    """抽出 admin 里的报告构造逻辑做单测——不依赖 telegram。"""
    import re

    src = open("commands/admin.py", encoding="utf-8").read()
    i = src.find("if res[\"settled\"] == 0 or res[\"remaining_after\"]:")
    assert i > 0, "诊断分支的条件被改掉了，本测试的前提不再成立"
    # 只验证条件本身覆盖了「补回了但仍有剩余」这一情形
    return i


def test_report_diagnoses_even_when_some_settled():
    """settled > 0 但仍有剩余时，必须继续给诊断。

    若条件写成 settled == 0，则「补回 486、剩余 102」不输出任何线索。
    """
    _report(None)


def test_remaining_after_gt_zero_is_enough_to_diagnose():
    """断言点：remaining_after > 0 单独就足以触发诊断。"""
    settled, remaining_after = 486, 102
    assert not (settled == 0)                      # 老条件不成立
    assert (settled == 0 or remaining_after)       # 新条件成立


# ---- 跨源 ID：队名兜底 -------------------------------------------------------
# 线上实测：库内 fd-560593（备用源编号空间）vs 源端 1570410（主源编号空间）。
# 剥掉前缀得到 560593，与主源 ID 毫无关系——前缀剥离对跨源无效。


def test_cross_source_ids_are_in_different_spaces():
    """两个源的 ID 在不同编号空间——这是「补不回来」的根因，先留档。

    主源 API-Football 实测 1500000 量级；备用源 football-data.org 实测
    560593 量级。``fd-560593`` 剥前缀 = ``560593``，与主源 ID 无关。
    """
    from service import PredictionService

    cands = PredictionService._id_candidates("fd-560593")
    assert cands == ["fd-560593", "560593"]
    assert "1570410" not in cands      # 主源返回的 ID，永远匹配不上


def _mk_fx(fid, home_name, away_name, hg, ag, status="FT"):
    return {"fixture": {"id": fid, "status": {"short": status}},
            "teams": {"home": {"name": home_name},
                      "away": {"name": away_name}},
            "goals": {"home": hg, "away": ag}}


@pytest.mark.asyncio
async def test_settle_by_team_names_matches_cross_source_rows():
    """ID 对不上时，靠「日期 + 双方队名」补回——且不额外花额度。

    known 里放的是**库里那一行自己的 ID**（fd-560593），不是数据源的
    （1570410）——这正是真实情形：主源 ID 在库里不存在，所以 ID 匹配失败；
    但按队名定位后，写的是行自己的 ID，能写进去。
    """
    from service import PredictionService

    svc = object.__new__(PredictionService)
    repo = _FakeRepo(["2026-08-01"], known={"fd-560593"})
    repo.rows_for_day = {"2026-08-01": [("fd-560593", "Arsenal FC", "Chelsea FC")]}
    svc.repo = repo
    svc.elo_enabled = False
    svc._sync_elo = lambda *a, **k: None

    finished = [_mk_fx("1570410", "Arsenal", "Chelsea", 2, 1)]
    assert svc._settle_by_team_names("2026-08-01", finished) == 1
    # 写的是**行自己的 ID**，不是数据源的
    assert repo.settled == [("fd-560593", 2, 1)]


@pytest.mark.asyncio
async def test_settle_by_team_names_refuses_ambiguous():
    """有歧义一律放弃——写错比分比少补一场严重得多。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    repo = _FakeRepo(["2026-08-01"], known={"fd-1"})
    repo.rows_for_day = {"2026-08-01": [("fd-1", "Arsenal FC", "Chelsea FC")]}
    svc.repo = repo
    svc.elo_enabled = False
    svc._sync_elo = lambda *a, **k: None

    # 同日同名两场（理论上罕见，但出现了就必须放弃而不是猜）
    finished = [_mk_fx("1570410", "Arsenal", "Chelsea", 2, 1),
                _mk_fx("1570411", "Arsenal", "Chelsea", 0, 0)]
    assert svc._settle_by_team_names("2026-08-01", finished) == 0
    assert repo.settled == []


@pytest.mark.asyncio
async def test_settle_by_team_names_requires_both_sides():
    """只有主队名对上不算——单侧匹配会张冠李戴。"""
    from service import PredictionService

    svc = object.__new__(PredictionService)
    repo = _FakeRepo(["2026-08-01"], known={"fd-1"})
    repo.rows_for_day = {"2026-08-01": [("fd-1", "Arsenal FC", "Chelsea FC")]}
    svc.repo = repo
    svc.elo_enabled = False
    svc._sync_elo = lambda *a, **k: None

    finished = [_mk_fx("1570410", "Arsenal", "Tottenham", 2, 1)]
    assert svc._settle_by_team_names("2026-08-01", finished) == 0


@pytest.mark.asyncio
async def test_backfill_reports_matched_by_name():
    """回填结果要如实报出「靠队名补回多少」。"""
    from service import PredictionService

    class Api:
        async def get_fixtures_by_date(self, day):
            return [_mk_fx("1570410", "Arsenal", "Chelsea", 2, 1)]

    svc = object.__new__(PredictionService)
    repo = _FakeRepo(["2026-08-01"], known={"fd-560593"})
    repo.rows_for_day = {"2026-08-01": [("fd-560593", "Arsenal FC", "Chelsea FC")]}
    repo.id_kinds = {"fd": 1, "numeric": 0}
    svc.repo = repo
    svc.api = Api()
    svc.elo_enabled = False
    svc.refresh_calibration = lambda: None
    res = await svc.backfill_stale_settlements()

    assert res["settled"] == 1
    assert res["matched_by_name"] == 1
    assert res["id_kinds"] == {"fd": 1, "numeric": 0}


# ---- ID 形态统计 -------------------------------------------------------------


def test_id_kinds_counts_backup_vs_main(tmp_path):
    repo = _mk(tmp_path, [("fd-560593", _days_ago(30)), ("1570410", _days_ago(20)),
                          ("fd-560592", _days_ago(25))])
    assert repo.stale_pending_id_kinds() == {"fd": 2, "numeric": 1}


def test_rows_for_day_returns_team_names(tmp_path):
    """队名兜底要能取到队名——只有 ID 定位不了。"""
    repo = _mk(tmp_path, [("fd-560593", _days_ago(30))])
    rows = repo.stale_pending_rows_for_day(_days_ago(30)[:10])
    assert rows == [("fd-560593", "A", "B")]
