"""赛前信号对照：采集与统计口径 / Signal A/B capture & statistics.

覆盖用户评审里的生产链路接入与统计口径要求：
  * 采集器接进预测流程后真的会写库（此前存储层完备但无人调用）
  * 未触发的信号不写空行（否则统计会被稀释成"信号没用"）
  * signal 版算不出来 → partial_failure，而不是拿 baseline 冒充
  * 命中率 / Log Loss 分「全部」与「强信号场」两个口径，主指标是强信号场
  * 样本不足时明确说不足，不拿噪声下结论
"""
from __future__ import annotations

import pytest

from repository import PredictionRepository
from signal_ab import (CHANGE_REVOKED, SIGNAL_INJURY, SIGNAL_LINEUP,
                       SIGNAL_RATING, SIGNAL_WEATHER, STATUS_PARTIAL_FAILURE,
                       summarise, verdict)
from signal_capture import (_is_neutral, capture, raw_strength,
                            split_factors)


@pytest.fixture()
def repo(tmp_path):
    return PredictionRepository(str(tmp_path / "cap.db"))


class FakeAnalyzer:
    """最小可预测替身：施加信号就往主胜方向偏一点，便于断言三版不同。"""

    def __init__(self, shift: float = 0.03, broken: bool = False):
        self.shift = shift
        self.broken = broken
        self.calls: list[object] = []

    def predict_match(self, model, home_id, away_id, temperature=None,
                      prematch=None):
        self.calls.append(prematch)
        if self.broken and prematch:
            raise RuntimeError("boom")
        # 让概率随"施加了多少修正量"变化，这样 pure/partial/signal 三版
        # 真的不同 —— 只看"有没有 prematch"的替身会让 partial 与 signal 相等。
        dev = 0.0
        if prematch:
            for side in ("home", "away"):
                part = prematch.get(side) or {}
                dev += abs(float(part.get("attack", 1.0)) - 1.0)
                dev += abs(float(part.get("concede", 1.0)) - 1.0)
            dev += abs(float(prematch.get("weather", 1.0)) - 1.0)
        win = 0.45 + self.shift * round(dev, 6)
        return {"win_prob": win, "draw_prob": 0.27,
                "loss_prob": round(1.0 - win - 0.27, 6)}


def _injury(team_id=1):
    return [{"team": {"id": str(team_id)},
             "player": {"id": 9, "type": "Missing Fixture"}}]


# ---- 分裂因子 --------------------------------------------------------------


def test_no_data_means_every_signal_is_neutral():
    """无数据时四类因子全部中性 —— 未触发就不该产生对照行。"""
    factors = split_factors(1, 2, prematch_strength=0.5)
    assert set(factors) == {SIGNAL_INJURY, SIGNAL_LINEUP, SIGNAL_RATING,
                            SIGNAL_WEATHER}
    assert all(_is_neutral(v) for v in factors.values())


def test_injury_only_triggers_injury_signal():
    """只有伤停时，阵容/评分/天气三类必须保持中性，否则归因就错了。"""
    factors = split_factors(1, 2, injuries=_injury(1), prematch_strength=0.5)
    assert not _is_neutral(factors[SIGNAL_INJURY])
    assert _is_neutral(factors[SIGNAL_LINEUP])
    assert _is_neutral(factors[SIGNAL_RATING])
    assert _is_neutral(factors[SIGNAL_WEATHER])


def test_raw_strength_is_stored_unrounded():
    """原始强度要能重算，不能只存归一化值。"""
    factors = split_factors(1, 2, injuries=_injury(1), prematch_strength=0.5)
    assert raw_strength(SIGNAL_INJURY, factors[SIGNAL_INJURY]) == 1.0
    assert raw_strength(SIGNAL_WEATHER, factors[SIGNAL_WEATHER]) == 0.0


# ---- 采集写入 --------------------------------------------------------------


def test_capture_writes_one_row_per_active_signal(repo):
    """两种信号同时触发 → 两行，且各自的 partial 只缺自己那一种。"""
    factors = split_factors(1, 2, injuries=_injury(1), prematch_strength=0.5)
    factors[SIGNAL_RATING] = {"home": {"attack": 1.1, "concede": 0.95},
                              "away": {"attack": 1.0, "concede": 1.0},
                              "weather": 1.0, "detail": {}}
    analyzer = FakeAnalyzer()
    written = capture(repo, fixture_id=11, analyzer=analyzer, model=None,
                      home_id=1, away_id=2, by_signal=factors)
    assert written == 2
    rows = repo.signal_ab_rows()
    assert {r["signal_type"] for r in rows} == {SIGNAL_INJURY, SIGNAL_RATING}


def test_capture_stores_three_distinct_variants(repo):
    """pure / partial / signal 三版必须真的不同，否则对照毫无意义。"""
    factors = split_factors(1, 2, injuries=_injury(1), prematch_strength=0.5)
    factors[SIGNAL_RATING] = {"home": {"attack": 1.1, "concede": 0.95},
                              "away": {"attack": 1.0, "concede": 1.0},
                              "weather": 1.0, "detail": {}}
    capture(repo, fixture_id=12, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=factors)
    row = [r for r in repo.signal_ab_rows()
           if r["signal_type"] == SIGNAL_INJURY][0]
    pure, partial, signal = (row["pure_baseline"], row["partial_baseline"],
                             row["signal_prediction"])
    # partial 拿着另一种信号，所以与 pure 不同；signal 两种都拿，又与 partial 不同
    assert pure != partial and partial != signal
    assert row["human_note"]


def test_capture_skips_rows_when_nothing_fired(repo):
    """所有信号都中性时不写行 —— 空行会把统计稀释成"信号没用"。"""
    capture(repo, fixture_id=13, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=split_factors(1, 2))
    assert repo.signal_ab_rows() == []


def test_signal_failure_recorded_as_partial_not_fake(repo):
    """signal 版算不出来 → partial_failure + NULL，绝不拿 baseline 顶替。"""
    factors = split_factors(1, 2, injuries=_injury(1), prematch_strength=0.5)
    capture(repo, fixture_id=14, analyzer=FakeAnalyzer(broken=True),
            model=None, home_id=1, away_id=2, by_signal=factors)
    row = repo.signal_ab_rows()[0]
    assert row["status"] == STATUS_PARTIAL_FAILURE
    assert row["signal_prediction"] is None
    assert row["pure_baseline"]        # baseline 仍在，供后续排查


def test_capture_never_raises(repo):
    """采集挂了只告警 —— 不能因为它让今天的预测发不出去。"""
    class Boom:
        def predict_match(self, *a, **k):
            raise RuntimeError("boom")
    assert capture(repo, fixture_id=15, analyzer=Boom(), model=None,
                   home_id=1, away_id=2,
                   by_signal=split_factors(1, 2, injuries=_injury(1),
                                           prematch_strength=0.5)) == 0


# ---- 统计口径 --------------------------------------------------------------


def _row(sig_type, strength, base_w, sig_w, home, away):
    def _p(w):
        # 剩余概率平分给平/客，保证三项恒为正 —— 高主胜概率时写成
        # "away 为负"会被 normalise_probs 判非法，统计就静默少样本。
        rest = round(1.0 - w, 4)
        return '{"home": %s, "draw": %s, "away": %s}' % (w, rest / 2, rest / 2)
    return {"signal_type": sig_type, "signal_strength": strength,
            "pure_baseline": _p(base_w), "partial_baseline": _p(base_w),
            "signal_prediction": _p(sig_w),
            "actual_home": home, "actual_away": away}


def test_strong_is_headline_and_weak_rows_excluded():
    """主指标只算强信号场；弱信号行留在 all 口径里但不能拿来下结论。"""
    rows = [_row(SIGNAL_INJURY, 0.40, 0.44, 0.48, 2, 1),     # 强，两版都命中
            _row(SIGNAL_INJURY, 0.01, 0.44, 0.44, 0, 3)]     # 弱，两版都不中
    out = summarise(rows, min_samples=1)
    entry = out["by_signal"][SIGNAL_INJURY]
    assert entry["strong"]["n"] == 1
    assert entry["all"]["n"] == 2
    assert entry["strong"]["hit_signal"] == 1.0
    assert entry["all"]["hit_signal"] == 0.5      # 混进弱信号后被稀释


def test_insufficient_samples_reported_not_guessed():
    """样本不足时只报数量，不报命中率 —— 3 场全中显示 100% 会骗人。"""
    rows = [_row(SIGNAL_INJURY, 0.5, 0.44, 0.48, 2, 1) for _ in range(3)]
    out = summarise(rows, min_samples=30)
    entry = out["by_signal"][SIGNAL_INJURY]
    assert entry["strong"]["insufficient"] is True
    assert "样本不足" in verdict(entry)


def test_logloss_improvement_is_negative_delta():
    """信号把概率推对了 → Log Loss 差值为负（越低越准）。"""
    rows = [_row(SIGNAL_INJURY, 0.5, 0.40, 0.80, 2, 1) for _ in range(40)]
    out = summarise(rows, min_samples=10)
    entry = out["by_signal"][SIGNAL_INJURY]
    assert entry["strong"]["logloss_delta"] < 0
    assert "更好" in verdict(entry)


def test_unsettled_rows_are_not_counted():
    """未结算的行不能参与统计，否则比分缺失会被当成 0-0。"""
    rows = [_row(SIGNAL_INJURY, 0.5, 0.44, 0.48, 2, 1),
            {"signal_type": SIGNAL_INJURY, "signal_strength": 0.5,
             "pure_baseline": None, "partial_baseline": None,
             "signal_prediction": None, "actual_home": None,
             "actual_away": None}]
    out = summarise(rows, min_samples=1)
    assert out["by_signal"][SIGNAL_INJURY]["strong"]["n"] == 1


def test_verdict_says_no_difference_when_ci_crosses_zero():
    """两版完全一样时置信区间必然跨 0 → 明说无差异，不说"有效"。"""
    rows = [_row(SIGNAL_INJURY, 0.5, 0.44, 0.44, 2, 1) for _ in range(40)]
    entry = summarise(rows, min_samples=10)["by_signal"][SIGNAL_INJURY]
    assert "无显著差异" in verdict(entry)


def test_summarise_tolerates_empty_and_junk():
    """复盘不能因为读不到对照数据就整个挂掉。"""
    assert summarise([])["total"] == 0
    assert summarise([object(), None])["total"] == 0


# ---- 信号撤销 --------------------------------------------------------------


def _injury_factors():
    return split_factors(1, 2, injuries=_injury(1), prematch_strength=0.5)


def test_signal_withdrawn_becomes_revoked_row(repo):
    """信号消失（伤停球员赛前复出）必须写一行 revoked，而不是静默不写。

    不写的话，复盘会把"信号撤销后仍然命中的比赛"错算成"信号有效"，
    结论系统性偏乐观。
    """
    capture(repo, fixture_id=21, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=_injury_factors())
    assert repo.signal_ab_rows()[0]["change_type"] == "applied"

    # 第二轮：伤停消失（球员复出）
    written = capture(repo, fixture_id=21, analyzer=FakeAnalyzer(), model=None,
                      home_id=1, away_id=2, by_signal=split_factors(1, 2))
    assert written == 1
    row = repo.signal_ab_rows()[0]
    assert row["change_type"] == CHANGE_REVOKED
    assert row["signal_strength"] == 0.0


def test_first_capture_never_writes_revoked(repo):
    """没有历史就没有"撤销"——首次采集不该凭空产生 revoked 行。"""
    capture(repo, fixture_id=22, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=_injury_factors())
    assert all(r["change_type"] == "applied" for r in repo.signal_ab_rows())


def test_revoked_row_keeps_pure_baseline(repo):
    """撤销行仍要留下 pure 版预测，排查时能看出"没信号会怎么说"。"""
    capture(repo, fixture_id=23, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=_injury_factors())
    capture(repo, fixture_id=23, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=split_factors(1, 2))
    row = repo.signal_ab_rows()[0]
    assert row["pure_baseline"]
    assert "撤销" in row["human_note"]


def test_revoked_row_is_not_written_twice(repo):
    """第二轮已经撤销后，第三轮不该再把它当"新撤销"重复计数。"""
    capture(repo, fixture_id=24, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=_injury_factors())
    capture(repo, fixture_id=24, analyzer=FakeAnalyzer(), model=None,
            home_id=1, away_id=2, by_signal=split_factors(1, 2))
    written = capture(repo, fixture_id=24, analyzer=FakeAnalyzer(), model=None,
                      home_id=1, away_id=2, by_signal=split_factors(1, 2))
    assert written == 0        # 已撤销且无新信号 → 无需再写
    assert len(repo.signal_ab_rows()) == 1


def test_revoked_rows_excluded_from_statistics():
    """撤销行必须排除在统计之外 —— 它代表"信号已经不在预测里了"。"""
    applied = [_row(SIGNAL_INJURY, 0.5, 0.44, 0.44, 2, 1) for _ in range(40)]
    revoked = dict(applied[0])
    revoked["change_type"] = CHANGE_REVOKED
    out = summarise(applied + [revoked], min_samples=10)
    entry = out["by_signal"][SIGNAL_INJURY]
    assert entry["strong"]["n"] == 40      # 撤销行没被算进去
    assert out["revoked"] == 1
    assert out["revoked_by_type"][SIGNAL_INJURY] == 1
    assert entry["n_revoked"] == 1


def test_revoked_only_rows_do_not_create_empty_verdict():
    """全是撤销行时统计为空，但不能因此报错或凭空造结论。"""
    row = _row(SIGNAL_INJURY, 0.5, 0.44, 0.48, 2, 1)
    row["change_type"] = CHANGE_REVOKED
    out = summarise([row], min_samples=1)
    assert out["total"] == 0
    assert out["revoked"] == 1


def test_capture_is_not_gated_on_prematch_presence():
    """采集不能被「prematch 非空」挡住 —— prematch 为 None 正是信号全撤的场景。

    若加上 `prematch and`，信号一旦全部消失采集就被整体跳过，撤销行永远
    写不出来，复盘会把「撤销后仍命中」错算成「信号有效」。
    """
    src = open("service.py", encoding="utf-8").read()
    marker = 'getattr(self.settings, "signal_ab_enabled", False)'
    assert marker in src
    snippet = src[src.index(marker):src.index(marker) + 200]
    assert "prematch and" not in snippet
