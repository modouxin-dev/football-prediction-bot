"""回测指标单元测试 / Backtest metrics.

先保证尺子本身准，再拿它去量模型。
"""
from backtest import (
    OUTCOME_AWAY, OUTCOME_DRAW, OUTCOME_HOME, WalkForwardBacktester,
    accuracy, brier, calibration_buckets, calibration_error, compare,
    favorite_index, level_monotonic, log_loss, outcome_index, rps, run_backtest,
)

P = (0.5, 0.3, 0.2)


# ============================================================================
# 一、指标本身
# ============================================================================

def test_outcome_index():
    assert outcome_index(2, 1) == OUTCOME_HOME
    assert outcome_index(1, 1) == OUTCOME_DRAW
    assert outcome_index(0, 3) == OUTCOME_AWAY


def test_log_loss_perfect_is_zero():
    assert log_loss((1.0, 0.0, 0.0), OUTCOME_HOME) < 1e-9


def test_log_loss_punishes_overconfidence():
    """把 30% 说成 90% 会被狠狠扣分——这是准确率做不到的。

    实测倍率约 2.5 倍（-ln0.05 / -ln0.3），而非直觉上的 5 倍以上；
    对数损失的惩罚随概率指数增长，但基数差异没那么悬殊。
    """
    honest = log_loss((0.3, 0.4, 0.3), OUTCOME_AWAY)
    overconfident_wrong = log_loss((0.9, 0.05, 0.05), OUTCOME_AWAY)
    assert overconfident_wrong > honest * 2


def test_log_loss_clamps_zero_probability():
    """概率为 0 但押中了，不能返回无穷大（会污染整条回测）。"""
    val = log_loss((0.0, 1.0, 0.0), OUTCOME_HOME)
    assert val < 100 and val > 0


def test_rps_perfect_is_zero():
    assert abs(rps((1.0, 0.0, 0.0), OUTCOME_HOME)) < 1e-9


def test_rps_treats_draw_as_between():
    """RPS 的核心价值：平局是介于主胜与客胜之间的「半对」。"""
    near_miss = rps((1.0, 0.0, 0.0), OUTCOME_DRAW)
    far_miss = rps((1.0, 0.0, 0.0), OUTCOME_AWAY)
    assert near_miss < far_miss


def test_rps_ordinal_nature():
    """RPS 是有序指标：均匀分布下，结果落在中间（平局）时损失最小。

    这正是它优于 Brier 的地方——平局被当作「半对」而非「全错」。
    我最初误以为均匀分布下三种结果损失相同，实测推翻：
    中间桶的累积概率更接近 1，故偏差更小。
    """
    vals = [rps((1 / 3, 1 / 3, 1 / 3), i) for i in range(3)]
    assert vals[OUTCOME_DRAW] < vals[OUTCOME_HOME]
    assert vals[OUTCOME_DRAW] < vals[OUTCOME_AWAY]
    # 两端对称：主胜与客胜的损失应相同
    assert abs(vals[OUTCOME_HOME] - vals[OUTCOME_AWAY]) < 1e-9


def test_brier_perfect_is_zero():
    assert abs(brier((1.0, 0.0, 0.0), OUTCOME_HOME)) < 1e-9


def test_accuracy():
    assert accuracy(OUTCOME_HOME, OUTCOME_HOME) == 1
    assert accuracy(OUTCOME_HOME, OUTCOME_AWAY) == 0


def test_favorite_index():
    assert favorite_index(P) == OUTCOME_HOME
    assert favorite_index((0.1, 0.2, 0.7)) == OUTCOME_AWAY


# ---- 校准 -------------------------------------------------------------------

def test_calibration_perfect_has_zero_error():
    """完美校准：说 70% 的场次真有 70% 发生。"""
    records = [{"prob": 0.7, "hit": True}] * 7 + [{"prob": 0.7, "hit": False}] * 3
    err = calibration_error(calibration_buckets(records))
    assert err < 0.02


def test_calibration_detects_overconfidence():
    """说 90% 却只有 50% 命中 → 校准误差应接近 0.4。"""
    records = [{"prob": 0.9, "hit": (i % 2 == 0)} for i in range(20)]
    err = calibration_error(calibration_buckets(records))
    assert 0.35 < err < 0.45


def test_calibration_buckets_skips_empty():
    records = [{"prob": 0.5, "hit": True}]
    buckets = calibration_buckets(records)
    assert all(b["n"] > 0 for b in buckets)


def test_calibration_error_empty_is_zero():
    assert calibration_error([]) == 0.0


# ============================================================================
# 二、滚动回测引擎（前视偏差是重点）
# ============================================================================

def make_matches(n=60, teams=6):
    """构造确定性数据：强队 A 常年赢弱队，用于验证 Elo 能学到东西。"""
    out = []
    for i in range(n):
        home = f"T{i % teams}"
        away = f"T{(i + 1) % teams}"
        # 让 T0 明显更强：对谁都赢
        if home == "T0":
            hs, as_ = 3, 0
        elif away == "T0":
            hs, as_ = 0, 2
        else:
            hs, as_ = (1, 1) if i % 3 == 0 else (2, 1)
        out.append({
            "fixture_id": 1000 + i, "home_team_id": home, "away_team_id": away,
            "home_score": hs, "away_score": as_, "utc_date": f"2026-01-{i + 1:02d}",
        })
    return out


def test_no_lookahead_first_match_uses_default():
    """第一场比赛时没有任何历史，必须用默认概率，不能偷看未来。"""
    bt = WalkForwardBacktester(min_history=0)
    probs = bt._predict("T0", "T1")
    assert abs(sum(probs) - 1.0) < 1e-9


def test_history_grows_as_matches_observed():
    bt = WalkForwardBacktester(min_history=0)
    bt._observe("A", "B", 2, 0)
    assert bt._team_stats["A"]["home_played"] == 1
    assert bt._team_stats["B"]["away_played"] == 1
    assert bt._seen == 1


def test_min_history_excludes_early_matches():
    """前 N 场不纳入评估——样本太少时的预测没有参考价值。"""
    bt = WalkForwardBacktester(min_history=30)
    report = bt.run(make_matches(60))
    assert report.n == 30  # 60 场里只有后 30 场参与评估


def test_min_history_zero_includes_all():
    bt = WalkForwardBacktester(min_history=0)
    report = bt.run(make_matches(60))
    assert report.n == 60


def test_ratings_actually_change():
    """Elo 分必须在回测过程中真的变化，否则等于没接。"""
    bt = WalkForwardBacktester(min_history=0)
    bt.run(make_matches(60))
    ratings = bt._ratings
    assert max(ratings.values()) - min(ratings.values()) > 50


def test_strongest_team_has_highest_rating():
    """T0 在所有比赛里都赢，回测结束时它必须是最高分。"""
    bt = WalkForwardBacktester(min_history=0)
    bt.run(make_matches(60))
    best = max(bt._ratings, key=lambda k: bt._ratings[k])
    assert best == "T0"


def test_report_probabilities_valid():
    bt = WalkForwardBacktester(min_history=10)
    report = bt.run(make_matches(60))
    assert report.n > 0
    assert report.log_loss is not None and report.log_loss > 0
    assert report.rps_score is not None
    assert 0.0 <= (report.accuracy_rate or 0) <= 1.0


def test_backtest_handles_bad_rows():
    """脏数据不能让回测崩溃。"""
    bt = WalkForwardBacktester(min_history=1)
    rows = [
        {"home_team_id": "A", "away_team_id": "B", "home_score": None, "away_score": 1},
        {"home_team_id": "A", "away_team_id": "B", "home_score": "x", "away_score": 1},
        {"home_team_id": None, "away_team_id": "B", "home_score": 1, "away_score": 1},
        {"home_team_id": "A", "away_team_id": "B", "home_score": 1, "away_score": 0},
    ]
    report = bt.run(rows)
    assert report.n == 0  # 没有一行能进入评估，但不该抛异常


def test_backtest_empty_input():
    assert WalkForwardBacktester().run([]).n == 0


# ============================================================================
# 三、对比与结论
# ============================================================================

def test_compare_insufficient_sample():
    base = WalkForwardBacktester(min_history=0).run([])
    chal = WalkForwardBacktester(min_history=0).run([])
    assert compare(base, chal)["verdict"] == "样本不足"


def test_compare_identical_is_no_difference():
    matches = make_matches(80)
    a = WalkForwardBacktester(min_history=20, use_elo=False).run(matches)
    b = WalkForwardBacktester(min_history=20, use_elo=False).run(matches)
    assert compare(a, b)["verdict"] == "无显著差异"


def test_run_backtest_returns_both_paths():
    result = run_backtest(make_matches(80), min_history=20)
    assert "comparison" in result
    assert result["comparison"]["baseline"]["label"] == "poisson"
    assert result["comparison"]["challenger"]["label"] == "elo"
    assert result["comparison"]["baseline"]["n"] > 0
    assert result["comparison"]["challenger"]["n"] > 0


def test_level_monotonic_structure():
    bt = WalkForwardBacktester(min_history=20)
    report = bt.run(make_matches(80))
    info = level_monotonic(report)
    assert "rates" in info and "monotonic" in info
    for k, v in info["rates"].items():
        assert 0.0 <= v <= 1.0
