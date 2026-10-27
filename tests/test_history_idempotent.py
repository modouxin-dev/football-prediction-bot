"""历史赛季回填幂等测试：已结束的赛季本地有数据就不再回源。

为什么必须有这个测试（缺陷是什么）：
    history_job 每 24 小时对「全部赛季 × 全部联赛」各发一次整季请求。
    但历史赛季的赛果不会再变——第二轮拉回来的是一模一样的数据。
    实测 5 联赛 × 10 赛季：一轮 51 次请求，第二轮新增 0 场，100% 空转。
    容器重启还会额外触发一轮（first=600s），空转次数随之翻倍。

    这些请求全落在主源 7500/天的额度上，是额度被提前打光的一个确定来源。

断言的是**真实请求数**，不是文案：
    只断言 message 的话，改个措辞测试就绿了，而请求照样在发。
"""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from repository import PredictionRepository
from sync import MatchSync, _season_is_closed, _season_window


def run(coro):
    return asyncio.run(coro)


def _opening_season(today: date) -> int:
    """当前所处的开赛年（与 _season_is_closed 同一口径）。"""
    return today.year if today.month >= 7 else today.year - 1


def _fixtures(season: int, lid: int = 39, n: int = 3) -> list[dict]:
    """构造一个赛季的赛果。

    fixture id 必须全局唯一：matches 主键是 id，重复 id 会命中
    ON CONFLICT(id) DO UPDATE 而**不改** competition_code，
    数据会全落进第一个分区，让「本地已有数据」的判定失真。
    """
    from tests.sample_data import fixture

    out = []
    for i in range(n):
        fx = fixture(lid * 100000 + season * 100 + i, 1, "主", 2, "客",
                     datetime(season, 8, 20 + i, tzinfo=timezone.utc), status="FT")
        fx["league"]["season"] = season
        fx["league"]["id"] = lid
        fx["goals"] = {"home": 1, "away": 0}
        out.append(fx)
    return out


class CountingAPI:
    """只数真实请求数，不做任何节流。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.source_label = "api-football"

    async def get_fixtures_by_season(self, league_id: int, season: int):
        self.calls.append((int(league_id), int(season)))
        return _fixtures(season, int(league_id))


def _build(api, tmp_path, league_ids=(39,)):
    repo = PredictionRepository(str(tmp_path / "h.db"))
    service = SimpleNamespace(
        api=api,
        settings=SimpleNamespace(league_id=league_ids[0], league_ids=league_ids),
        source_label=api.source_label,
    )
    return MatchSync(service, repo), repo


# ---- 幂等：第二轮不再发请求 ----------------------------------------------

def test_closed_season_second_round_sends_no_request(tmp_path):
    """已结束赛季回填过一次后，第二轮一次请求都不发。"""
    api = CountingAPI()
    sync, _ = _build(api, tmp_path)
    closed = _opening_season(date.today()) - 1  # 上一个赛季，必定已结束

    r1 = run(sync.sync_season(closed))
    n1 = len(api.calls)
    assert r1["saved"] > 0, "第一轮必须先真的存进本地库"

    api.calls.clear()
    r2 = run(sync.sync_season(closed))
    assert len(api.calls) == 0, (
        f"已结束赛季第二轮不该再发请求，实际发了 {len(api.calls)} 次（第一轮 {n1} 次）")
    assert r2["skipped"] == 1
    assert r2["ok"] is True


def test_closed_season_skip_is_not_reported_as_failure(tmp_path):
    """跳过不能被汇总成失败——否则看板上「省额度」看起来像任务挂了。"""
    api = CountingAPI()
    sync, _ = _build(api, tmp_path)
    closed = _opening_season(date.today()) - 1

    run(sync.sync_season(closed))
    r2 = run(sync.sync_season(closed))

    assert r2["ok"] is True
    assert "跳过" in r2["message"], f"跳过原因必须写清楚，实际：{r2['message']!r}"
    # 关键：不能误报成「套餐不支持」，那是完全不同的问题
    assert "套餐不支持" not in r2["message"]


def test_closed_season_without_local_data_still_fetches(tmp_path):
    """已结束但本地还没有数据 → 必须正常回源，不能因为「已结束」就永远不拉。"""
    api = CountingAPI()
    sync, _ = _build(api, tmp_path)
    closed = _opening_season(date.today()) - 1

    r = run(sync.sync_season(closed))
    assert len(api.calls) == 1
    assert r["saved"] > 0
    assert r.get("skipped") == 0


def test_current_season_never_skipped(tmp_path):
    """进行中的赛季绝不跳过：它每轮都可能产生新赛果。"""
    api = CountingAPI()
    sync, _ = _build(api, tmp_path)
    current = _opening_season(date.today())

    run(sync.sync_season(current))
    api.calls.clear()
    r2 = run(sync.sync_season(current))

    assert len(api.calls) == 1, (
        "当前赛季第二轮必须继续回源（还有新赛果），跳过会让本地库停在过去")
    assert r2.get("skipped") == 0


def test_multi_league_all_skipped_on_second_round(tmp_path):
    """多联赛场景：第二轮全部联赛整季跳过，请求数从 51 降到 0。"""
    leagues = (39, 140, 135, 78, 61)
    api = CountingAPI()
    sync, _ = _build(api, tmp_path, leagues)
    closed = _opening_season(date.today()) - 1

    run(sync.sync_season(closed))
    n1 = len(api.calls)
    assert n1 == len(leagues)

    api.calls.clear()
    r2 = run(sync.sync_season(closed))
    assert len(api.calls) == 0, f"多联赛第二轮应 0 请求，实际 {len(api.calls)}"
    assert r2["skipped"] == len(leagues)


# ---- 赛季结束判定 --------------------------------------------------------

def test_season_is_closed_boundary():
    """7 月是新赛季分界：此前算上赛季进行中，此后算上赛季已结束。"""
    # 2026-06：2025 赛季还没结束（跨年制到 6 月）
    assert _season_is_closed(2025, date(2026, 6, 30)) is False
    # 2026-07：2026 赛季已开，2025 赛季结束
    assert _season_is_closed(2025, date(2026, 7, 1)) is True
    # 当前赛季永远不算结束
    assert _season_is_closed(2026, date(2026, 7, 1)) is False
    assert _season_is_closed(2026, date(2026, 10, 28)) is False


def test_season_window_covers_full_season_without_overlap():
    """赛季窗口要覆盖跨年制整季，且不与相邻赛季重叠。"""
    d0, d1 = _season_window(2025)
    assert d0 == "2025-07-01"
    assert d1 == "2026-06-30"
    # 2025-08 的比赛（赛季主力月份）必须落在窗口内
    assert d0 <= "2025-08-20" <= d1
    # 上赛季窗口不能覆盖到本季
    prev0, prev1 = _season_window(2024)
    assert prev1 < d0, "相邻赛季窗口不应重叠"


# ---- 查询失败不能误判 ----------------------------------------------------

def test_local_lookup_failure_falls_back_to_fetch(tmp_path):
    """本地库读不到时按「没数据」处理，走回源——绝不能把未回填的赛季误判成已回填。"""
    api = CountingAPI()
    sync, _ = _build(api, tmp_path)
    closed = _opening_season(date.today()) - 1

    # 让查询抛异常
    def boom(*a, **k):
        raise RuntimeError("db down")

    sync.repo.load_matches = boom
    r = run(sync.sync_season(closed))

    assert len(api.calls) == 1, "查询失败必须回源，不能因此永久跳过该赛季"
    assert r.get("skipped") == 0
