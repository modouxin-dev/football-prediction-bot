#!/usr/bin/env python3
"""实验（不接生产）：射正数据能否提升泊松强度估计——**结论：不能，已存档**。

假设
----
进球是低频事件（场均 ~1.5），样本噪声大；射正是高频事件（场均 ~4.5），
同样的历史窗口下「创造机会的能力」应估计得更稳。若成立，用射正算相对
强度、只借用进球的量级做 λ，应优于纯进球基线。

方法（严格走前）
----------------
* 语料：镜像内置英超 2023/2024/2025 三季真实赛果（含比分、射正、赔率）
* **第一个整季（380 场）只作热身**，评估只用 2024/2025 共 760 场
* 每场只用**该场之前**的比赛累积统计，绝不看未来
* 相对强度在各自量纲内归一化，λ 绝对量级仍由进球基准决定
* 评估：RPS（已按 1/(r-1) 归一）、命中率、LogLoss
* 显著性：2000 次 bootstrap 的 95% 置信区间

结论（2026-10-14 实测）
----------------------
    goal    RPS 0.2343   命中 41.45%
    sot     RPS 0.2342   命中 40.13%   ΔRPS -0.0001  不显著
    mix     RPS 0.2331   命中 41.32%   ΔRPS -0.0011  不显著
    market  RPS 0.2014   命中 51.58%   ΔRPS -0.0330  显著

热身期从 40 场放宽到整季后，射正的收益**从"显著"退化为"不显著"**——
说明那一点收益只是在补偿基线不成熟（强度还没攒够），而不是射正真的
携带了额外信息。这一条很重要：它示范了**热身期太短会制造假的改进**。

因此本实验**不接入生产**，仅存档，避免日后重复尝试。

复现：python3 exp_shot_quality.py
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from football_data_uk import load_local

# 热身期：赛季前 WARMUP 轮不评估（强度还没攒出来）
WARMUP = 380  # 第一个赛季整季作热身，评估只用 2024/2025
# 指数衰减半衰期（场）：近期比赛权重更高
HALFLIFE = 20.0
# 收缩先验：样本少时向联赛均值收缩
PRIOR_K = 6.0


def load_all():
    rows = []
    for season in (2023, 2024, 2025):
        rows.extend(load_local(season, "E0"))
    out = []
    for r in rows:
        ft = (r.get("score") or {}).get("fullTime") or {}
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        sot = r.get("shotsOnTarget") or {}
        out.append({
            "date": r["utcDate"][:10],
            "home": (r["homeTeam"] or {}).get("name"),
            "away": (r["awayTeam"] or {}).get("name"),
            "hg": int(hg), "ag": int(ag),
            "hsot": sot.get("home"), "asot": sot.get("away"),
            "mkt": r.get("marketProbabilities"),
        })
    out.sort(key=lambda x: x["date"])
    return out


def poisson_1x2(lh: float, la: float, rho: float = 0.0, maxg: int = 12):
    """由两个 λ 算胜平负概率（含 Dixon-Coles 低比分修正，rho=0 时即纯泊松）。"""
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(maxg + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(maxg + 1)]
    home = draw = away = 0.0
    for i in range(maxg + 1):
        for j in range(maxg + 1):
            p = ph[i] * pa[j]
            if i == 0 and j == 0:
                p *= 1.0 - rho
            elif i == 0 and j == 1:
                p *= 1.0 + rho
            elif i == 1 and j == 0:
                p *= 1.0 + rho
            elif i == 1 and j == 1:
                p *= 1.0 - rho
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
    tot = home + draw + away
    return (home / tot, draw / tot, away / tot)


class Accum:
    """按主客场分别累积「产出」与「被产出」，带收缩先验。

    h_for      = 该队主场的产出（进球或射正）
    h_against  = 该队主场被对手产出
    a_for      = 该队客场的产出
    a_against  = 该队客场被对手产出
    """

    KEYS = ("h_for", "h_against", "a_for", "a_against")

    def __init__(self):
        self.s = defaultdict(float)   # (key, team) -> 累计值
        self.n = defaultdict(int)     # (key, team) -> 场次
        self.lg_s = defaultdict(float)
        self.lg_n = defaultdict(int)

    def update(self, m, stat):
        hs = m["hg"] if stat == "goal" else m["hsot"]
        as_ = m["ag"] if stat == "goal" else m["asot"]
        if hs is None or as_ is None:
            return
        pairs = (
            ("h_for", m["home"], hs), ("h_against", m["away"], as_),
            ("a_for", m["away"], as_), ("a_against", m["home"], hs),
        )
        for key, team, val in pairs:
            self.s[(key, team)] += val
            self.n[(key, team)] += 1
            self.lg_s[key] += val
            self.lg_n[key] += 1

    def rate(self, key, team):
        """收缩后的场均值：样本少时向联赛均值靠。"""
        lg = self.lg_s[key] / self.lg_n[key] if self.lg_n[key] else 1.0
        cnt = self.n[(key, team)]
        raw = self.s[(key, team)]
        return (raw + PRIOR_K * lg) / (cnt + PRIOR_K)


def build(matches_upto, stat):
    acc = Accum()
    for m in matches_upto:
        acc.update(m, stat)
    return acc


def strength(acc, team, key):
    return acc.rate(key, team)


def rps(probs, actual):
    c = 0.0
    s = 0.0
    tot = 0.0
    for i in range(3):
        c += probs[i]
        s += 1.0 if i == actual else 0.0
        tot += (c - s) ** 2
    return tot / 2.0


def logloss(probs, actual):
    p = max(probs[actual], 1e-9)
    return -math.log(p)


def hit(probs, actual):
    return 1 if max(range(3), key=lambda i: probs[i]) == actual else 0


def bootstrap_diff(a, b, n=2000, seed=7):
    """a/b 为逐场 (rps) 列表，返回 b-a 的均值差与 95% CI。"""
    rnd = random.Random(seed)
    N = len(a)
    diffs = []
    for _ in range(n):
        idx = [rnd.randrange(N) for _ in range(N)]
        da = sum(a[i] for i in idx) / N
        db = sum(b[i] for i in idx) / N
        diffs.append(db - da)
    diffs.sort()
    mean = sum(diffs) / len(diffs)
    return mean, diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]


def run():
    matches = load_all()
    print(f"总场次 {len(matches)}")
    res = {"goal": [], "sot": [], "mix": [], "market": []}
    hits = {"goal": [], "sot": [], "mix": [], "market": []}
    lls = {"goal": [], "sot": [], "mix": [], "market": []}

    for i, m in enumerate(matches):
        if i < WARMUP:
            acc_g = build(matches[:i], "goal")
            acc_s = build(matches[:i], "sot")
            if acc_s.lg_n["h_for"] == 0:
                continue
            continue
        past = matches[:i]
        acc_g = build(past, "goal")
        acc_s = build(past, "sot")
        if acc_s.lg_n["h_for"] == 0:
            continue

        # 联赛均值（收缩基准）
        lh_for = acc_g.lg_s["h_for"] / max(acc_g.lg_n["h_for"], 1)
        la_for = acc_g.lg_s["a_for"] / max(acc_g.lg_n["a_for"], 1)

        def lam(acc, home, away, base_h, base_a):
            """λ = 进球量级基准 × 相对攻击强度 × 相对防守漏洞。

            相对强度在**该 acc 自己的量纲内**归一化（射正的场均 4.5 与
            进球的场均 1.5 各自除自己的联赛均值），所以射正只贡献更稳的
            **比率**，λ 的绝对量级仍由进球基准决定——否则会预测出 4:4。
            """
            lh_s = acc.lg_s["h_for"] / max(acc.lg_n["h_for"], 1)
            la_s = acc.lg_s["a_for"] / max(acc.lg_n["a_for"], 1)
            lh_ag = acc.lg_s["h_against"] / max(acc.lg_n["h_against"], 1)
            la_ag = acc.lg_s["a_against"] / max(acc.lg_n["a_against"], 1)
            ah = acc.rate("h_for", home) / max(lh_s, 1e-6)
            da = acc.rate("a_against", away) / max(la_ag, 1e-6)
            aa = acc.rate("a_for", away) / max(la_s, 1e-6)
            dh = acc.rate("h_against", home) / max(lh_ag, 1e-6)
            return max(base_h * ah * da, 0.05), max(base_a * aa * dh, 0.05)

        lg_h, lg_a = lam(acc_g, m["home"], m["away"], lh_for, la_for)
        ls_h, ls_a = lam(acc_s, m["home"], m["away"], lh_for, la_for)

        pg = poisson_1x2(lg_h, lg_a)
        ps = poisson_1x2(ls_h, ls_a)
        # 融合：几何平均（对数量级更稳）
        mix = [math.sqrt(pg[k] * ps[k]) for k in range(3)]
        t = sum(mix)
        mix = [x / t for x in mix]

        mk = m.get("mkt")
        if mk:
            pm = (mk["主胜"], mk["平局"], mk["客胜"])
        else:
            pm = None

        actual = 0 if m["hg"] > m["ag"] else (1 if m["hg"] == m["ag"] else 2)
        for name, p in (("goal", pg), ("sot", ps), ("mix", mix)):
            res[name].append(rps(p, actual))
            hits[name].append(hit(p, actual))
            lls[name].append(logloss(p, actual))
        if pm:
            res["market"].append(rps(pm, actual))
            hits["market"].append(hit(pm, actual))
            lls["market"].append(logloss(pm, actual))

    print(f"评估场次 {len(res['goal'])}")
    print(f"{'口径':<10}{'RPS':>10}{'命中率':>10}{'LogLoss':>10}")
    for name in ("goal", "sot", "mix", "market"):
        if not res[name]:
            continue
        r = sum(res[name]) / len(res[name])
        h = sum(hits[name]) / len(hits[name])
        l = sum(lls[name]) / len(lls[name])
        print(f"{name:<10}{r:>10.4f}{h:>10.2%}{l:>10.4f}")

    print("\n显著性（vs 进球基线，负=更好）")
    for name in ("sot", "mix", "market"):
        if not res[name]:
            continue
        d, lo, hi = bootstrap_diff(res["goal"], res[name])
        print(f"{name:<10} ΔRPS {d:+.4f}  95%CI [{lo:+.4f}, {hi:+.4f}]  "
              f"{'显著' if lo * hi > 0 else '不显著'}")


if __name__ == "__main__":
    run()
