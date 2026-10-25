"""额度治理：可见化 + 分级熔断 + 按任务归因。

三件事对应同一个问题的三个面：额度打光后，系统处于无人知晓的停摆状态。
- 可见化：知道还剩多少（否则只能等它报错）
- 按任务归因：知道是谁在用（否则只能猜该停谁）
- 分级熔断：知道该停谁（否则一刀切会把关键链路一起停掉）
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import api as apimod
import repository
import scheduler


# ---- ① 按任务记账 --------------------------------------------------------

@pytest.fixture()
def db(monkeypatch, tmp_path):
    path = str(tmp_path / "q.db")
    monkeypatch.setattr("paths.DB_PATH", path, raising=False)
    monkeypatch.setattr(repository, "_quota_day", lambda: "2026-10-26")
    return path


def test_bump_quota_records_task(db):
    """请求必须记到当前任务名下，而不是只记数据源。"""
    with repository.quota_scope("auto_predict"):
        repository.bump_quota("api-football", 5)
    assert repository.quota_tasks_today().get("auto_predict") == 5


def test_bump_outside_scope_goes_to_other(db):
    """未标注任务时归入 other——宁可粒度粗，也不能不记。"""
    repository.bump_quota("api-football", 3)
    assert repository.quota_tasks_today().get("other") == 3


def test_nested_scope_restores_outer(db):
    """嵌套标注后必须还原外层，不能污染后续请求。"""
    with repository.quota_scope("outer"):
        with repository.quota_scope("inner"):
            repository.bump_quota("api-football", 1)
        repository.bump_quota("api-football", 2)
    t = repository.quota_tasks_today()
    assert t.get("inner") == 1 and t.get("outer") == 2


def test_quota_scope_restores_on_exception(db):
    """任务抛异常时也必须还原，否则同一个任务的额度会串到别的任务上。"""
    try:
        with repository.quota_scope("boom"):
            raise RuntimeError("x")
    except RuntimeError:
        pass
    assert repository.current_quota_task() == "other"


def test_quota_endpoint_returns_task_breakdown(db):
    """/quota 必须给出按任务的明细与最大消耗方。"""
    with repository.quota_scope("auto_predict"):
        repository.bump_quota("api-football", 12)
    with repository.quota_scope("sync_matches"):
        repository.bump_quota("api-football", 5)
    apimod._DB_PATH = db

    import asyncio
    data = asyncio.run(apimod.quota()).body
    import json
    d = json.loads(data)
    assert d["total"] == 17
    assert d["top_task"] == "auto_predict"
    assert d["by_task"]["auto_predict"] == 12


# ---- ② 分级熔断 ----------------------------------------------------------

def test_backtest_stops_first():
    """回测最先停：它能用本地库跑，停掉不影响数据闭环。"""
    ok, _ = scheduler.quota_allows("backtest_run", 500)
    assert not ok


def test_history_stops_before_sync():
    """历史回填整季数百场，是额度大户，先于同步停。"""
    assert not scheduler.quota_allows("sync_history", 300)[0]
    assert scheduler.quota_allows("sync_matches", 300)[0]


def test_critical_jobs_never_gated():
    """结算 / 同步 / 预测在任何档位都不停——停掉等于数据闭环断掉。"""
    for left in (100, 10, 0):
        assert scheduler.quota_allows("settle_results", left)[0]
        assert scheduler.quota_allows("sync_matches", left)[0]
        assert scheduler.quota_allows("auto_predict", left)[0]


def test_unknown_quota_never_gates():
    """额度未知时一律放行。

    拿不到读数就停所有任务，比额度打光本身更糟——
    那等于因为量具坏了就把生产线关掉。
    """
    for task in ("backtest_run", "sync_history", "prematch_refresh"):
        assert scheduler.quota_allows(task, None)[0]


def test_skip_reason_is_actionable():
    """跳过原因必须带数字，不能只说「额度不足」。"""
    ok, reason = scheduler.quota_allows("backtest_run", 500)
    assert not ok
    assert "500" in reason and "800" in reason
