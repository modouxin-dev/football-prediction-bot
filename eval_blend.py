"""市场锚定融合实测脚本（一次性评估工具，不进生产路径）。

目的：在内置 1140 场含赔率语料上，用 walk-forward 实测各方案的
RPS / Log Loss / 准确率，用来决定「要不要上市场锚定、权重取多少」。

为什么必须实测而不是照抄论文数字：
    论文的 RPS 差距（赔率 vs 统计模型 0.004~0.010）是在各自数据集上测的，
    换到本项目的数据（英超近三赛季 + 本项目泊松实现）未必成立。
    权重 w 更是强数据依赖——必须用本项目的语料拟合。

用法：
    python eval_blend.py
"""

from __future__ import annotations

import statistics
import sys
from typing import Any

from analyzer import fit_rho
from backtest import (
    DEFAULT_MIN_HISTORY,
    WalkForwardBacktester,
    log_loss,
    outcome_index,
    rps,
    favorite_index,
    accuracy,
)
from backtest_corpus import fixture_to_match
from football_data_uk import load_local_all

OUTCOMES = ("home", "draw", "away")


def load_matches() -> list[dict]:
    """加载语料并转成回测 match 结构，同时保留市场概率。"""
    out: list[dict] = []
    for fx in load_local_all():
        m = fixture_to_match(fx)
        if m is None:
            continue
        m["marketProbabilities"] = fx.get("marketProbabilities")
        out.append(m)
    out.sort(key=lambda m: (m.get("utc_date") or ""))
    return out


def market_probs_of(match: dict) -> tuple[float, float, float] | None:
    """取该场去水市场概率，取不到返回 None。"""
    mp = match.get("marketProbabilities")
    if not mp:
        return None
    try:
        # football_data_uk 用中文键
        vals = (mp.get("主胜"), mp.get("平局"), mp.get("客胜"))
        if vals[0] is None:
            vals = (mp.get("home"), mp.get("draw"), mp.get("away"))
        h, d, a = (float(v) for v in vals)
    except (TypeError, ValueError):
        return None
    total = h + d + a
    if total <= 0:
        return None
    return (h / total, d / total, a / total)


def blend(model_p: tuple[float, float, float],
          market_p: tuple[float, float, float],
          w: float) -> tuple[float, float, float]:
    """P_final = w·市场 + (1−w)·模型。w=0 纯模型，w=1 纯市场。"""
    out = tuple(w * market_p[i] + (1.0 - w) * model_p[i] for i in range(3))
    s = sum(out)
    return tuple(v / s for v in out) if s > 0 else out


def fit_weight(samples: list[tuple[tuple, tuple, int]],
               grid: list[float]) -> tuple[float, dict[float, float]]:
    """在给定样本上网格搜索 w，按 RPS 取最优。返回 (最优w, {w: RPS})。"""
    scores: dict[float, float] = {}
    for w in grid:
        tot = 0.0
        for model_p, market_p, actual in samples:
            if market_p is None:
                p = model_p
            else:
                p = blend(model_p, market_p, w)
            tot += rps(p, actual)
        scores[w] = tot / len(samples) if samples else float("nan")
    best = min(scores, key=lambda k: scores[k])
    return best, scores


def run() -> int:
    matches = load_matches()
    if not matches:
        print("语料为空", file=sys.stderr)
        return 1
    n_odds = sum(1 for m in matches if market_probs_of(m) is not None)
    print(f"语料：{len(matches)} 场，其中含赔率 {n_odds} 场\n")

    # ---- 方案 A：纯泊松（线上当前口径） ----
    base = WalkForwardBacktester(min_history=DEFAULT_MIN_HISTORY,
                                 use_elo=False, use_dc=False).run(matches)
    # ---- 方案 B：Dixon-Coles ----
    dc = WalkForwardBacktester(min_history=DEFAULT_MIN_HISTORY,
                               use_elo=False, use_dc=True).run(matches)
    # ---- 方案 C：Elo ----
    elo = WalkForwardBacktester(min_history=DEFAULT_MIN_HISTORY,
                                use_elo=True, use_dc=False).run(matches)

    # ---- 重跑一遍拿逐场模型概率（与市场配对） ----
    # 复用 tester 的内部状态较难，改为直接跑一个收集器
    samples: list[tuple[tuple, tuple, int]] = []
    collector = _Collector(min_history=DEFAULT_MIN_HISTORY, use_dc=True)
    for m in matches:
        rec = collector.step(m)
        if rec is not None:
            samples.append(rec)

    with_odds = [s for s in samples if s[1] is not None]
    print(f"评估样本：{len(samples)} 场，含赔率 {len(with_odds)} 场\n")

    # ---- 方案 D：纯市场（只算含赔率场次） ----
    if with_odds:
        mkt_rps = statistics.fmean(rps(s[1], s[2]) for s in with_odds)
        mkt_ll = statistics.fmean(log_loss(s[1], s[2]) for s in with_odds)
        mkt_acc = statistics.fmean(
            accuracy(favorite_index(s[1]), s[2]) for s in with_odds)
    else:
        mkt_rps = mkt_ll = mkt_acc = float("nan")

    # DC 在含赔率子集上的表现（与市场可比）
    dc_on_odds_rps = statistics.fmean(rps(s[0], s[2]) for s in with_odds)
    dc_on_odds_ll = statistics.fmean(log_loss(s[0], s[2]) for s in with_odds)
    dc_on_odds_acc = statistics.fmean(
        accuracy(favorite_index(s[0]), s[2]) for s in with_odds)

    print("=== 全样本（三方案） ===")
    _row("纯泊松(线上)", base)
    _row("Dixon-Coles", dc)
    _row("Poisson+Elo", elo)
    print()
    print("=== 含赔率子集（与市场可比） ===")
    print(f"{' Dixon-Coles':16s} RPS {dc_on_odds_rps:.4f}  "
          f"LL {dc_on_odds_ll:.4f}  ACC {dc_on_odds_acc:.2%}")
    print(f"{' 市场(去水)':16s} RPS {mkt_rps:.4f}  "
          f"LL {mkt_ll:.4f}  ACC {mkt_acc:.2%}")
    print()

    # ---- 拟合权重：前半段拟合，后半段评估（遵守时序） ----
    if len(with_odds) >= 100:
        split = len(with_odds) // 2
        fit_part, eval_part = with_odds[:split], with_odds[split:]
        grid = [round(i * 0.05, 2) for i in range(21)]
        best_w, fit_scores = fit_weight(fit_part, grid)
        print(f"拟合段 {len(fit_part)} 场 → 最优 w = {best_w:.2f}")
        print("  拟合段 RPS 曲线（部分）：")
        for w in (0.0, 0.25, 0.5, 0.75, 1.0):
            print(f"    w={w:.2f}  RPS {fit_scores.get(w, float('nan')):.4f}")
        print()
        print(f"=== 留出段评估（{len(eval_part)} 场，未参与拟合） ===")
        for w in sorted({0.0, 0.25, best_w, 0.5, 0.75, 1.0}):
            rr = statistics.fmean(
                rps(blend(s[0], s[1], w) if s[1] else s[0], s[2])
                for s in eval_part)
            ll = statistics.fmean(
                log_loss(blend(s[0], s[1], w) if s[1] else s[0], s[2])
                for s in eval_part)
            ac = statistics.fmean(
                accuracy(favorite_index(
                    blend(s[0], s[1], w) if s[1] else s[0]), s[2])
                for s in eval_part)
            tag = "  ← 拟合所得" if abs(w - best_w) < 1e-9 else ""
            print(f"  w={w:.2f}  RPS {rr:.4f}  LL {ll:.4f}  ACC {ac:.2%}{tag}")
    return 0


def _row(label: str, rep: Any) -> None:
    print(f"{label:16s} n={rep.n:5d}  RPS {rep.rps_score:.4f}  "
          f"LL {rep.log_loss:.4f}  ACC {rep.accuracy_rate:.2%}")


class _Collector:
    """逐场收集 (模型概率, 市场概率, 实际结果)，内部状态与 WalkForwardBacktester 一致。"""

    def __init__(self, *, min_history: int, use_dc: bool):
        self.t = WalkForwardBacktester(min_history=min_history,
                                       use_elo=False, use_dc=use_dc)
        self.min_history = min_history

    def step(self, m: dict):
        hs, as_ = m.get("home_score"), m.get("away_score")
        if hs is None or as_ is None:
            return None
        hs, as_ = int(hs), int(as_)
        hid, aid = m.get("home_team_id"), m.get("away_team_id")
        if hid is None or aid is None:
            return None
        self.t._seen = getattr(self.t, "_seen", 0)
        # 预热/评分逻辑与 tester.run 保持一致
        if self.t._seen < self.min_history and self.t.use_dc and not self.t._rho_fitted:
            _, lh, la = self.t._predict_detail(hid, aid)
            if lh is not None:
                self.t._dc_observations.append((lh, la, hs, as_))
        rec = None
        if self.t._seen >= self.min_history:
            if self.t.use_dc and (
                not self.t._rho_fitted
                or (self.t.refit_every > 0
                    and self.t._seen % self.t.refit_every == 0
                    and len(self.t._dc_observations) >= self.t.min_fit_samples)
            ):
                self.t.rho = fit_rho(self.t._dc_observations)
                self.t._rho_fitted = True
            probs = self.t._predict(hid, aid)
            actual = outcome_index(hs, as_)
            rec = (probs, market_probs_of(m), actual)
        self.t._observe(hid, aid, hs, as_,
                        m.get("home_sot"), m.get("away_sot"))
        if self.t.use_dc and self.t._seen >= self.min_history:
            _, lh2, la2 = self.t._predict_detail(hid, aid)
            if lh2 is not None:
                self.t._dc_observations.append((lh2, la2, hs, as_))
        return rec


if __name__ == "__main__":
    raise SystemExit(run())
