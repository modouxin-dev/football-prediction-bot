"""历史赛季入库 + 回测结论落盘。

要解决的问题
------------
1. 历史赛季从不进库：自动调度只有 sync_upcoming(+7天) 与 sync_recent(-14天)，
   而 sync_season / sync_full_season 只挂在 /backfill 手动命令上。
   结果就是库里 0 场，回测只能吃静态 CSV 快照。
2. sync_full_season 名不副实：它走的是带日期范围的 sync_window，
   而主源对历史赛季正是按"是否带日期范围"判定套餐权限的——
   带日期请求会被拒。名字叫整季却拉不到整季。
3. 回测结论只活在内存里：容器重启 / 重新部署就消失，
   无从回答"模型是在变好还是在变差"。
"""

import datetime as dt

import pytest

from repository import PredictionRepository


def _fixtures(n: int, season: int = 2024) -> list[dict]:
    """造 n 场已完赛比赛（结构对齐 API-Football 的 fixtures 响应）。"""
    base = dt.datetime(2024, 8, 10, 14, 0, 0)
    return [{
        "fixture": {
            "id": 900000 + i,
            "date": (base + dt.timedelta(days=i // 3)).isoformat() + "+00:00",
            "status": {"short": "FT"},
        },
        "league": {"id": 39, "season": season},
        "teams": {
            "home": {"id": 33 + i % 20, "name": "T%d" % (i % 20)},
            "away": {"id": 34 + (i + 7) % 20, "name": "T%d" % ((i + 7) % 20)},
        },
        "goals": {"home": i % 4, "away": (i + 1) % 3},
    } for i in range(n)]


# ---- 历史入库确实进了库，且回测能读到 -------------------------------------

def test_saved_matches_become_backtest_corpus(tmp_path):
    """入库的历史赛果必须被回测语料读到（db 计数上升）。

    这条锁住"历史数据入库"的端到端链路：save_matches → load_corpus。
    只落库但语料读不到，等于白存。
    """
    from backtest_corpus import load_corpus

    repo = PredictionRepository(str(tmp_path / "h.db"))
    _, before = load_corpus(repo, "PL")
    assert before["db"] == 0, "新库不该有库内赛果"

    saved = repo.save_matches("PL", _fixtures(200), source="api-football",
                              http_status="200", message="season-2024")
    assert saved == 200

    matches, after = load_corpus(repo, "PL")
    assert after["db"] == 200, "入库的赛果应计入 corpus"
    assert after["total"] > before["total"], "样本总量应增加"
    assert len(matches) == after["total"]


def test_corpus_is_time_ordered(tmp_path):
    """回测样本必须按开赛时间正序——乱序会让滚动前进变成偷看未来。"""
    from backtest_corpus import load_corpus

    repo = PredictionRepository(str(tmp_path / "h.db"))
    repo.save_matches("PL", _fixtures(120), source="api-football",
                      http_status="200", message="season-2024")
    matches, _ = load_corpus(repo, "PL")
    dates = [m.get("utc_date") for m in matches if m.get("utc_date")]
    assert len(dates) > 100
    assert dates == sorted(dates), "样本必须按时间正序"


# ---- sync_full_season 必须走整季请求 --------------------------------------

def test_sync_full_season_uses_season_request():
    """sync_full_season 必须委托 sync_season（不带日期范围）。

    带日期范围请求历史赛季会被主源按套餐限制拒掉，这是历史数据拉不回来的
    直接原因。这里用行为验证：看它到底调了 sync_season 还是 sync_window。
    """
    import asyncio

    from sync import MatchSync

    calls: list[str] = []

    class FakeSync(MatchSync):
        def __init__(self):  # 绕过 __init__，只关心 sync_full_season 的走向
            pass

        async def sync_season(self, season: int) -> dict:
            calls.append("season:%s" % season)
            return {"received": 380, "saved": 380, "ok": True, "message": ""}

        async def sync_window(self, *a, **kw) -> dict:
            calls.append("window")
            return {"received": 0, "saved": 0, "ok": False, "message": ""}

        class _Svc:
            season_in_use = 2025

        service = _Svc()

    asyncio.run(FakeSync().sync_full_season())
    assert calls == ["season:2025"], "应走整季请求而非日期窗口"


def test_sync_history_aggregates_per_season():
    """sync_history 应逐赛季汇总，且单季失败不影响其余。"""
    import asyncio

    from sync import MatchSync

    calls: list[int] = []

    class FakeSync(MatchSync):
        def __init__(self):  # 绕过 __init__，只测 sync_history
            pass

        async def sync_season(self, season: int) -> dict:
            calls.append(season)
            if season == 2021:
                raise RuntimeError("套餐不支持")
            return {"received": 380, "saved": 380, "ok": True, "message": ""}

    fs = FakeSync()
    out = asyncio.run(fs.sync_history([2023, 2021, 2022]))

    assert calls == [2023, 2021, 2022], "应按给定顺序逐季尝试"
    assert out["seasons"] == 3
    assert out["saved"] == 760, "失败的赛季不计入 saved"
    failed = [p for p in out["per_season"] if p["season"] == 2021][0]
    assert failed["ok"] is False
    assert "套餐" in failed["message"]


def test_sync_history_handles_unavailable_seasons():
    """查不到可用赛季时应明确报告，而不是静默成功。"""
    import asyncio

    from sync import MatchSync

    class _Api:
        async def get_available_seasons(self):
            return []

    class FakeSync(MatchSync):
        def __init__(self):
            self.service = type("S", (), {"api": _Api()})()

        async def sync_season(self, season: int) -> dict:
            raise AssertionError("没有可用赛季就不该调用 sync_season")

    out = asyncio.run(FakeSync().sync_history())
    assert out["seasons"] == 0
    assert out["ok"] is False
    assert "套餐" in out["message"]


# ---- 回测结论落盘 ---------------------------------------------------------

def test_backtest_run_persists_and_reads_back(tmp_path):
    """回测结论必须落盘且能原样读回——否则重启即失。"""
    repo = PredictionRepository(str(tmp_path / "b.db"))
    result = {
        "total_predictions": 1110, "correct_predictions": 566,
        "accuracy": 0.5099, "log_loss": 1.0053, "rps": 0.2073,
        "brier": 0.6003, "ece": 0.0312, "rho": None,
        "corpus": {"db": 200, "history": 1140},
        "verdict": "无显著差异",
        "by_level": {"low": {"n": 100, "hit": 40}, "high": {"n": 50, "hit": 34}},
        "calibration": {"0": {"n": 10, "pred": 0.4, "actual": 0.4}},
    }
    rid = repo.save_backtest_run(competition="PL", variant="poisson",
                                 min_history=30, result=result)
    assert rid is not None

    got = repo.latest_backtest("PL")
    assert got["id"] == rid
    assert got["n"] == 1110
    assert got["hits"] == 566
    assert abs(got["accuracy"] - 0.5099) < 1e-9
    assert abs(got["log_loss"] - 1.0053) < 1e-9
    assert got["corpus_db"] == 200, "语料来源要留档，才知道结论基于多少真实数据"
    assert got["corpus_csv"] == 1140
    assert got["verdict"] == "无显著差异"
    # JSON 字段要能还原成字典，不能是字符串
    assert got["by_level"]["high"]["hit"] == 34
    assert got["calibration"]["0"]["pred"] == 0.4


def test_backtest_history_accumulates_newest_first(tmp_path):
    """多次回测应逐条累积，且最新的排在最前。"""
    repo = PredictionRepository(str(tmp_path / "b.db"))
    for i in range(3):
        repo.save_backtest_run(
            competition="PL", variant="poisson", min_history=30,
            result={"total_predictions": 100 + i, "correct_predictions": 50,
                    "accuracy": 0.5, "log_loss": 1.0 - i * 0.01,
                    "corpus": {"db": i * 100, "history": 1140},
                    "verdict": "v%d" % i})
    hist = repo.backtest_history("PL")
    assert len(hist) == 3
    assert [h["id"] for h in hist] == [3, 2, 1], "应新→旧"
    # 能看出趋势：log_loss 逐次下降
    assert hist[0]["log_loss"] < hist[2]["log_loss"]


def test_latest_backtest_returns_none_when_empty(tmp_path):
    """没有回测记录时返回 None，调用方据此判断"还没跑过"。"""
    repo = PredictionRepository(str(tmp_path / "b.db"))
    assert repo.latest_backtest("PL") is None
    assert repo.backtest_history("PL") == []


def test_save_backtest_run_survives_bad_result(tmp_path):
    """结果字段缺失时不应抛异常（回测落盘不能反过来拖垮主流程）。"""
    repo = PredictionRepository(str(tmp_path / "b.db"))
    rid = repo.save_backtest_run(competition="PL", variant="poisson",
                                 min_history=30, result={})
    assert rid is not None, "缺字段也应落盘，只是数值为空"
    got = repo.latest_backtest("PL")
    assert got["n"] == 0
    assert got["by_level"] == {}


# ---- 端到端：入库后回测的语料来源确实变化 ---------------------------------

@pytest.mark.asyncio
async def test_end_to_end_history_then_backtest(tmp_path):
    """入库 → 回测 → 落盘，整条链路跑通，且语料来源正确记录。"""
    import os

    from backtester_fixed import BacktesterFixed

    db = str(tmp_path / "e.db")
    repo = PredictionRepository(db)
    repo.save_matches("PL", _fixtures(200), source="api-football",
                      http_status="200", message="season-2024")

    bt = BacktesterFixed(league_id=39, db_path=db)
    result = await bt.backtest()

    assert result["total_predictions"] > 0
    # 关键：语料里必须体现库内那 200 场
    assert (result["corpus"] or {}).get("db") == 200

    rid = repo.save_backtest_run(competition=bt.competition_code,
                                 variant=bt.variant,
                                 min_history=bt.min_history, result=result)
    got = repo.latest_backtest("PL")
    assert got["id"] == rid
    assert got["corpus_db"] == 200
    assert got["n"] == result["total_predictions"]
