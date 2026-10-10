"""回测兼容层的回归测试（防假数据复发）。

为什么需要这个文件：
    ``backtester.py`` 与 ``backtester_with_db.py`` 曾经都能跑通、也都打印
    出"完成"，但数字来自硬编码的 Team A/B/C 假比赛与硬编码概率。这类
    **假绿灯**比崩溃更危险——它会被当成模型有效的证据。

    这里锁死三件事：
      1. 数据源必须是真实语料（场数量级对得上）；
      2. 准确率必须是算出来的，不是恒定的 100% / 33%；
      3. 判定函数必须能识破假数据（反向用例）。

    任一条退化，说明又有人把假数据塞回来了。
"""

from __future__ import annotations

import pytest

from backtester import Backtester
from backtester_with_db import BacktesterWithDB
from integrated_backtest_test import ACC_RANGE, MIN_SAMPLES, judge_backtest

EXPECTED_TOTAL = 1110   # 1140 场语料 - 30 场预热


def _empty_db(tmp_path) -> str:
    return str(tmp_path / "empty.db")


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ---- 1. 数据源是真的 -------------------------------------------------------

def test_backtester_loads_real_corpus(tmp_path):
    """原实现只有 1 场硬编码比赛，现在必须是真实语料。"""
    bt = Backtester(league_id=39, db_path=_empty_db(tmp_path))
    matches = _run(bt.load_historical_fixtures())

    assert len(matches) == 1140, f"语料场数异常: {len(matches)}"
    assert all(m.get("home_team_name") for m in matches[:20]), "缺队名，疑似假数据"
    assert "Team A" not in [m.get("home_team_name") for m in matches]


def test_backtester_with_db_loads_real_corpus(tmp_path):
    """原实现是 3 场 Team A/C/E，现在必须是真实语料。"""
    bw = BacktesterWithDB(league_id=39, db_path=_empty_db(tmp_path))
    matches = _run(bw.load_from_db())

    assert len(matches) == 1140, f"语料场数异常: {len(matches)}"
    names = [m.get("home_team_name") for m in matches]
    assert "Team A" not in names and "Team C" not in names, "仍是硬编码假数据"


# ---- 2. 数字是真算出来的 ---------------------------------------------------

def test_backtester_backtest_real_metrics(tmp_path):
    """准确率不能再是硬编码概率算出的假值。"""
    bt = Backtester(league_id=39, db_path=_empty_db(tmp_path))
    r = _run(bt.backtest())

    assert r["total_predictions"] == EXPECTED_TOTAL
    lo, hi = ACC_RANGE
    assert lo < r["accuracy"] < hi, f"准确率分布异常: {r['accuracy']}"
    assert r["log_loss"] > 0 and r["rps"] > 0

    # 逐场明细要能还原出真实队名与比分，不是 "Team A vs Team B"
    first = r["predictions"][0]
    assert first["match"] != "? vs ?"
    assert "-" in first["actual_score"], f"比分异常: {first['actual_score']}"


def test_backtester_with_db_real_metrics(tmp_path):
    """预测不能恒为 home（原实现就是恒定预测）。"""
    bw = BacktesterWithDB(league_id=39, db_path=_empty_db(tmp_path))
    r = _run(bw.run_backtest())

    assert r["total_predictions"] == EXPECTED_TOTAL
    predicted = {p["predicted"] for p in r["predictions"]}
    assert len(predicted) > 1, f"预测结果恒定: {predicted}"

    lo, hi = ACC_RANGE
    assert lo < r["accuracy"] < hi


def test_confidence_levels_monotonic(tmp_path):
    """高信心命中率必须高于低信心，否则等级只是随机标签。"""
    bt = Backtester(league_id=39, db_path=_empty_db(tmp_path))
    _run(bt.backtest())
    rates = {k: v["accuracy"] for k, v in bt.get_confidence_analysis().items()
             if v["accuracy"] is not None}

    assert "high" in rates and "low" in rates
    assert rates["high"] > rates["low"], f"等级无区分度: {rates}"


def test_empty_data_raises(tmp_path):
    """真没数据时抛错，而不是静默返回 0 场。"""
    bw = BacktesterWithDB(league_id=39, db_path=_empty_db(tmp_path), season=1800)
    with pytest.raises(ValueError, match="无可用数据"):
        _run(bw.run_backtest())


# ---- 3. 判定函数必须能识破假数据 -------------------------------------------

def _fake(n: int, acc: float, levels=("low",)) -> dict:
    preds = [{"level": levels[i % len(levels)], "correct": i < int(n * acc)}
             for i in range(n)]
    return {"total_predictions": n, "accuracy": acc, "predictions": preds,
            "accuracy_by_type": {}, "log_loss": 1.0}


@pytest.mark.parametrize("name,data", [
    ("样本不足", _fake(3, 0.333)),
    ("恒100%", _fake(1110, 1.0)),
    ("恒0%", _fake(1110, 0.0)),
    ("分布外", _fake(1110, 0.95)),
    ("等级无区分度", _fake(1110, 0.51, levels=("low", "high"))),
])
def test_judge_rejects_fake_data(name, data):
    """假数据必须被判为未通过——否则又会变成假绿灯。"""
    ok, reasons = judge_backtest(data)
    assert not ok, f"{name} 竟然被判为通过"
    assert reasons, "未给出失败原因"


def test_judge_accepts_real_result(tmp_path):
    """真实回测结果必须通过判定。"""
    bw = BacktesterWithDB(league_id=39, db_path=_empty_db(tmp_path))
    ok, reasons = judge_backtest(_run(bw.run_backtest()))
    assert ok, f"真实结果被误判: {reasons}"


def test_min_samples_guard_exists():
    """样本下限必须存在且合理，防止几场样本就下结论。"""
    assert MIN_SAMPLES >= 200
