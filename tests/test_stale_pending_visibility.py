"""超期未结算（僵尸行）必须可见。

`count_stale_pending()` 此前**只被测试消费**，生产代码无人调用——
算得出来却没人看，等于没有。这跟额度是同一类问题：
有计数、有展示，唯独没有接入。

僵尸行是什么：已开赛但超出结算回溯窗口、永远结算不了的行。
它们被 pending() 的时间下界挡在结算之外，不再堵塞队列，
但数量本身是故障信号——赛季切换后失效的旧 ID，或某几天赛果没拉回。

本次让它在三个地方可见：看板「模型表现」、/diag、/health/model。
"""
from __future__ import annotations

import json
import os

import pytest

import api as apimod
from repository import PredictionRepository


def _seed(repo, fixture_id: str, kickoff_iso: str, settled: bool = False):
    conn = repo._connect()
    conn.execute(
        """INSERT INTO predictions
           (fixture_id, home, away, kickoff, home_prob, draw_prob, away_prob,
            result, level_key, actual_home, actual_away)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (fixture_id, "A队", "B队", kickoff_iso, 0.5, 0.25, 0.25, "主胜", "low",
         1 if settled else None, 0 if settled else None),
    )
    conn.commit()


def test_stale_count_excludes_settled(repo):
    """已结算的旧比赛不算僵尸——它已经闭环了。"""
    _seed(repo, "old-1", "2020-01-01T12:00:00+00:00", settled=True)
    assert repo.count_stale_pending() == 0


def test_stale_count_includes_unsettled_old(repo):
    """开赛很久却没结算的，正是僵尸行。"""
    _seed(repo, "old-2", "2020-01-01T12:00:00+00:00")
    assert repo.count_stale_pending() == 1


def test_stale_count_excludes_recent(repo):
    """最近的未结算比赛不算僵尸——它还在回溯窗口内，只是还没到结算时机。"""
    from datetime import datetime, timezone
    _seed(repo, "new-1", datetime.now(timezone.utc).isoformat())
    assert repo.count_stale_pending() == 0


@pytest.fixture()
def repo(tmp_path):
    return PredictionRepository(str(tmp_path / "s.db"))


def test_model_health_exposes_stale_pending(monkeypatch, tmp_path):
    """/health/model 必须带上 stale_pending，否则看板无从显示。"""
    path = str(tmp_path / "h.db")
    r = PredictionRepository(path)
    r._connect()
    _seed(r, "old-3", "2020-01-01T12:00:00+00:00")

    apimod._DB_PATH = path
    import asyncio
    body = asyncio.run(apimod.model_health()).body
    data = json.loads(body)
    assert "stale_pending" in data, "/health/model 未暴露僵尸行计数"
    assert data["stale_pending"] == 1


def test_model_health_survives_stale_read_failure(monkeypatch, tmp_path):
    """计数读不到时健康度仍要返回，不能整页 500。"""
    apimod._DB_PATH = str(tmp_path / "none.db")
    monkeypatch.setattr(
        PredictionRepository, "count_stale_pending",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=True)
    import asyncio
    body = asyncio.run(apimod.model_health()).body
    data = json.loads(body)
    assert data.get("stale_pending") is None
