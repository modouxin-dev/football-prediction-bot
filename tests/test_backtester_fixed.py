"""BacktesterFixed 的数据源与回测链路测试。

这个文件存在的理由：
    本模块历史上长期"看起来能跑、实际回测恒为 100%"——数据源悬空时抛
    NameError，而 _evaluate() 又恒返回 True。两者叠加的结果是：即使有人
    喂进数据，得到的也是一个毫无意义的漂亮数字。因此这里的每条断言都指向
    「这个数字是不是真的算出来的」。
"""

from __future__ import annotations

import pytest

from backtester_fixed import BacktesterFixed


def _empty_db(tmp_path) -> str:
    """一个尚未建库的路径：语料只能来自内置 CSV。"""
    return str(tmp_path / "empty.db")


# ---- 1. 数据源真的接上了 ---------------------------------------------------

def test_loads_real_corpus_from_builtin_history(tmp_path):
    """空库时必须能靠内置语料拿到真实样本（1140 场英超）。"""
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path))
    matches = _run(bt.load_historical_fixtures())

    assert len(matches) == 1140, f"语料场数异常: {len(matches)}"
    assert bt.corpus_info["history"] == 1140

    # 每条都得有回测必需的字段，缺一个就跑不动
    for m in matches[:20]:
        assert m["home_team_id"] and m["away_team_id"]
        assert m["home_score"] is not None and m["away_score"] is not None
        assert m["utc_date"]


def test_matches_are_sorted_by_time(tmp_path):
    """滚动前进的前提：样本必须按开赛时间正序。"""
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path))
    matches = _run(bt.load_historical_fixtures())

    dates = [m["utc_date"] for m in matches]
    assert dates == sorted(dates), "样本未按时间正序，会引入前视偏差"


def test_unknown_league_raises(tmp_path):
    """未知联赛直接报错，不猜、不静默返回空。"""
    bt = BacktesterFixed(league_id=999999, db_path=_empty_db(tmp_path))
    with pytest.raises(ValueError, match="不支持的联赛"):
        _ = bt.competition_code


# ---- 2. 回测结果是真算出来的 -----------------------------------------------

def test_backtest_returns_real_metrics(tmp_path):
    """核心回归：accuracy 不能再是无意义的 100%。"""
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path))
    result = _run(bt.backtest())

    # 1140 场 - 30 场预热
    assert result["total_predictions"] == 1110
    assert result["correct_predictions"] == pytest.approx(566, abs=2)

    acc = result["accuracy"]
    assert 0.40 < acc < 0.60, f"准确率落在真实分布之外: {acc}"
    assert abs(acc - 1.0) > 1e-9, "accuracy 恒为 100%，说明评估逻辑仍是占位符"

    # 主判据必须真的算出来了
    for key in ("log_loss", "rps", "brier", "ece"):
        assert result[key] is not None, f"{key} 未计算"
    assert result["log_loss"] > 0


def test_confidence_levels_are_monotonic(tmp_path):
    """信心等级必须有效：高信心命中率显著高于低信心。

    这条是模型可信度的基础——若等级无区分度，「高信心」就是随机标签。
    """
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path))
    result = _run(bt.backtest())

    rates = {}
    for level, stat in result["by_level"].items():
        if stat["n"]:
            rates[level] = stat["rate"]

    assert "high" in rates and "low" in rates, f"等级缺失: {rates}"
    assert rates["high"] > rates["low"], f"高信心未优于低信心: {rates}"


def test_predictions_carry_per_match_detail(tmp_path):
    """predictions 要有逐场明细，不能是空壳列表。"""
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path))
    result = _run(bt.backtest())

    assert len(result["predictions"]) == result["total_predictions"]
    first = result["predictions"][0]
    # 至少要有概率与真实结果，否则没法做二次分析
    assert "probs" in first or "pred" in first or "actual" in first, \
        f"明细字段异常: {sorted(first)}"


# ---- 3. 无数据时的行为 -----------------------------------------------------

def test_empty_corpus_raises_value_error(tmp_path):
    """真的没数据时抛 ValueError（不是 NameError，也不是静默成功）。"""
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path), season=1800)
    with pytest.raises(ValueError, match="无可用数据"):
        _run(bt.backtest())


def test_season_filter_narrows_corpus(tmp_path):
    """指定赛季时样本被裁剪（默认不过滤，因为滚动前进需要历史积累）。"""
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path), season=2024)
    matches = _run(bt.load_historical_fixtures())

    assert matches, "2024 赛季应有样本"
    assert all(m["season"] == 2024 for m in matches)
    assert len(matches) < 1140


# ---- 4. 变体 ---------------------------------------------------------------

def test_elo_variant_runs(tmp_path):
    """Elo 变体同样要能跑出真实指标。"""
    bt = BacktesterFixed(league_id=39, db_path=_empty_db(tmp_path), variant="elo")
    result = _run(bt.backtest())

    assert result["total_predictions"] == 1110
    assert result["variant"] == "elo"
    assert 0.40 < result["accuracy"] < 0.60


def _run(coro):
    import asyncio
    return asyncio.run(coro)
