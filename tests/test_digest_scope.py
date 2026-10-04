"""「今日赛程 N 场」与「今日预测汇总 M 场」不一致的根因回归测试。

用户实测现象：赛程页显示 4 场，预测汇总只给 3 场，且日期还跳到了第二天。

根因有两条，各自独立、都必须被守住：

    1. build_predictions 用 settings.max_matches 截断候选（默认 3），
       赛程页却不截断——同一批比赛两个数必然对不上，且截断是静默的。
    2. 赛程页按「自然日」取数（本地时区 00:00~23:59），
       预测汇总按「滚动小时窗口」取数（now ~ now+LOOKAHEAD_HOURS）。
       两套窗口不是同一批比赛，汇总标题因此跳到别的日期。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from config import load_settings
from service import PredictionService

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}


def run(coro):
    return asyncio.run(coro)


def settings_of(league_ids=(39,), **over):
    env = dict(ENV)
    env["LEAGUE_ID"] = str(league_ids[0])
    env["LEAGUE_IDS"] = ",".join(str(i) for i in league_ids)
    env.update(over)
    return load_settings(env)


def fx(fid, league_id, home_id, away_id, when, status="NS"):
    """when 为绝对 UTC 时间，便于精确构造「跨自然日」场景。"""
    return {
        "fixture": {"id": fid, "date": when.isoformat(),
                    "status": {"short": status}, "venue": {"name": "G"}},
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
    def __init__(self, fixtures_by_league=None, standings_by_league=None):
        self._fx = fixtures_by_league or {}
        self._st = standings_by_league or {}
        self.source_label = "api-football"

    async def get_fixtures(self, league_id, season, date_from, date_to):
        return list(self._fx.get(int(league_id), []))

    async def get_standings(self, league_id, season):
        lid = int(league_id)
        return list(self._st.get(lid, standings_of(lid, 1, 2, 3, 4)))

    async def get_odds(self, fixture_id, fresh=False):
        return []

    async def get_team_form(self, team_id, season, limit):
        return []

    async def get_h2h(self, home_id, away_id, limit):
        return []


# ---- 根因 1：候选被静默截断 ---------------------------------------------------
def test_predictions_not_silently_truncated_by_default_cap():
    """赛程 4 场 → 预测也必须是 4 场，不能被 max_matches 悄悄砍成 3 场。"""
    now = datetime.now(timezone.utc)
    fxs = [fx(i, 39, i * 10, i * 10 + 1, now + timedelta(hours=i + 1)) for i in range(1, 5)]
    svc = PredictionService(settings_of(), RecordingAPI(fixtures_by_league={39: fxs}))

    preds = run(svc.build_predictions(now=now, lookahead_hours=48))

    assert len(preds) == 4, (
        f"赛程 4 场却只返回 {len(preds)} 场：候选被 settings.max_matches 截断，"
        f"且没有任何提示，用户无法判断是机器人坏了还是真只有这么多"
    )


def test_default_max_matches_covers_a_full_matchday():
    """默认上限必须能覆盖一个比赛日的全部场次，五大联赛一天可达 30+ 场。"""
    s = settings_of()
    assert s.max_matches >= 50, (
        f"默认 max_matches={s.max_matches}，一个比赛日必然被截断"
    )


def test_truncation_is_visible_when_explicit_limit_given():
    """显式传 limit 截断时也必须留下提示，不能静默丢比赛。"""
    now = datetime.now(timezone.utc)
    fxs = [fx(i, 39, i * 10, i * 10 + 1, now + timedelta(hours=i + 1)) for i in range(1, 6)]
    svc = PredictionService(settings_of(), RecordingAPI(fixtures_by_league={39: fxs}))

    run(svc.build_predictions(now=now, lookahead_hours=48, limit=2))

    assert svc.last_note, "截断后必须给出可见提示，否则用户以为这就是全部比赛"


# ---- 根因 2：赛程按自然日、预测按滚动窗口 -------------------------------------
def test_pre_match_statuses_include_tbd():
    """TBD（时间待定）也是未开赛，只认 NS 会漏掉这类比赛。"""
    now = datetime.now(timezone.utc)
    svc = PredictionService(settings_of(), RecordingAPI())
    items = [
        (now + timedelta(hours=2), fx(1, 39, 1, 2, now + timedelta(hours=2), status="TBD")),
    ]
    kept = PredictionService._upcoming(
        [it[1] for it in items], now, now + timedelta(hours=48)
    )
    assert len(kept) == 1, "TBD 属于未开赛，应纳入预测候选"


def test_finished_matches_never_enter_predictions():
    now = datetime.now(timezone.utc)
    svc = PredictionService(settings_of(), RecordingAPI())
    items = [fx(1, 39, 1, 2, now - timedelta(hours=1), status="FT")]
    kept = PredictionService._upcoming(items, now, now + timedelta(hours=48))
    assert kept == [], "已完场的比赛不能进预测候选"


# ---- 根因 2（续）：自然日窗口 vs 滚动窗口 -------------------------------------
def test_day_window_covers_local_calendar_day():
    """自然日窗口必须是本地 00:00~23:59:59，换算到 UTC 后跨日也要正确。"""
    import pytz
    tz = pytz.timezone("Asia/Shanghai")
    start, end = PredictionService._day_window(__import__("datetime").date(2026, 10, 4), tz)

    assert start.astimezone(tz).date().isoformat() == "2026-10-04"
    assert end.astimezone(tz).date().isoformat() == "2026-10-04"
    assert start.astimezone(tz).hour == 0 and start.astimezone(tz).minute == 0
    assert end.astimezone(tz).hour == 23 and end.astimezone(tz).minute == 59
    # 上海 10-04 00:00 对应 UTC 10-03 16:00：两端都必须落在正确的 UTC 日
    assert start.date().isoformat() == "2026-10-03"


def test_predictions_for_day_matches_fixtures_page_scope():
    """给同一天取数，预测候选必须与赛程页看到的是同一批比赛。

    赛程页按自然日展示「明天凌晨那场」，预测按滚动窗口就会漏掉它，
    于是汇总标题跳到下一天——这正是用户实测到的现象。
    """
    import pytz
    from datetime import date as _date, time as _time

    tz = pytz.timezone("Asia/Shanghai")
    # 构造本地 10-04 的三场比赛：00:30（UTC 10-03 16:30）、21:45、23:30
    def local(day, h, m):
        return tz.localize(datetime(2026, 10, day, h, m)).astimezone(timezone.utc)

    fxs = [
        fx(1, 39, 10, 11, local(4, 0, 30)),
        fx(2, 39, 12, 13, local(4, 21, 45)),
        fx(3, 39, 14, 15, local(4, 23, 30)),
        fx(4, 39, 16, 17, local(5, 20, 0)),  # 明天，不该进来
    ]
    svc = PredictionService(
        settings_of(TIMEZONE="Asia/Shanghai"),
        RecordingAPI(fixtures_by_league={39: fxs}),
    )

    preds = run(svc.build_predictions(day=_date(2026, 10, 4)))

    assert len(preds) == 3, (
        f"本地 10-04 共 3 场，按自然日窗口应返回 3 场，实际 {len(preds)} 场"
    )
    assert {p.fixture_id for p in preds} == {1, 2, 3}


def test_digest_entry_uses_calendar_day_window():
    """汇总入口必须传 day=，否则窗口对不齐的回退会静默复活。

    只测 service 层不够：main 里改成 build_predictions() 一样能跑通全部
    service 测试。必须钉住「调用方确实按自然日取数」这一层。
    """
    import main

    seen = {}

    class SpyService:
        settings = load_settings(dict(ENV, LEAGUE_ID="39", LEAGUE_IDS="39",
                                      TIMEZONE="Asia/Shanghai"))
        last_note = None

        async def build_predictions(self, **kwargs):
            seen.update(kwargs)
            return []

    run(main._build_digest_predictions(SpyService()))

    assert "day" in seen, (
        "汇总入口没有按自然日取数，会退回滚动小时窗口，"
        "导致赛程页与汇总日期、场数对不上"
    )
