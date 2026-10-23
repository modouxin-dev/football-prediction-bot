"""回测「双口径」与「语料窗口」的测试。

为什么必须锁这两件事
--------------------
1. 方向命中率（主/平/客，随机基线 33%）与精确比分命中率（猜中 2-1，
   业界 5~8%）差约一个量级。早期看板只有一列「命中率」，
   把 5% 显示出来会被读成"模型崩了"。两个口径必须分列、分别可查。
2. 回测每天跑一次，但语料固定时 20 条记录会一字不差——
   看起来像验证过 20 次，实际是同一次验证重复 20 遍。
   窗口边界 + 相对上次的增量，才能让"每天跑"产生可观测的变化。

这里的断言都是**行为**而非实现细节：改内部实现不该让它们失效。
"""

from __future__ import annotations

from backtest import BacktestReport, poisson_mode
from backtest_corpus import load_corpus
from repository import PredictionRepository


# ---- 精确比分的众数定义 -----------------------------------------------------

def test_poisson_mode_floor():
    """泊松众数 = floor(λ)：λ=1.8 → 1 球，不是四舍五入的 2 球。"""
    assert poisson_mode(1.8) == 1
    assert poisson_mode(0.3) == 0
    assert poisson_mode(2.0) == 2


def test_poisson_mode_negative_safely_zero():
    """λ 异常为负时归零，不能产出负比分参与比对。"""
    assert poisson_mode(-1.5) == 0


# ---- 两个口径必须分开 -------------------------------------------------------

def test_report_has_both_rates():
    """报告同时给出 direction 与 exact 两个命中率，且互不覆盖。"""
    r = BacktestReport(label="t")
    r.n, r.hits = 10, 5              # 方向命中 50%
    r.exact_n, r.exact_hits = 10, 1  # 比分命中 10%
    assert r.accuracy_rate == 0.5
    assert r.exact_rate == 0.1
    d = r.to_dict()
    assert d["accuracy"] == 0.5
    assert d["exact_rate"] == 0.1
    # 关键：两个键必须同时存在，缺任一就会退回"一个数字两种理解"
    assert "accuracy" in d and "exact_rate" in d


def test_exact_rate_none_when_no_sample():
    """没有精确比分样本时返回 None，而不是 0.0。

    0.0 会被看板显示成 0%，与"真的一个都没猜中"无法区分。
    """
    r = BacktestReport()
    assert r.exact_rate is None
    assert r.accuracy_rate is None


# ---- 语料窗口 ---------------------------------------------------------------

def test_corpus_info_exposes_window(tmp_path):
    """语料信息必须带窗口边界，否则看不出回测样本有没有增长。"""
    repo = PredictionRepository(db_path=str(tmp_path / "t.db"))
    conn = repo._connect()
    for i, date in enumerate(("2024-01-01", "2024-06-01")):
        conn.execute(
            "INSERT INTO matches (id, competition_code, season, utc_date, status, "
            "home_team_id, home_team_name, away_team_id, away_team_name, "
            "home_score, away_score, source, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, "PL", 2024, date + "T12:00:00Z", "FINISHED",
             "1", "A", "2", "B", 1, 0, "test", "2026-01-01T00:00:00"),
        )
    conn.commit()

    _, info = load_corpus(repo, "PL", div="E0", include_history=False)
    assert info["window_start"] == "2024-01-01"
    assert info["window_end"] == "2024-06-01"


# ---- 落库与增量 -------------------------------------------------------------

def _run(n: int, corpus_total: int) -> dict:
    return {
        "total_predictions": n,
        "correct_predictions": n // 2,
        "accuracy": 0.5,
        "exact_n": n,
        "exact_hits": n // 10,
        "exact_rate": 0.1,
        "log_loss": 1.0,
        "rps": 0.2,
        "corpus": {"db": corpus_total, "history": 0,
                   "window_start": "2024-01-01", "window_end": "2024-06-01"},
    }


def test_save_backtest_run_persists_both_rates(tmp_path):
    """两个口径都要真的落库——只存一个等于没改。"""
    repo = PredictionRepository(db_path=str(tmp_path / "bt.db"))
    rid = repo.save_backtest_run(competition="PL", variant="poisson",
                                 min_history=30, result=_run(100, 500))
    assert rid is not None

    row = repo.latest_backtest("PL", "poisson")
    assert row["accuracy"] == 0.5
    assert row["exact_rate"] == 0.1
    assert row["exact_n"] == 100
    assert row["window_start"] == "2024-01-01"
    # 首次没有可比对象 → 增量应为 None（未知），不能填 0 冒充"没增长"
    assert row["n_new"] is None


def test_n_new_counts_corpus_growth(tmp_path):
    """第二次回测若语料变多，n_new 必须反映出来。

    n_new 是判断"这次是不是又跑了一遍同样的样本"的唯一依据：
    0 表示样本没长，数字相同属于正常，不该被误读成模型稳定。
    """
    repo = PredictionRepository(db_path=str(tmp_path / "bt2.db"))
    repo.save_backtest_run(competition="PL", variant="poisson",
                           min_history=30, result=_run(100, 500))
    repo.save_backtest_run(competition="PL", variant="poisson",
                           min_history=30, result=_run(120, 560))

    latest = repo.latest_backtest("PL", "poisson")
    assert latest["n_new"] == 60  # 560 - 500


def test_n_new_zero_when_corpus_unchanged(tmp_path):
    """语料没变时 n_new = 0：明确告知"这次没有新样本"。"""
    repo = PredictionRepository(db_path=str(tmp_path / "bt3.db"))
    repo.save_backtest_run(competition="PL", variant="poisson",
                           min_history=30, result=_run(100, 500))
    repo.save_backtest_run(competition="PL", variant="poisson",
                           min_history=30, result=_run(100, 500))
    assert repo.latest_backtest("PL", "poisson")["n_new"] == 0
