"""自动预测覆盖范围：必须与看板展示的赛程是同一批比赛。

缺陷背景（用户实测 2026-10-15）：
    赛程同步遍历 settings.league_ids（10 个联赛 × 30 天 = 380 场），
    而自动预测只跑 (settings.league_id, 欧冠) 两个联赛、窗口 2 天。
    于是看板上「有赛程」和「有预测」永远不是同一批比赛——
    绝大部分比赛恒显示「尚未生成预测」。

本文件用可执行的契约锁住覆盖范围，防止再次退化。
"""
from __future__ import annotations

import asyncio
import types

import pytest

import scheduler


class _FakeSettings:
    league_id = 39
    league_ids = (39, 40, 61, 71, 78, 88, 94, 135, 140, 2)


class _FakeService:
    def __init__(self) -> None:
        self.settings = _FakeSettings()
        self.ids: tuple[int, ...] = ()
        self.days: int = 0

    async def auto_predict_store(self, ids, days=2):
        self.ids = tuple(int(x) for x in ids)
        self.days = int(days)
        return {"saved": len(self.ids), "errors": [], "per_league": {},
                "window_days": self.days}


def _run(ids_attr: tuple[int, ...]) -> _FakeService:
    svc = _FakeService()
    svc.settings.league_ids = ids_attr
    app = types.SimpleNamespace(bot_data={"service": svc})
    asyncio.run(scheduler.auto_predict_job(types.SimpleNamespace(application=app)))
    return svc


def test_auto_predict_covers_all_enabled_leagues():
    """自动预测必须覆盖 settings.league_ids 的全部联赛。

    否则赛程页展示 10 个联赛、预测只覆盖 2 个，
    其余 8 个联赛的比赛永远显示「尚未生成预测」。
    """
    enabled = (39, 40, 61, 71, 78, 88, 94, 135, 140, 2)
    svc = _run(enabled)
    missing = [x for x in enabled if x not in svc.ids]
    assert not missing, f"自动预测漏掉联赛：{missing}"


def test_auto_predict_keeps_champions_league_even_if_not_configured():
    """欧冠必须始终在列，即便 LEAGUE_IDS 没写它。

    用户手动点预测只覆盖当前选中联赛，欧冠靠自动预测兜底。
    """
    svc = _run((39, 40))
    assert 2 in svc.ids, "欧冠（2）被漏掉，看板上欧冠会整片显示未预测"


def test_auto_predict_window_covers_board_default_range():
    """预测窗口必须覆盖看板可切换的天数范围。

    看板 days 可选 1/3/7；窗口只有 2 天时，切到「3 天」
    第三天的比赛必然全是未预测。
    """
    svc = _run((39,))
    assert svc.days >= 3, f"窗口仅 {svc.days} 天，覆盖不到看板「3 天」视图"


def test_auto_predict_quota_does_not_starve_tail_leagues():
    """配额不足时尾部联赛也必须分到，不能先到先得饿死后面。

    原实现按 league_ids 顺序累加，cap 打满后直接 break，
    排在后面的联赛（意甲/西甲）一场都轮不到。
    """
    from service import PredictionService

    src_ids = (39, 40, 61, 71, 78)
    per_league = 10          # 每个联赛 10 场待预测
    cap = 20                 # 明显不够 5×10

    calls: list[tuple[int, int]] = []

    async def _fetch(self, d_from, d_to, league_id=0):
        fx = [{"fixture": {"id": league_id * 1000 + i}} for i in range(per_league)]
        return fx, 2026, ""

    async def _predict(self, model, kickoff, fx):
        calls.append((int(fx["fixture"]["id"]) // 1000, 1))
        return types.SimpleNamespace(fixture_id=fx["fixture"]["id"])

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(PredictionService, "_fetch_fixtures", _fetch)
    monkeypatch.setattr(PredictionService, "_predict_one", _predict)
    monkeypatch.setattr(PredictionService, "_upcoming",
                        lambda self, fx, now, end: [(None, f) for f in fx])
    monkeypatch.setattr(PredictionService, "_remember", lambda self, p: None)

    async def _model(self, league_id, cache=None):
        return object()   # 建模内容不影响覆盖统计

    monkeypatch.setattr(PredictionService, "_model_for", _model)
    try:
        svc = PredictionService.__new__(PredictionService)
        stats = asyncio.run(svc.auto_predict_store(src_ids, days=3, cap=cap))
    finally:
        monkeypatch.undo()

    covered = {lid for lid, _ in calls}
    assert len(covered) == len(src_ids), (
        f"配额不足时只有 {sorted(covered)} 分到预测，"
        f"尾部联赛 {sorted(set(src_ids) - covered)} 被饿死")
