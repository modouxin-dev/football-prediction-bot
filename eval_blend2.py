"""第二阶段：细网格 + 滚动权重 + bootstrap 显著性检验。

回答三个问题：
  1. 最优 w 到底是多少（细网格 + 滚动重拟）
  2. 融合相对纯模型的改善是否显著（bootstrap 95% CI）
  3. DC / Elo 是否真的没用（与第一阶段结论互证）
"""

from __future__ import annotations

import random
import statistics

from backtest import (
    DEFAULT_MIN_HISTORY,
    accuracy,
    favorite_index,
    log_loss,
    rps,
)
from eval_blend import _Collector, blend, load_matches


def collect(use_dc: bool = False, use_elo: bool = False):
    ms = load_matches()
    c = _Collector(min_history=DEFAULT_MIN_HISTORY, use_dc=use_dc)
    c.t.use_elo = use_elo
    out = []
    for m in ms:
        r = c.step(m)
        if r is not None:
            out.append(r)
    return out


def rps_of(samples, w: float) -> float:
    tot = 0.0
    for mp_, mk_, ac in samples:
        p = blend(mp_, mk_, w) if mk_ else mp_
        tot += rps(p, ac)
    return tot / len(samples)


def ll_of(samples, w: float) -> float:
    tot = 0.0
    for mp_, mk_, ac in samples:
        p = blend(mp_, mk_, w) if mk_ else mp_
        tot += log_loss(p, ac)
    return tot / len(samples)


def acc_of(samples, w: float) -> float:
    hits = 0
    for mp_, mk_, ac in samples:
        p = blend(mp_, mk_, w) if mk_ else mp_
        hits += accuracy(favorite_index(p), ac)
    return hits / len(samples)


def rolling_weight(samples, grid, first_fit=200, refit_every=100):
    """滚动：每 refit_every 场用截止当时的历史重拟 w，之后固定用到下次重拟。

    严格无前视——拟合只用 i 之前的样本，绝不涉及 i 及之后。
    """
    preds_model, preds_blend, actuals = [], [], []
    chosen = []
    cur_w = grid[len(grid) // 2]
    for i in range(first_fit, len(samples)):
        if (i - first_fit) % refit_every == 0:
            hist = samples[:i]
            cur_w, _ = min(((w, rps_of(hist, w)) for w in grid),
                           key=lambda t: t[1])
        chosen.append(cur_w)
        mp_, mk_, ac = samples[i]
        preds_model.append(mp_)
        preds_blend.append(blend(mp_, mk_, cur_w) if mk_ else mp_)
        actuals.append(ac)
    return preds_model, preds_blend, actuals, chosen


def boot_ci(pairs_a, pairs_b, n=2000, seed=7):
    """配对 bootstrap：返回 (均值差, 低, 高)。差<0 表示 a 更好。"""
    rnd = random.Random(seed)
    diffs = [a - b for a, b in zip(pairs_a, pairs_b)]
    m = len(diffs)
    if m == 0:
        return float("nan"), float("nan"), float("nan")
    boots = []
    for _ in range(n):
        s = sum(diffs[rnd.randrange(m)] for _ in range(m))
        boots.append(s / m)
    boots.sort()
    mean = statistics.fmean(diffs)
    return mean, boots[int(0.025 * n)], boots[int(0.975 * n)]


def main() -> int:
    samples = collect(use_dc=False)
    with_odds = [s for s in samples if s[1] is not None]
    print(f"样本 {len(samples)}，含赔率 {len(with_odds)}\n")

    # ---- 1. 细网格（留出法：后半段评估） ----
    split = len(with_odds) // 2
    fit_p, eval_p = with_odds[:split], with_odds[split:]
    grid = [round(0.30 + i * 0.02, 2) for i in range(36)]  # 0.30~1.00
    scored = sorted(((w, rps_of(fit_p, w)) for w in grid), key=lambda t: t[1])
    print("拟合段最优 w（前 5）:")
    for w, s in scored[:5]:
        print(f"  w={w:.2f}  RPS {s:.4f}")
    print()
    print(f"留出段（{len(eval_p)} 场）以 RPS 排序:")
    rows = sorted(((w, rps_of(eval_p, w), ll_of(eval_p, w), acc_of(eval_p, w))
                   for w in grid), key=lambda t: t[1])
    for w, rr, ll, ac in rows[:6]:
        print(f"  w={w:.2f}  RPS {rr:.4f}  LL {ll:.4f}  ACC {ac:.2%}")
    print("  ...")
    print(f"  w=0.00(纯模型) RPS {rps_of(eval_p, 0.0):.4f}  "
          f"LL {ll_of(eval_p, 0.0):.4f}  ACC {acc_of(eval_p, 0.0):.2%}")
    print()

    # ---- 2. 滚动权重（真实生产方式） ----
    pm, pb, acts, chosen = rolling_weight(with_odds, grid)
    rw = statistics.fmean(chosen)
    print(f"滚动重拟：w 均值 {rw:.2f}（min {min(chosen):.2f} / "
          f"max {max(chosen):.2f}），评估 {len(acts)} 场")
    rps_m = statistics.fmean(rps(p, a) for p, a in zip(pm, acts))
    rps_b = statistics.fmean(rps(p, a) for p, a in zip(pb, acts))
    acc_m = statistics.fmean(accuracy(favorite_index(p), a)
                             for p, a in zip(pm, acts))
    acc_b = statistics.fmean(accuracy(favorite_index(p), a)
                             for p, a in zip(pb, acts))
    print(f"  纯模型    RPS {rps_m:.4f}  ACC {acc_m:.2%}")
    print(f"  滚动融合  RPS {rps_b:.4f}  ACC {acc_b:.2%}")
    print(f"  改善      RPS {rps_b - rps_m:+.4f}  ACC {acc_b - acc_m:+.2%}")
    print()

    # ---- 3. 显著性 ----
    pa = [rps(p, a) for p, a in zip(pb, acts)]
    pbm = [rps(p, a) for p, a in zip(pm, acts)]
    d, lo, hi = boot_ci(pa, pbm)
    print(f"RPS 差异（融合−模型）: {d:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]")
    print(f"  → {'显著改善' if hi < 0 else ('显著变差' if lo > 0 else '不显著')}")
    print()

    # ---- 4. DC / Elo 复核 ----
    for name, dc_, elo_ in (("纯泊松", False, False),
                            ("Dixon-Coles", True, False),
                            ("Poisson+Elo", False, True)):
        s2 = collect(use_dc=dc_, use_elo=elo_)
        if not s2:
            continue
        rr = statistics.fmean(rps(p, a) for p, _, a in s2)
        ll = statistics.fmean(log_loss(p, a) for p, _, a in s2)
        ac = statistics.fmean(accuracy(favorite_index(p), a) for p, _, a in s2)
        print(f"  {name:14s} n={len(s2)}  RPS {rr:.4f}  LL {ll:.4f}  ACC {ac:.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
