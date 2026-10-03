"""赛季初「强队主场」校准闸门（合理性断言，不是函数自证）。

为什么要有这个文件
------------------
此前的单元测试只验证「函数按实现跑」，函数本身算得对，测试就绿；
但输出在业务上是否说得通，没人检查。典型后果：赛季初（每队样本 3-4 场）
``PRIOR_GAMES=5`` 的收缩把强队和弱队一起拉向联赛平均，导致模型给阿森纳
这种主场强队只有 ~48% 主胜，而市场去水 70.9%。

本测试用**镜像内置的真实历史 CSV**（真赛果 + 真赔率）离线复现该场景，
直接断言「模型对强队主场的估计不得低于实际 / 市场太多」。
只要模型重新犯这个错，这里立刻变红。

判定口径
--------
- 早赛季：取每赛季前 30~80 场（约 3-8 轮）作为已赛样本，预测紧随的 10 场
- 强队主场：市场去水主胜概率 ≥ 0.60
- 断言：模型均值与实际主胜率、与市场去水均值的差距均需在容差内
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import pytest

from analyzer import MatchAnalyzer, build_league_model

HIST_DIR = Path(__file__).resolve().parent.parent / "data" / "history"

# 容差：模型与实际主胜率的最大允许差距（5pp）
MAX_GAP_VS_ACTUAL = 0.05
# 容差：模型与市场去水均值的最大允许差距（8pp，市场本身含信息优势）
MAX_GAP_VS_MARKET = 0.08


def _load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("FTR")]


def _devig(h, d, a):
    try:
        raw = [1.0 / float(h), 1.0 / float(d), 1.0 / float(a)]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    s = sum(raw)
    if s <= 0:
        return None
    return [x / s for x in raw]


def _standings(rows: list[dict], tid: dict[str, str] | None = None):
    """tid 为跨赛季共享的「队名 → id」映射；不传则当场生成。"""
    st: dict[str, dict] = defaultdict(lambda: {
        "home": {"played": 0.0, "goals": {"for": 0.0, "against": 0.0}},
        "away": {"played": 0.0, "goals": {"for": 0.0, "against": 0.0}},
    })
    for r in rows:
        h, a = r["HomeTeam"], r["AwayTeam"]
        hg, ag = float(r["FTHG"]), float(r["FTAG"])
        st[h]["home"]["played"] += 1
        st[h]["home"]["goals"]["for"] += hg
        st[h]["home"]["goals"]["against"] += ag
        st[a]["away"]["played"] += 1
        st[a]["away"]["goals"]["for"] += ag
        st[a]["away"]["goals"]["against"] += hg
    if tid is None:
        tid = {name: str(i) for i, name in enumerate(sorted(st))}
    for name in st:
        tid.setdefault(name, f"x{len(tid)}")
    out = [{"team": {"id": tid[name]}, **d} for name, d in st.items()]
    return out, tid


@pytest.fixture(scope="module")
def early_strong_home() -> dict:
    """早赛季 + 市场判定为主场强队 的样本聚合。"""
    files = sorted(HIST_DIR.glob("E0_*.csv"))
    if not files:
        pytest.skip("内置历史数据缺失")
    analyzer = MatchAnalyzer()
    acc = {"n": 0, "model": 0.0, "market": 0.0, "actual": 0}
    # 跨赛季共享的队名 → id 空间（先验与当季必须用同一套 id）
    shared: dict[str, str] = {}
    seasons = []
    for p in files:
        rows = _load(p)
        rows.sort(key=lambda r: r["Date"])
        for r in rows:
            shared.setdefault(r["HomeTeam"], f"t{len(shared)}")
            shared.setdefault(r["AwayTeam"], f"t{len(shared)}")
        seasons.append(rows)
    for idx, rows in enumerate(seasons):
        # 上赛季最终强度 → 先验目标（season_prior 的机制：整季 standings 建模）
        prior = None
        if idx > 0:
            prev_st, _ = _standings(seasons[idx - 1], dict(shared))
            prev_model = build_league_model(prev_st)
            prior = {}
            for row in prev_st:
                s_ = prev_model.strength(row["team"]["id"])
                prior[row["team"]["id"]] = {
                    "attack_home": s_.attack_home, "defense_home": s_.defense_home,
                    "attack_away": s_.attack_away, "defense_away": s_.defense_away,
                }
        for cutoff in range(30, 131, 10):
            if cutoff <= 80 and cutoff + 10 <= len(rows):
                played, upcoming = rows[:cutoff], rows[cutoff:cutoff + 10]
                st, tid = _standings(played, dict(shared))
                model = build_league_model(st, prior_strength=prior)
                for r in upcoming:
                    if r["HomeTeam"] not in tid or r["AwayTeam"] not in tid:
                        continue
                    mk = _devig(r.get("AvgH"), r.get("AvgD"), r.get("AvgA"))
                    if not mk or mk[0] < 0.60:
                        continue
                    pred = analyzer.predict_match(
                        model, tid[r["HomeTeam"]], tid[r["AwayTeam"]])
                    acc["n"] += 1
                    acc["model"] += pred["win_prob"]
                    acc["market"] += mk[0]
                    acc["actual"] += 1 if r["FTR"] == "H" else 0
    return acc


def test_season_start_strong_home_not_underestimated_vs_actual(early_strong_home):
    """模型对强队主场的估计，不得低于实际主胜率 5pp 以上。"""
    a = early_strong_home
    assert a["n"] >= 30, f"样本不足，无法判定：{a['n']}"
    model = a["model"] / a["n"]
    actual = a["actual"] / a["n"]
    gap = actual - model
    assert gap <= MAX_GAP_VS_ACTUAL, (
        f"赛季初低估强队主场：模型 {model:.1%} < 实际 {actual:.1%}"
        f"（低估 {gap * 100:.1f}pp，容差 {MAX_GAP_VS_ACTUAL * 100:.0f}pp）"
    )


def test_season_start_strong_home_not_underestimated_vs_market(early_strong_home):
    """模型对强队主场的估计，不得低于市场去水 8pp 以上。"""
    a = early_strong_home
    assert a["n"] >= 30, f"样本不足，无法判定：{a['n']}"
    model = a["model"] / a["n"]
    market = a["market"] / a["n"]
    gap = market - model
    assert gap <= MAX_GAP_VS_MARKET, (
        f"赛季初低估强队主场：模型 {model:.1%} < 市场 {market:.1%}"
        f"（低估 {gap * 100:.1f}pp，容差 {MAX_GAP_VS_MARKET * 100:.0f}pp）"
    )
