#!/usr/bin/env python3
"""回测命令行入口。

用法：
    # 用库里的真实历史赛果回测（需要已有数据）
    python backtest_cli.py --db /data/football.db

    # 用模拟数据自检回测管线是否可用（无需真实数据）
    python backtest_cli.py --simulate 1140 --seeds 1,2,3

    # 扫描 Elo 融合强度
    python backtest_cli.py --simulate 1140 --scan-blend

输出：双路对比（纯泊松 vs 泊松+Elo）、信心等级单调性、校准曲线。
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import sys

from backtest import compare, run_backtest, WalkForwardBacktester
from migrate_elo import fetch_finished

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("backtest")

LEVEL_ORDER = {"high": 3, "medium": 2, "low": 1}


# ---- 数据加载 ---------------------------------------------------------------

def load_real(db: str, competition: str = "") -> list[dict]:
    from repository import PredictionRepository

    repo = PredictionRepository(db)
    return fetch_finished(repo, competition)


def simulate(seed: int, n: int = 1140, teams: int = 20, drift: float = 0.0,
             dc_rho: float = 0.0) -> list[dict]:
    """生成与真实联赛分布接近的模拟数据（用于管线自检，不用于下结论）。

    ⚠️ dc_rho 是关键：
        真实足球的比分分布存在低比分相关性（0-0/1-1 偏多、1-0/0-1 偏少）。
        若用**独立泊松**（dc_rho=0）生成数据，再去测 Dixon-Coles 的修正效果，
        等于「数据里根本没有这个现象，却指望模型修出效果」——必然得到 ρ≈0、
        无改善的假结论。这是测试方法错误，不是 Dixon-Coles 无效。

        因此评估 DC 时必须用 dc_rho<0 生成带真实相关性的数据；
        同时保留 dc_rho=0 的对照组，检验 DC 在「无相关性」时不会帮倒忙。
    """
    from analyzer import dc_score_matrix

    random.seed(seed)
    names = [f"T{i}" for i in range(teams)]
    cur = {t: random.gauss(1500, 150) for t in names}

    def sample_from_matrix(matrix: list[list[float]]) -> tuple[int, int]:
        """按二维概率矩阵抽样比分。"""
        r = random.random()
        acc = 0.0
        for x, row in enumerate(matrix):
            for y, p in enumerate(row):
                acc += p
                if r <= acc:
                    return x, y
        return len(matrix) - 1, len(matrix[0]) - 1

    out = []
    for i in range(n):
        h, a = random.sample(names, 2)
        diff = (cur[h] - cur[a]) / 400.0 + 0.15   # 0.15 ≈ 主场优势
        lh = 1.35 * (1.5 ** diff)
        la = 1.15 * (1.5 ** -diff)
        if dc_rho:
            hs, as_ = sample_from_matrix(dc_score_matrix(lh, la, rho=dc_rho))
        else:
            hs, as_ = poisson_sample(lh), poisson_sample(la)
        out.append({
            "fixture_id": 10000 + i,
            "home_team_id": h, "away_team_id": a,
            "home_score": hs, "away_score": as_,
        })
        if drift:
            cur[h] += random.gauss(0, drift)
            cur[a] += random.gauss(0, drift)
    return out


def poisson_sample(lmbda: float) -> int:
    limit, k, p = math.exp(-lmbda), 0, 1.0
    while True:
        p *= random.random()
        if p <= limit:
            return k
        k += 1


# ---- 输出 -------------------------------------------------------------------

def _fmt(v, nd=4):
    return f"{v:.{nd}f}" if isinstance(v, float) else "—"


def print_table(cmp_result: dict, label: str) -> None:
    b, c = cmp_result["baseline"], cmp_result["challenger"]
    print(f"\n{'=' * 62}\n{label}  样本 {b['n']} 场\n{'=' * 62}")
    print(f"{'指标':<10}{'纯泊松':>10}{'+Elo':>10}{'变化':>10}   方向")
    print("-" * 62)
    rows = [
        ("准确率", "accuracy", "↑ 越好"),
        ("对数损失", "log_loss", "↓ 越好"),
        ("RPS", "rps", "↓ 越好"),
        ("Brier", "brier", "↓ 越好"),
        ("校准误差", "ece", "↓ 越好"),
    ]
    for name, key, direction in rows:
        bv, cv = b[key], c[key]
        delta = (cv - bv) if (bv is not None and cv is not None) else None
        print(f"{name:<10}{_fmt(bv):>10}{_fmt(cv):>10}{_fmt(delta):>10}   {direction}")
    print("-" * 62)
    print(f"结论：{cmp_result['verdict']}")
    print(f"      对数损失改善 {_fmt(cmp_result['log_loss_delta'])}"
          f"（正=Elo 更好）· RPS 改善 {_fmt(cmp_result['rps_delta'])}")


def print_levels(info: dict, by_level: dict, label: str) -> None:
    rates = info["rates"]
    if not rates:
        print(f"\n{label}：无足够样本")
        return
    print(f"\n{label} 信心等级命中率：")
    ordered = sorted(rates.items(), key=lambda kv: LEVEL_ORDER.get(kv[0], 0), reverse=True)
    for key, rate in ordered:
        n = by_level.get(key, {}).get("n", 0)
        print(f"  {key:<8} {rate:.3f}  (n={n})")
    mono = info["monotonic"]
    flag = "✅ 单调（分级有效）" if mono else ("❌ 非单调（分级失效）" if mono is False else "— 样本不足")
    print(f"  {flag}")


def print_calibration(buckets: list[dict], label: str) -> None:
    if not buckets:
        return
    print(f"\n{label} 校准曲线（模型说的 vs 实际发生的）：")
    print(f"  {'概率区间':<14}{'场次':>6}{'模型说':>10}{'实际':>10}{'偏差':>10}")
    for b in buckets:
        gap = b["observed"] - b["predicted"]
        print(f"  {b['range']:<14}{b['n']:>6}{b['predicted']:>10.3f}"
              f"{b['observed']:>10.3f}{gap:>+10.3f}")


# ---- 主流程 -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回测：验证模型改动是否真的有效")
    ap.add_argument("--db", default=None, help="用真实库回测，如 /data/football.db")
    ap.add_argument("--competition", default="", help="只回测某联赛")
    ap.add_argument("--simulate", type=int, default=0, metavar="N",
                    help="生成 N 场模拟数据（管线自检用）")
    ap.add_argument("--seeds", default="1,2,3", help="模拟随机种子，逗号分隔")
    ap.add_argument("--drift", type=float, default=0.0,
                    help="实力漂移幅度，模拟球队变强/变弱")
    ap.add_argument("--min-history", type=int, default=200,
                    help="前 N 场仅用于冷启动，不纳入评估")
    ap.add_argument("--scan-blend", action="store_true", help="扫描 Elo 融合强度")
    args = ap.parse_args(argv)

    if args.db:
        matches = load_real(args.db, args.competition)
        print(f"从 {args.db} 读到 {len(matches)} 场带比分的比赛")
        if len(matches) < args.min_history + 20:
            print("⚠️ 样本太少，结论不可靠。建议至少 200 场以上。")
        result = run_backtest(matches, min_history=args.min_history)
        print_table(result["comparison"], "真实数据回测")
        print_levels(result["challenger_levels"],
                     result["comparison"]["challenger"]["by_level"], "Elo 模型")
        print_calibration(result["comparison"]["challenger"]["calibration"], "Elo 模型")
        return 0

    if args.scan_blend:
        return _scan_blend(args)

    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    print(f"模拟数据自检：{args.simulate} 场 × {len(seeds)} 个种子 "
          f"（drift={args.drift}, min_history={args.min_history}）")
    for seed in seeds:
        matches = simulate(seed, args.simulate, drift=args.drift)
        result = run_backtest(matches, min_history=args.min_history)
        print_table(result["comparison"], f"seed={seed}")
        print_levels(result["challenger_levels"],
                     result["comparison"]["challenger"]["by_level"], "Elo 模型")
    print("\n⚠️ 模拟数据只能验证「管线可用」，不能证明模型在真实数据上有效。")
    print("   真实结论请以 python backtest_cli.py --db /data/football.db 为准。")
    return 0


def _scan_blend(args) -> int:
    """扫描融合强度：确认「调参无效」还是「方法本身无效」。"""
    import elo as elo_mod
    import backtest as bt

    original = elo_mod.elo_multiplier
    print("\nElo 融合强度扫描（blend）")
    print(f"{'blend':<8}{'Δ准确率':>10}{'Δ对数损失':>12}{'判定':>10}")
    best = None
    for blend in (0.0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0):
        def patched(hr, ar, *, home_advantage=elo_mod.HOME_ADVANTAGE, _b=blend, **kw):
            return original(hr, ar, home_advantage=home_advantage, blend=_b, **kw)

        elo_mod.elo_multiplier = patched
        bt.elo_multiplier = patched
        deltas = []
        for seed in [int(s) for s in args.seeds.split(",") if s.strip()]:
            ms = simulate(seed, args.simulate, drift=args.drift)
            b = WalkForwardBacktester(min_history=args.min_history, use_elo=False).run(ms)
            c = WalkForwardBacktester(min_history=args.min_history, use_elo=True).run(ms)
            deltas.append(compare(b, c)["log_loss_delta"])
        mean = sum(deltas) / len(deltas)
        if best is None or mean > best[1]:
            best = (blend, mean)
        print(f"{blend:<8.1f}{'':>10}{mean:>+12.4f}{'更优' if mean > 0.001 else '更差':>10}")
    elo_mod.elo_multiplier = original
    bt.elo_multiplier = original
    print(f"\n最佳 blend={best[0]}，对数损失改善 {best[1]:+.4f}")
    if best[1] < 0.001:
        print("→ 即便调到最佳参数仍无改善：Elo 在当前框架下没有增量价值，不建议接入预测主流程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
