"""额度账本：数出「一天真实请求数」（scripts/quota_ledger.py）。

用法：python3 scripts/quota_ledger.py
不联网、不需要数据源密钥（网络层已替换为本地假响应）。

v2 暴露了一个方法论问题：进程内一轮接一轮跑，TTL 都没过期，稳态恒为 0，
测不出真实占用。真实情况是一天里缓存会反复过期。

v3 的做法（可复核，不猜）：
  1. 只 mock 网络层（_request），客户端 TTL 缓存真实生效——与生产一致；
  2. 记录每个端点 **逻辑调用次数/轮** 与 **涉及的缓存键个数**；
  3. 按各任务的真实调度频率折算到一天，再用 TTL 上限封顶：
       每天请求 = min(逻辑调用/轮 × 每天轮次, 缓存键数 × 86400/TTL)
     两个约束缺一不可：前者是访问频次，后者是缓存能把频次压到多低。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import types
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# 以脚本位置定位仓库根目录，保证 `python3 scripts/quota_ledger.py` 从任意
# 工作目录都能导入顶层模块（顶层模块之间是直接 import 的）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

TMPDIR = tempfile.mkdtemp(prefix="quota_ledger_")
os.environ["DATABASE_PATH"] = os.path.join(TMPDIR, "q.db")
os.environ["DATA_DIR"] = TMPDIR
os.environ["FOOTBALL_DATA_ENABLED"] = "false"
os.environ["PREMATCH_ENABLED"] = "1"
os.environ["PREMATCH_REFRESH_MINUTES"] = "15"
os.environ["PREMATCH_LINEUP_WINDOW"] = "3600"
os.environ["PREMATCH_REFRESH_CAP"] = "10"
os.environ["LOG_LEVEL"] = "ERROR"
os.environ.setdefault("TELEGRAM_TOKEN", "dummy-token")
os.environ.setdefault("API_FOOTBALL_KEY", "dummy-key")

import api_client  # noqa: E402
import config  # noqa: E402
import scheduler  # noqa: E402
from service import PredictionService  # noqa: E402

NOW = datetime.now(timezone.utc)
LEAGUES = (39, 40, 61, 71, 78, 88, 94, 135, 140, 2)
FIX: list[tuple] = []
_fid = 1000
for lg in LEAGUES:
    for i in range(3):
        FIX.append((_fid, lg, NOW + timedelta(minutes=20 + i * 90)))
        _fid += 1
    for i in range(35):
        FIX.append((_fid, lg, NOW + timedelta(hours=25 + i * 17)))
        _fid += 1


def mk(fid, lg, k):
    return {
        "fixture": {"id": fid, "date": k.isoformat(), "timestamp": int(k.timestamp()),
                    "status": {"short": "NS", "long": "Not Started"},
                    "venue": {"id": 1, "name": "V"}, "referee": None},
        "league": {"id": lg, "name": f"L{lg}", "country": "X", "season": 2025,
                   "round": "Regular Season - 1"},
        "teams": {"home": {"id": fid * 2, "name": f"H{fid}", "logo": "", "winner": None},
                  "away": {"id": fid * 2 + 1, "name": f"A{fid}", "logo": "", "winner": None}},
        "goals": {"home": None, "away": None},
        "score": {"halftime": {"home": None, "away": None},
                  "fulltime": {"home": None, "away": None},
                  "extratime": {"home": None, "away": None},
                  "penalty": {"home": None, "away": None}},
    }


TTL = {
    "fixtures": api_client.TTL_FIXTURES,
    "standings": api_client.TTL_STANDINGS,
    "odds": api_client.TTL_ODDS,
    "injuries": api_client.TTL_INJURIES,
    "fixtures/lineups": api_client.TTL_LINEUPS,
    "players/squads": api_client.TTL_SQUAD,
    "players": api_client.TTL_PLAYER_STATS,
    "leagues/seasons": api_client.TTL_SEASONS,
    "fixtures/headtohead": api_client.TTL_H2H,
    "fixtures/statistics": api_client.TTL_STATS,
}


class NetStub(api_client.FootballAPI):
    def __init__(self, *a, **kw):
        kw["api_key"] = kw.get("api_key", "dummy")
        super().__init__(*a, **kw)
        self.net: Counter = Counter()
        self.logical: Counter = Counter()
        self.keys: dict[str, set] = defaultdict(set)

    async def _get(self, path, params, ttl=0.0, fresh=False):
        self.logical[path] += 1
        self.keys[path].add(tuple(sorted((k, str(v)) for k, v in params.items())))
        return await super()._get(path, params, ttl=ttl, fresh=fresh)

    async def _request(self, path, params):
        self.net[path] += 1
        if path == "fixtures":
            if "league" in params:
                return [mk(f, l, k) for f, l, k in FIX if l == int(params["league"])]
            return [mk(f, l, k) for f, l, k in FIX]
        if path == "standings":
            return [{"league": {"standings": [[
                {"rank": i + 1, "team": {"id": 1000 + i, "name": f"T{i}"},
                 "points": 60 - i, "goalsDiff": 10 - i,
                 "all": {"played": 20, "win": 10, "draw": 2, "lose": 8,
                         "goals": {"for": 30, "against": 25}},
                 "home": {"played": 10, "win": 6, "draw": 1, "lose": 3,
                          "goals": {"for": 18, "against": 12}},
                 "away": {"played": 10, "win": 4, "draw": 1, "lose": 5,
                          "goals": {"for": 12, "against": 13}},
                 "form": "WWDLW"} for i in range(20)]]}}]
        if path == "leagues/seasons":
            return [{"year": y} for y in (2025, 2024, 2023)]
        if path == "status":
            return {"requests": {"limit_day": 7500, "remaining": 7500}}
        return []


def main() -> int:
    settings = config.load_settings()
    api = NetStub(api_key="dummy", provider="rapidapi")
    svc = PredictionService(settings, api)
    app = types.SimpleNamespace(bot_data={"service": svc, "settings": settings})
    ctx = types.SimpleNamespace(application=app)

    print(f"合成赛程 {len(FIX)} 场 / {len(LEAGUES)} 联赛；赛前窗口内 30 场")
    print()

    # 冷启动：填满缓存，同时记录各端点的缓存键
    asyncio.run(scheduler.sync_job(ctx))
    asyncio.run(scheduler.auto_predict_job(ctx))
    asyncio.run(svc.prematch_refresh())
    print(f"冷启动首轮真实请求：{sum(api.net.values())} 次")
    for m, c in sorted(api.net.items(), key=lambda x: -x[1]):
        print(f"      {m:<20} {c}")

    # 稳态一轮：分别测三个任务的逻辑调用增量（缓存命中不计入 net）
    def snap():
        return (Counter(api.logical), {k: len(v) for k, v in api.keys.items()},
                Counter(api.net))

    l0, k0, n0 = snap()
    asyncio.run(scheduler.sync_job(ctx))
    l1, k1, n1 = snap()
    d_sync = l1 - l0
    asyncio.run(scheduler.auto_predict_job(ctx))
    l2, k2, n2 = snap()
    d_auto = l2 - l1
    asyncio.run(svc.prematch_refresh())
    l3, k3, n3 = snap()
    d_pm = l3 - l2

    print()
    print("── 稳态一轮的逻辑调用（缓存命中也算调用，但未发请求）──")
    for label, d in (("同步", d_sync), ("自动预测", d_auto), ("赛前刷新", d_pm)):
        print(f"  {label}: {sum(d.values())} 次 → " +
              ", ".join(f"{m}×{c}" for m, c in sorted(d.items(), key=lambda x: -x[1])))

    sync_h = getattr(__import__("sync"), "SYNC_INTERVAL_HOURS", 6)
    auto_h = scheduler.AUTO_PREDICT_INTERVAL_HOURS
    pm_min = int(settings.prematch_refresh_minutes)
    runs = {"同步": 24 / sync_h, "自动预测": 24 / auto_h, "赛前刷新": 24 * 60 / pm_min}

    print()
    print("=" * 74)
    print(f"{'端点':<20}{'键数':>6}{'逻辑/天':>10}{'TTL上限/天':>12}{'取小值':>10}")
    print("-" * 74)
    per_job_day: dict[str, float] = {}
    for label, d in (("同步", d_sync), ("自动预测", d_auto), ("赛前刷新", d_pm)):
        job_total = 0.0
        for path, cnt in d.items():
            if not cnt:
                continue
            keys = len(api.keys.get(path, ()))
            ttl = TTL.get(path, 0)
            logical_day = cnt * runs[label]
            ttl_cap = keys * (86400 / ttl) if ttl else logical_day
            day = min(logical_day, ttl_cap)
            job_total += day
            print(f"{path:<20}{keys:>6}{logical_day:>10.0f}{ttl_cap:>12.0f}{day:>10.0f}")
        per_job_day[label] = job_total
    print("-" * 74)
    for label, v in per_job_day.items():
        print(f"{label:<12} 每天 {v:8.0f}    （{runs[label]:.0f} 轮/天）")
    grand = sum(per_job_day.values())
    print("-" * 74)
    print(f"{'稳态合计':<12} 每天 {grand:8.0f} / 7500   占 {grand / 7500 * 100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
