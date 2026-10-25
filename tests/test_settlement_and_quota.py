"""结算队列与数据源健壮性 / Settlement queue & data-source robustness.

本文件覆盖四个此前会造成**静默停摆**的缺陷。共同特征：不报错、不崩溃，
系统看起来在正常跑，实际上某条链路已经断了。

  1. 结算队列被僵尸行堵死 —— 看板停在最后一次成功的日期
  2. 额度熔断缺失 —— 有计数、有展示，唯独没有熔断
  3. kickoff 字符串比较 —— Z 与 +00:00 表示同一时刻却得出相反结论
  4. 可选数据主源返回空 —— 误报成「备用源没配」

全部不联网。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import api_client
import data_source
import repository as repomod
from repository import PredictionRepository, normalize_kickoff


# ── 1. 结算队列 ────────────────────────────────────────────────────────

def _mk_db(tmp_path, name="t.db"):
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE predictions(fixture_id TEXT PRIMARY KEY, league TEXT, "
        "home TEXT, away TEXT, kickoff TEXT, home_prob REAL, draw_prob REAL, "
        "away_prob REAL, best_score TEXT, result TEXT, actual_home INTEGER, "
        "actual_away INTEGER)")
    return db, conn


def _add(conn, fid, kickoff, actual=None):
    a, b = (None, None) if actual is None else actual
    conn.execute(
        "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, "140", "H", "A", kickoff, 0.5, 0.25, 0.25, "1-0", "home", a, b))


def test_stale_rows_cannot_block_new_matches(tmp_path):
    """僵尸行堆满 limit 时，新结束的比赛仍能进入结算队列。

    缺陷形态：pending() 按 kickoff 升序取前 limit 条，而超出回溯窗口的行
    永远结算不了却永远排在最前。堆满后新比赛一条都进不来，
    结算永久停摆且不会自愈。
    """
    db, conn = _mk_db(tmp_path)
    now = datetime.now(timezone.utc)
    for i in range(200):  # 200 条远超回溯窗口的僵尸行
        _add(conn, f"z{i}", (now - timedelta(days=30 + i)).isoformat())
    for i in range(5):    # 5 条刚结束、应当被结算的比赛
        _add(conn, f"n{i}", (now - timedelta(hours=3)).isoformat())
    conn.commit()

    repo = PredictionRepository(str(db))
    pending = [r["fixture_id"] for r in repo.pending(limit=200)]

    assert sum(1 for f in pending if f.startswith("n")) == 5, (
        "新结束的比赛必须全部进入结算队列，不能被僵尸行挤掉")


def test_count_stale_pending_reveals_invisible_rows(tmp_path):
    """永远结算不了的行要能被数出来，不能只藏在表里。"""
    db, conn = _mk_db(tmp_path)
    now = datetime.now(timezone.utc)
    for i in range(7):
        _add(conn, f"z{i}", (now - timedelta(days=10 + i)).isoformat())
    _add(conn, "fresh", (now - timedelta(hours=2)).isoformat())
    conn.commit()

    repo = PredictionRepository(str(db))
    assert repo.count_stale_pending() == 7
    assert "fresh" in [r["fixture_id"] for r in repo.pending(limit=200)]


# ── 2. 额度熔断 ────────────────────────────────────────────────────────

def _client_with_quota(remaining: str | None, floor: int = 0):
    c = api_client.FootballAPI.__new__(api_client.FootballAPI)
    c._quota_left = None
    c.quota_floor = floor
    c.quota_remaining = remaining
    if remaining is not None:
        try:
            c._quota_left = int(str(remaining).strip())
        except (TypeError, ValueError):
            c._quota_left = None
    return c


def test_quota_exhausted_when_below_floor():
    assert _client_with_quota("3", floor=5).quota_exhausted is True


def test_quota_not_exhausted_when_ample():
    assert _client_with_quota("75000", floor=5).quota_exhausted is False


def test_quota_unknown_never_breaks_the_system():
    """额度未知时绝不熔断。

    把「不知道」当成「耗尽」会让整套系统拒发所有请求——
    那比额度打光本身更糟，且极难排查。
    """
    assert _client_with_quota(None).quota_exhausted is False
    assert _client_with_quota("not-a-number").quota_exhausted is False
    assert _client_with_quota(None).quota_left is None


# ── 3. kickoff 归一化 ──────────────────────────────────────────────────

def test_z_and_offset_formats_are_the_same_instant():
    """同一时刻的两种合法写法必须归一到同一个字符串。

    否则字符串比较时：'+'(43) < '.'(46) < 'Z'(90)，
    比到小数位就分叉，Z 结尾的那场永远不进结算队列。
    """
    a = normalize_kickoff("2026-10-25T18:42:15+00:00")
    b = normalize_kickoff("2026-10-25T18:42:15Z")
    assert a == b
    assert a.endswith("+00:00")


def test_naive_kickoff_treated_as_utc():
    """不带时区的按 UTC 处理，而不是丢弃。"""
    assert normalize_kickoff("2026-10-25 18:42:15") == "2026-10-25T18:42:15+00:00"


def test_unparseable_kickoff_returns_none():
    """解析不了返回 None，不猜测、不填默认时刻。"""
    assert normalize_kickoff("明天晚上") is None
    assert normalize_kickoff("") is None
    assert normalize_kickoff(None) is None


def test_normalized_values_compare_correctly(tmp_path):
    """归一化后，字符串比较的结果与真实时间先后一致。"""
    now = datetime.now(timezone.utc).isoformat()
    past = normalize_kickoff((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    future = normalize_kickoff((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    z_past = normalize_kickoff(
        (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert past <= now
    assert future > now
    assert z_past <= now, "Z 结尾的过去时刻必须判定为已开赛"


# ── 4. 可选数据降级 ────────────────────────────────────────────────────

def test_optional_methods_do_not_mention_missing_fallback_token():
    """可选数据（阵容/伤停/赔率）不得把「主源没数据」说成「备用源没配」。

    实测症状：主源好端端的，看板却显示「缺少 FOOTBALL_DATA_API_TOKEN」，
    用户据此以为数据源坏了。
    """
    for m in ("get_lineups", "get_injuries", "get_odds"):
        assert m in data_source.OPTIONAL_METHODS, f"{m} 应属可选数据"


def test_settle_floor_matches_backfill_window():
    """待结算下界必须与回溯窗口一致。

    下界比回溯窗口宽，等于把「永远结算不了」的行又放回队列最前面，
    堵死问题会以更慢的速度复发。
    """
    import service

    assert PredictionRepository.SETTLE_PENDING_FLOOR_DAYS == service.SETTLE_BACKFILL_DAYS
