import pytest

from analyzer import (
    MatchAnalyzer,
    TeamStrength,
    build_league_model,
    collect_1x2_odds,
    consensus_odds,
    overround,
)
from api_client import flatten_standings
from tests.sample_data import odds_response, standings_response

A = MatchAnalyzer()
AVERAGE = {"attack": 1.0, "defense": 1.0}


def test_probabilities_sum_to_one_and_symmetric_teams_are_even():
    r = A.calculate_prediction(AVERAGE, AVERAGE, 1.4, 1.4)
    assert r["win_prob"] + r["draw_prob"] + r["loss_prob"] == pytest.approx(1.0)
    assert r["win_prob"] == pytest.approx(r["loss_prob"])


def test_stronger_side_is_favoured():
    strong = {"attack": 1.4, "defense": 0.7}
    weak = {"attack": 0.7, "defense": 1.4}
    r = A.calculate_prediction(strong, weak, 1.5, 1.2)
    assert r["win_prob"] > 0.6 > r["loss_prob"]
    assert r["lambda_home"] > r["lambda_away"]


def test_top_scores_sorted_and_extra_markets_bounded():
    r = A.calculate_prediction(AVERAGE, AVERAGE, 1.5, 1.2)
    probs = [p for _, p in r["top_scores"]]
    assert len(probs) == 5 and probs == sorted(probs, reverse=True)
    assert r["best_score"] == r["top_scores"][0][0]
    assert 0 < r["over_2_5"] < 1 and 0 < r["btts"] < 1


def test_league_model_uses_real_averages_and_ranks_teams():
    model = build_league_model(flatten_standings(standings_response()))
    assert model.avg_home_goals == pytest.approx(35 / 24)
    assert model.avg_away_goals == pytest.approx(23 / 24)
    alpha, delta = model.strength(1), model.strength(4)
    assert alpha.attack_home > 1 > delta.attack_home
    assert alpha.defense_home < 1 < delta.defense_home  # 防守强度越低越好
    assert alpha.games_home == 6


def test_strengths_are_shrunk_toward_one_when_no_games_played():
    rows = [{"team": {"id": 9}, "home": {"played": 0, "goals": {"for": 0, "against": 0}}, "away": {"played": 0, "goals": {"for": 0, "against": 0}}}]
    s = build_league_model(rows).strength(9)
    assert (s.attack_home, s.defense_home, s.attack_away, s.defense_away) == pytest.approx((1, 1, 1, 1))


def test_unknown_team_and_empty_standings_fall_back_to_defaults():
    model = build_league_model([])
    assert model.strength(123) == TeamStrength()
    assert model.avg_home_goals > 0 and model.avg_away_goals > 0


def test_real_data_gives_different_predictions_per_match():
    """回归：旧版对所有比赛都用同一组写死的数据。"""
    model = build_league_model(flatten_standings(standings_response()))
    alpha_home = A.predict_match(model, 1, 4)
    delta_home = A.predict_match(model, 4, 1)
    assert alpha_home["win_prob"] > 0.6
    assert delta_home["win_prob"] < 0.3


def test_value_is_positive_exactly_when_expected_value_is_positive():
    assert A.analyze_value(0.5, 2.2) > 0 and 0.5 * 2.2 - 1 > 0
    assert A.analyze_value(0.4, 2.2) < 0 and 0.4 * 2.2 - 1 < 0
    assert A.analyze_value(0.5, 0) == 0  # 赔率缺失不应崩溃


def test_collect_odds_picks_match_winner_bet_not_first_bet():
    rows = collect_1x2_odds(odds_response([("Bet A", 1.80, 3.60, 4.50), ("Bet B", 1.90, 3.40, 4.20)]))
    assert [r["bookmaker"] for r in rows] == ["Bet A", "Bet B"]
    assert rows[0]["home"] == 1.80 and rows[0]["draw"] == 3.60 and rows[0]["away"] == 4.50


def test_collect_odds_skips_incomplete_or_invalid_rows():
    response = odds_response([("Bet A", 1.80, 3.60, 4.50)])
    response[0]["bookmakers"].append({"name": "Broken", "bets": [{"id": 1, "values": [{"value": "Home", "odd": "abc"}]}]})
    assert len(collect_1x2_odds(response)) == 1
    assert collect_1x2_odds(None) == [] and collect_1x2_odds([]) == []


def test_consensus_is_median_and_overround_is_positive():
    rows = collect_1x2_odds(odds_response([("A", 1.8, 3.6, 4.5), ("B", 1.9, 3.4, 4.2), ("C", 9.0, 9.0, 9.0)]))
    odds = consensus_odds(rows)
    assert odds["home"] == 1.9 and odds["n"] == 3  # 异常报价不影响中位数
    assert consensus_odds([]) is None
    assert overround({"home": 1.8, "draw": 3.6, "away": 4.5}) > 0


def test_evaluate_outcomes_covers_all_three_results():
    analysis = A.calculate_prediction(AVERAGE, AVERAGE, 1.5, 1.2)
    out = A.evaluate_outcomes(analysis, {"home": 2.0, "draw": 3.5, "away": 4.0})
    assert set(out) == {"home", "draw", "away"}
    assert out["home"]["edge"] == pytest.approx(analysis["win_prob"] - 0.5)
    assert A.evaluate_outcomes(analysis, None) == {}
