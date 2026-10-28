"""额度账本实测：把每个定时任务一天发多少次数据请求真数出来。

方法：用一个"计数替身"替换 FootballAPI 的全部接口方法，让真实
PredictionService / sync 逻辑照常跑，统计每个任务一轮的请求数。
再乘任务的真实调度频率，得到一天的账本。

不猜、不估算：所有数字来自本脚本实跑。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from collections import Counter
from datetime import datetime, timedelta, timezone

TMPDIR = tempfile.mkdtemp(prefix="quotaledger_")
os.environ["DATABASE_PATH"] = os.path.join(TMPDIR, "q.db")
os.environ["DATA_DIR"] = TMPDIR
os.environ["DATA_SOURCE_MODE"] = "primary"     # 只走主源，排除备用源干扰
os.environ["FOOTBALL_DATA_ENABLED"] = "false"
os.environ["PREMATCH_REFRESH_MINUTES"] = "15"
os.environ["LOG_LEVEL"] = "ERROR"
# 账本只测请求次数，不真连数据源；缺这两个变量会导致 load_settings 直接拒绝。
os.environ.setdefault("TELEGRAM_TOKEN", "dummy-token-for-quota-ledger")
os.environ.setdefault("API_FOOTBALL_KEY", "dummy-key-for-quota-ledger")

import _tgstub  # noqa: E402,F401  （最小 telegram 替身，见文件内说明）

import config  # noqa: E402
import scheduler  # noqa: E402
from service import PredictionService  # noqa: E402

NOW = datetime.now(timezone.utc)
LEAGUES = (39, 40, 61, 71, 78, 88, 94, 135, 140, 2)
PER_LEAGUE = 38          # 30 天窗口内每联赛场次（项目实测 380 场 / 10 联赛）
KICKOFF_SPAN_DAYS = 30


def mk_fixture(fid: int, league: int, kickoff: datetime):
    return {
        "fixture": {
            "id": fid,
            "date": kickoff.isoformat(),
            "timestamp": int(kickoff.timestamp()),
            "status": {"short": "NS", "long": "Not Started"},
            "venue": {"id": 1, "name": "V"},
            "referee": None,
        },
        "league": {
            "id": league, "name": f"L{league}", "country": "X",
            "season": 2025, "round": "Regular Season - 1",
        },
        "teams": {
            "home": {"id": fid * 2, "name": f"H{fid}", "logo": "", "winner": None},
            "away": {"id": fid * 2 + 1, "name": f"A{fid}", "logo": "", "winner": None},
        },
        "goals": {"home": None, "away": None},
        "score": {"halftime": {"home": None, "away": None},
                  "fulltime": {"home": None, "away": None},
                  "extratime": {"home": None, "away": None},
                  "penalty": {"home": None, "away": None}},
    }


ALL_FIXTURES: list[dict] = []
_fid = 1000
# 现实形态：10 个联赛里，当天各 3 场（共 30 场，落在赛前刷新窗口内），
# 其余 350 场均匀铺满 30 天窗口。
for lg in LEAGUES:
    for i in range(3):
        k = NOW + timedelta(minutes=20 + i * 7 * 60)
        ALL_FIXTURES.append(mk_fixture(_fid, lg, k))
        _fid += 1
    for i in range(PER_LEAGUE - 3):
        k = NOW + timedelta(hours=25 + i * (KICKOFF_SPAN_DAYS * 24 / PER_LEAGUE))
        ALL_FIXTURES.append(mk_fixture(_fid, lg, k))
        _fid += 1


class CountingAPI:
    """计数替身：每个方法调用 = 一次真实数据请求。"""

    def __init__(self) -> None:
        self.calls: Counter = Counter()

    def _c(self, name):
        self.calls[name] += 1

    # ---- 赛程 ----
    async def get_fixtures(self, league_id, season, date_from, date_to):
        self._c("get_fixtures")
        return [f for f in ALL_FIXTURES if f["league"]["id"] == int(league_id)]

    async def get_fixtures_by_date(self, day):
        self._c("get_fixtures_by_date")
        return ALL_FIXTURES

    async def get_fixtures_by_season(self, league_id, season):
        self._c("get_fixtures_by_season")
        return [f for f in ALL_FIXTURES if f["league"]["id"] == int(league_id)]

    async def get_live_fixtures(self):
        self._c("get_live_fixtures")
        return []

    # ---- 单场附加数据 ----
    async def get_injuries(self, fixture_id):
        self._c("get_injuries")
        return []

    async def get_lineups(self, fixture_id):
        self._c("get_lineups")
        return []

    async def get_squad(self, team_id):
        self._c("get_squad")
        return []

    async def get_team_player_stats(self, team_id, season):
        self._c("get_team_player_stats")
        return []

    async def get_standings(self, league_id, season):
        self._c("get_standings")
        return []

    async def get_odds(self, fixture_id, fresh=False):
        self._c("get_odds")
        return []

    async def get_h2h(self, home_id, away_id, last=5):
        self._c("get_h2h")
        return []

    async def get_team_form(self, team_id, season, last=5):
        self._c("get_team_form")
        return []

    async def get_fixture_statistics(self, fixture_id):
        self._c("get_fixture_statistics")
        return []

    async def get_available_seasons(self):
        self._c("get_available_seasons")
        return [2025, 2024, 2023]

    async def get_account_status(self):
        self._c("get_account_status")
        return {"requests": {"limit_day": 7500, "remaining": 7500}}

    async def aclose(self):
        pass

    @property
    def quota_left(self):
        return 7500

    @property
    def quota_exhausted(self):
        return False


def build_service():
    settings = config.load_settings()
    api = CountingAPI()
    svc = PredictionService(settings, api)
    return settings, api, svc


def run(coro):
    return asyncio.run(coro)


def measure(name: str, coro_factory, api: CountingAPI):
    before = api.calls.copy()
    try:
        run(coro_factory())
    except Exception as exc:  # noqa: BLE001
        print(f"  [{name}] 抛出异常（不影响计数）：{type(exc).__name__}: {exc}")
    after = api.calls.copy()
    delta = after - before
    total = sum(delta.values())
    return total, delta


def main() -> int:
    settings, api, svc = build_service()
    app = types.SimpleNamespace(bot_data={"service": svc, "settings": settings})
    ctx = types.SimpleNamespace(application=app)

    print(f"合成赛程：{len(ALL_FIXTURES)} 场 / {len(LEAGUES)} 联赛 / "
          f"{KICKOFF_SPAN_DAYS} 天窗口")
    print()

    jobs = [
        ("sync_job（赛程同步）", lambda: scheduler.sync_job(ctx)),
        ("auto_predict_job（自动预测）", lambda: scheduler.auto_predict_job(ctx)),
        ("prematch_job（赛前刷新）", lambda: scheduler.prematch_job(ctx)),
        ("history_job（历史回填）", lambda: scheduler.history_job(ctx)),
        ("settle_job（赛果结算）", lambda: scheduler.settle_job(ctx)),
    ]

    # 每天运行次数（来自 scheduler.py 的注册参数）
    per_day = {
        "sync_job（赛程同步）": 24 / __import__("sync").SYNC_INTERVAL_HOURS,
        "auto_predict_job（自动预测）": 24 / scheduler.AUTO_PREDICT_INTERVAL_HOURS,
        "prematch_job（赛前刷新）": 24 * 60 / int(settings.prematch_refresh_minutes),
        "history_job（历史回填）": 24 / scheduler.HISTORY_INTERVAL_HOURS,
        "settle_job（赛果结算）": 24 / 6,
    }

    results = []
    for name, factory in jobs:
        total, delta = measure(name, factory, api)
        results.append((name, total, delta))
        print(f"{name}: 一轮 {total} 次")
        for m, c in sorted(delta.items(), key=lambda x: -x[1]):
            print(f"    {m}: {c}")
        print()

    print("=" * 62)
    print(f"{'任务':<28}{'一轮':>8}{'每天次数':>10}{'每天请求':>12}")
    print("-" * 62)
    grand = 0
    for name, total, _ in results:
        n = per_day[name]
        day = total * n
        grand += day
        print(f"{name:<28}{total:>8}{n:>10.0f}{day:>12.0f}")
    print("-" * 62)
    print(f"{'合计':<28}{'':>8}{'':>10}{grand:>12.0f}")
    print(f"{'免费套餐上限':<28}{'':>8}{'':>10}{7500:>12}")
    print(f"{'超出':<28}{'':>8}{'':>10}{grand - 7500:>12.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
