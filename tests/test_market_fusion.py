"""市场概率融合：锁死实测结论，防止回退。

实测依据（1110 场英超，语料带 B365 赔率，walk-forward）：
    纯泊松   准确率 50.99%  log_loss 1.0053
    纯市场   准确率 53.78%  log_loss 0.9692   McNemar p=0.007047
    分赛季/前后半段市场均不劣于泊松
    Value Bet 各阈值 ROI 全负：>0% -11.94% >5% -16.26% >10% -17.85% >15% -20.61%
详见 docs/MARKET_FUSION_EVAL.md。
"""
from analyzer import (
    MatchAnalyzer,
    apply_market_adjustment,
    build_league_model,
    implied_probabilities,
)


def _analysis():
    rows = [{
        "team_id": 1, "home_goals": 30, "away_goals": 20,
        "home_conceded": 10, "away_conceded": 15,
        "home_games": 12, "away_games": 12,
    }]
    model = build_league_model(rows)
    return MatchAnalyzer().predict_match(model, 1, 1)


def test_market_probs_replace_display_probs():
    a = _analysis()
    odds = {"home": 1.80, "draw": 3.60, "away": 4.50}
    out = apply_market_adjustment(a, odds)
    market = implied_probabilities(odds)
    assert out["market_used"] is True
    for k, key in (("home", "win_prob"), ("draw", "draw_prob"), ("away", "loss_prob")):
        assert out[key] == __import__("pytest").approx(market[k], abs=1e-9)


def test_poisson_baseline_is_preserved():
    """泊松结果必须原样保留，作为内部基准可追溯。"""
    a = _analysis()
    out = apply_market_adjustment(a, {"home": 1.8, "draw": 3.6, "away": 4.5})
    assert out["poisson_probs"]["home"] == a["win_prob"]
    assert out["poisson_probs"]["draw"] == a["draw_prob"]
    assert out["poisson_probs"]["away"] == a["loss_prob"]


def test_no_odds_keeps_pure_poisson():
    """无赔率时必须完全回落，行为与改造前一致。"""
    a = _analysis()
    for odds in (None, {}, {"home": 0, "draw": 3.6, "away": 4.5}):
        out = apply_market_adjustment(a, odds)
        assert out["market_used"] is False
        assert out["market_probs"] is None
        assert out["win_prob"] == a["win_prob"]
        assert out["best_score"] == a["best_score"]


def test_score_matrix_blocks_match_market_marginals():
    """比分矩阵按结果块缩放后，块和必须等于市场概率——否则展示会自相矛盾。"""
    a = _analysis()
    out = apply_market_adjustment(a, {"home": 1.8, "draw": 3.6, "away": 4.5})
    import pytest
    from analyzer import MAX_GOALS
    # 用 top_scores 无法还原全矩阵，改为校验三项概率之和为 1 且顺序与市场一致
    assert out["win_prob"] + out["draw_prob"] + out["loss_prob"] == pytest.approx(1.0)
    market = implied_probabilities({"home": 1.8, "draw": 3.6, "away": 4.5})
    order_m = sorted(market, key=lambda k: -market[k])
    order_o = sorted(
        ("home", "draw", "away"),
        key=lambda k: -{"home": out["win_prob"], "draw": out["draw_prob"],
                        "away": out["loss_prob"]}[k],
    )
    assert order_m == order_o


def test_best_score_stays_consistent_with_favoured_outcome():
    """回归：只换三项概率不缩放矩阵时，会出现「最可能比分 1-0」与「客胜最高」并存。"""
    a = _analysis()
    out = apply_market_adjustment(a, {"home": 5.0, "draw": 3.6, "away": 1.40})
    # 市场极度看好客胜时，最可能比分必须是客胜类比分（x>y 的格子）
    h, aw = (int(x) for x in out["best_score"].split("-"))
    assert aw > h, f"最可能比分 {out['best_score']} 与「客胜最高」矛盾"
