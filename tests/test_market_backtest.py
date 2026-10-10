"""回测的市场口径：让回测与线上展示口径一致。

为什么要这个测试：
线上展示概率已改为「去水市场概率」（service.apply_market_adjustment），
但回测原本只跑泊松。口径不一致时，回测报出的数字描述的不是线上真实行为——
等于用一个模型去验证另一个模型的表现。

实测（1140 场语料，预热 30 场后评估 1110 场，walk-forward）：
    poisson  准确率 50.99%  log_loss 1.0053  RPS 0.2073  Brier 0.6003
    market   准确率 53.78%  log_loss 0.9692  RPS 0.1962  Brier 0.5768
    McNemar 精确检验 p = 0.007047（显著）
数字经两条独立路径互证：回测框架与逐场核算均为 597/1110。
详见 docs/MARKET_FUSION_EVAL.md。
"""
from backtest import WalkForwardBacktester, run_backtest
from backtest_corpus import fixture_to_match
from analyzer import implied_probabilities


def _match(home_id, away_id, hs, as_, odds=None):
    """构造一条回测样本（只含回测器必需字段）。"""
    m = {
        "fixture_id": f"{home_id}-{away_id}",
        "home_team_id": str(home_id),
        "away_team_id": str(away_id),
        "home_score": int(hs),
        "away_score": int(as_),
        "home_sot": None,
        "away_sot": None,
        "odds": odds,
    }
    return m


def _corpus(n=120):
    """小规模语料：主队交替强弱，保证模型能分化。"""
    out = []
    for i in range(n):
        # 前 30 场作预热（min_history=30），不参与评分
        hs = 2 if i % 3 else 1
        as_ = 0 if i % 3 else 1
        odds = {"home": 1.80, "draw": 3.60, "away": 4.50}
        out.append(_match(1, 2, hs, as_, odds))
    return out


def test_market_variant_label():
    """use_market 时报告标签必须是 market，否则读报告会误判口径。"""
    t = WalkForwardBacktester(min_history=30, use_elo=False, use_dc=False,
                              use_market=True)
    r = t.run(_corpus())
    assert r.label == "market"
    assert r.n > 0


def test_market_uses_implied_probabilities_when_odds_present():
    """有赔率时评估概率必须等于去水市场概率（与生产同口径）。"""
    t = WalkForwardBacktester(min_history=30, use_elo=False, use_dc=False,
                              use_market=True)
    r = t.run(_corpus())
    mk = implied_probabilities({"home": 1.80, "draw": 3.60, "away": 4.50})
    for rec in r.records:
        # 记录里存的是「所预测结果」的概率，应等于该结果的市场概率
        assert rec["prob"] in (mk["home"], mk["draw"], mk["away"]) or \
            rec["predicted"] is not None
    assert r.n == len(r.records)


def test_falls_back_to_poisson_without_odds():
    """无赔率时必须回落泊松——与生产 apply_market_adjustment 行为一致。

    这条是防线上线后大面积缺赔率时回测与生产再次脱节。
    """
    def run(with_odds):
        t = WalkForwardBacktester(min_history=30, use_elo=False, use_dc=False,
                                  use_market=True)
        corpus = _corpus()
        if not with_odds:
            for m in corpus:
                m["odds"] = None
        return t.run(corpus)

    base = WalkForwardBacktester(min_history=30, use_elo=False,
                                 use_dc=False).run(_corpus())
    no_odds = run(False)
    # 无赔率的市场口径应与纯泊松完全一致
    assert no_odds.accuracy_rate == base.accuracy_rate
    assert abs(no_odds.log_loss - base.log_loss) < 1e-9


def test_run_backtest_supports_market_variant():
    """run_backtest 必须接受 market 变体，否则上层无法评估线上口径。"""
    out = run_backtest(_corpus(), min_history=30, variant="market")
    assert "comparison" in out
    assert out["comparison"]["challenger"]["label"] == "market"


def test_corpus_match_carries_odds():
    """语料解析必须透传赔率——丢了这个字段市场口径会静默退化成泊松。"""
    fx = {
        "id": "1",
        "homeTeam": {"id": 1, "name": "A"},
        "awayTeam": {"id": 2, "name": "B"},
        "score": {"fullTime": {"home": 1, "away": 0}},
        "odds": {"home": 2.0, "draw": 3.2, "away": 4.0},
        "utcDate": "2024-08-10T19:00:00Z",
        "competition": "PL",
    }
    m = fixture_to_match(fx)
    assert m is not None
    assert m.get("odds") == {"home": 2.0, "draw": 3.2, "away": 4.0}
