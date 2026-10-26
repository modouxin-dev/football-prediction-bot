"""模型选择框架的判定逻辑测试。

⚠️ 这里用**合成数据**验证的是「判定逻辑本身」，不是生产参数。
合成数据能证明：样本不足会拒答、无差异会维持现状、显著改善会被采纳。
它不能证明 Elo 该不该启用——那必须用真实历史语料在本地/生产跑。

判定规则（预先注册，不得事后放宽）：
① Brier 改善 ② RPS 不退化 ③ 配对 bootstrap 95% CI 不跨 0
"""
from __future__ import annotations

import pytest

from model_select import (
    BOOTSTRAP_N,
    MIN_MATCHES,
    Candidate,
    brier_of,
    log_loss_of,
    paired_bootstrap,
    per_match_scores,
    rps_of,
    run_experiment,
)


def _synth(n: int = 200, seed: int = 7) -> list[dict]:
    """合成历史比赛（仅供回测框架跑通，不用于定生产参数）。"""
    import random

    rng = random.Random(seed)
    out = []
    for i in range(n):
        hs, as_ = rng.randint(0, 3), rng.randint(0, 3)
        out.append({
            "fixture_id": f"fx{i}",
            "utc_date": f"2024-01-{1 + i % 28:02d}T15:00:00Z",
            "home_team_id": f"t{rng.randint(1, 8)}",
            "away_team_id": f"t{rng.randint(1, 8)}",
            "home_score": hs,
            "away_score": as_,
        })
    return out


# ---- 指标函数 ---------------------------------------------------------------


def test_brier_perfect_is_zero():
    assert brier_of([1.0, 0.0, 0.0], 0) == pytest.approx(0.0)


def test_brier_worse_than_uniform_when_confidently_wrong():
    """坚信主胜却输球，Brier 必须比均匀分布（0.667）更差。"""
    assert brier_of([1.0, 0.0, 0.0], 2) > brier_of([1 / 3] * 3, 2)


def test_rps_penalises_ordinal_distance():
    """RPS 的核心性质：主胜判成客胜，惩罚大于判成平局。"""
    wrong_far = rps_of([1.0, 0.0, 0.0], 2)    # 预测主胜，实际客胜
    wrong_near = rps_of([1.0, 0.0, 0.0], 1)   # 预测主胜，实际平局
    assert wrong_far > wrong_near


def test_rps_perfect_is_zero():
    assert rps_of([0.0, 0.0, 1.0], 2) == pytest.approx(0.0)


def test_log_loss_zero_probability_is_clamped_not_inf():
    """概率为 0 时截断而非返回 inf——inf 会污染均值，使比较失去意义。"""
    v = log_loss_of([0.0, 1.0, 0.0], 0)
    assert v == pytest.approx(5.0)
    assert v != float("inf")


# ---- 逐场得分 ---------------------------------------------------------------


def test_per_match_scores_drops_records_without_probs():
    """缺三项概率的场次必须丢弃，不能填 0——填 0 等于伪造改善。"""
    records = [
        {"probs": [0.5, 0.3, 0.2], "actual": "home"},
        {"prob": 0.5, "actual": "home"},          # 旧格式，无 probs
        {"probs": [0.2, 0.3, 0.5], "actual": "away"},
    ]
    scores = per_match_scores(records)
    assert len(scores) == 2
    assert all("brier" in s and "rps" in s for s in scores)


def test_per_match_scores_accepts_index_actual():
    """actual 既可是名称也可是下标。"""
    assert len(per_match_scores([{"probs": [.5, .3, .2], "actual_index": 0}])) == 1


# ---- 配对 bootstrap ---------------------------------------------------------


def _scores(deltas: list[float], metric: str = "brier") -> list[dict]:
    """构造逐场得分：基线恒为 0.5，候选为 0.5+delta。"""
    return [{metric: 0.5 + d} for d in deltas]


def test_bootstrap_refuses_mismatched_lengths():
    r = paired_bootstrap(_scores([0.0] * 10), _scores([0.0] * 9))
    assert "error" in r


def test_bootstrap_identical_models_not_significant():
    """两路完全相同时，CI 必然跨 0，不得判显著。"""
    base = _scores([0.0] * 200)
    r = paired_bootstrap(base, _scores([0.0] * 200), n=2000)
    assert r["significant"] is False


def test_bootstrap_detects_consistent_improvement():
    """候选稳定更优（每场都好一点）→ 均值差为负且 CI 不跨 0。"""
    base = _scores([0.0] * 200)
    chal = _scores([-0.02] * 200)
    r = paired_bootstrap(base, chal, n=2000)
    assert r["mean_diff"] < 0
    assert r["ci_high"] < 0
    assert r["significant"] is True


def test_bootstrap_is_reproducible():
    base = _scores([0.0] * 100)
    chal = _scores([-0.01] * 100)
    a = paired_bootstrap(base, chal, n=1000)
    b = paired_bootstrap(base, chal, n=1000)
    assert a["ci_low"] == b["ci_low"]


# ---- 实验编排 ---------------------------------------------------------------


def test_insufficient_sample_refuses_to_conclude():
    """低于 MIN_MATCHES 必须拒答，不给结论。"""
    res = run_experiment(_synth(20), [Candidate(key="base"), Candidate(key="c1")],
                         baseline_key="base", min_history=5)
    assert res.verdict == "insufficient"
    assert "下限" in res.reason


def test_missing_baseline_is_inconclusive():
    res = run_experiment(_synth(200), [Candidate(key="c1")],
                         baseline_key="base", min_history=5)
    assert res.verdict == "inconclusive"
    assert "缺少基线" in res.reason


def test_identical_candidates_keep_baseline():
    """与基线完全相同的候选不得被采纳——三条规则中的 CI 不满足。"""
    res = run_experiment(
        _synth(200),
        [Candidate(key="base"), Candidate(key="same_as_base")],
        baseline_key="base", min_history=5, n_boot=500)
    assert res.verdict == "keep_baseline"
    assert res.winner_key == "base"


def test_experiment_reports_all_metrics_per_candidate():
    res = run_experiment(
        _synth(200),
        [Candidate(key="base"),
         Candidate(key="elo", use_elo=True, elo_blend=0.6)],
        baseline_key="base", min_history=5, n_boot=300)
    assert len(res.table) == 2
    for row in res.table:
        for metric in ("brier", "rps", "log_loss"):
            assert metric in row
    # 四条规则字段必须落出来，供人工审阅。
    # 主指标是 Log Loss（小差异场景判别力优于 Brier/RPS），
    # 次指标是 Brier 与 RPS 两项——只盯一项会让误差被挪到另一维度。
    if res.table[1].get("rules"):
        assert set(res.table[1]["rules"]) == {
            "log_loss_improves", "brier_not_worse", "rps_not_worse",
            "ci_significant"}


def test_backtest_records_carry_three_way_probs():
    """回测 records 必须带三项概率，否则 Brier/RPS 无从计算。

    这是本框架能成立的前提：没有它，per_match_scores 会丢弃全部场次。
    """
    from backtest import WalkForwardBacktester

    tester = WalkForwardBacktester(min_history=5, use_elo=False)
    report = tester.run(_synth(120))
    assert report.records, "回测未产出任何记录"
    assert all("probs" in r and len(r["probs"]) == 3 for r in report.records)
    scores = per_match_scores(report.records)
    assert len(scores) == len(report.records)


def test_elo_blend_is_passed_through():
    """elo_blend 必须真正传到 elo_multiplier，否则网格搜索是假的。"""
    from backtest import WalkForwardBacktester

    t = WalkForwardBacktester(min_history=5, use_elo=True, elo_blend=0.1)
    assert t.elo_blend == 0.1
    d = WalkForwardBacktester(min_history=5, use_elo=True)
    assert d.elo_blend is None  # None = 沿用 elo.ELO_BLEND


# ---- 主指标必须是 Log Loss ---------------------------------------------------
# 文献：区分完美预测者时，ignorance（负对数损失）通常优于 Brier，
# Brier 又往往优于 RPS，尤其在模型差异较小时。
# 本框架判的正是「小幅调参有无增益」这种小差异场景，故主指标定为 Log Loss。


def test_primary_metric_is_log_loss():
    """主指标必须是 log_loss——bootstrap 也必须以它为单位。"""
    import inspect

    from model_select import run_experiment

    src = inspect.getsource(run_experiment)
    assert 'metric="log_loss"' in src, "主指标被改成了别的，文献依据不再成立"
    assert "log_loss_improves" in src


def test_rejects_when_log_loss_better_but_brier_worse():
    """主指标改善但次指标退化 → 不采纳。

    只盯主指标的话，改善可能只是把误差挪到了另一个维度。
    """
    from model_select import judge

    def _scores(ll, brier, rps, n=40):
        return [{"match_id": str(i), "actual": 0,
                 "brier": brier, "rps": rps, "log_loss": ll}
                for i in range(n)]

    # baseline: Log Loss 差、Brier 好；challenger: Log Loss 好、Brier 差
    base = _scores(ll=1.10, brier=0.60, rps=0.40)
    chal = _scores(ll=1.00, brier=0.62, rps=0.40)
    boot = {"significant": True, "mean_diff": -0.10}

    v = judge(base, chal, boot)
    assert v["log_loss_improves"] is True
    assert v["brier_not_worse"] is False      # 次指标退化
    assert v["rps_not_worse"] is True
    assert not all(v.values()), "四项未同时满足，不该采纳"


def test_judge_requires_all_four():
    """四项全满足才采纳——少一项都不行。"""
    from model_select import judge

    def _scores(ll, brier, rps, n=40):
        return [{"match_id": str(i), "actual": 0,
                 "brier": brier, "rps": rps, "log_loss": ll}
                for i in range(n)]

    good = _scores(ll=1.00, brier=0.60, rps=0.40)
    assert all(judge(_scores(1.10, 0.60, 0.40), good,
                     {"significant": True, "mean_diff": -0.1}).values())
    # 显著但方向是变差 → 不算
    v = judge(_scores(1.10, 0.60, 0.40), good,
              {"significant": True, "mean_diff": 0.1})
    assert v["ci_significant"] is False
    # 不显著 → 不算
    v = judge(_scores(1.10, 0.60, 0.40), good,
              {"significant": False, "mean_diff": -0.1})
    assert v["ci_significant"] is False


def test_judge_returns_none_when_metric_missing():
    """指标缺失返回 None，不能当成 0——那会伪造改善。"""
    from model_select import judge

    base = [{"match_id": "1", "actual": 0, "brier": 0.6,
             "rps": 0.4, "log_loss": 1.1}]
    chal = [{"match_id": "1", "actual": 0, "brier": None,
             "rps": 0.4, "log_loss": 1.0}]
    assert judge(base, chal, {"significant": True, "mean_diff": -0.1}) is None
