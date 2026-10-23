#!/usr/bin/env python3
"""回测体检：一次跑出「模型到底准不准」的可信数字。

为什么需要它
------------
看板上如果出现 4.86% 这种方向命中率，它比随机基线（33.3%）还低，
在逻辑上是不可能的——要么口径被读错，要么语料里有脏数据。
本脚本不猜，直接把证据打出来：

  1. 语料构成：按联赛分组各多少场（混合联赛会稀释模型表现）
  2. 两个口径：方向命中率 / 精确比分命中率（差约一个量级，别混看）
  3. 混淆矩阵：模型预测了什么 vs 实际发生了什么
  4. 平局判定率：若预测几乎不出平局，或实际平局比例异常，会直接压低命中率
  5. 分联赛命中率：定位是不是某个联赛的数据在拖后腿

用法（在生产容器里跑，才能读到真实库）：
    python scripts/diagnose_backtest.py
    python scripts/diagnose_backtest.py --competition PL --limit 400
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

import paths  # noqa: F401  （确保 DB_PATH 解析到位）
from backtest import outcome_index, favorite_index
from backtest_corpus import load_corpus
from repository import PredictionRepository


def _bar(pct: float, width: int = 28) -> str:
    filled = int(round(pct * width))
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--competition", default="", help="留空 = 全部联赛")
    ap.add_argument("--div", default="E0")
    ap.add_argument("--min-history", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0, help="只跑最近 N 场（0=全部）")
    args = ap.parse_args()

    repo = PredictionRepository()
    matches, info = load_corpus(repo, args.competition, div=args.div)
    if args.limit:
        matches = matches[-args.limit:]

    print("\n" + "=" * 66)
    print("语料构成")
    print("=" * 66)
    print(f"  总场次     : {info['total']}")
    print(f"  库内/CSV   : {info['db']} / {info['history']}")
    print(f"  时间窗口   : {info.get('window_start')} → {info.get('window_end')}")
    by_comp = Counter(str(m.get('competition') or '?') for m in matches)
    for comp, n in by_comp.most_common():
        print(f"    {comp:<6} {n:>6} 场  {_bar(n / max(1, len(matches)))}")

    # ---- 滚动回测，同时收集混淆矩阵 -------------------------------------
    from backtester_fixed import BacktesterFixed

    bt = BacktesterFixed(league_id=39, min_history=args.min_history)
    bt.matches = matches
    bt.corpus_info = info
    out = asyncio.run(bt.backtest())

    print("\n" + "=" * 66)
    print("两个口径（不要混看）")
    print("=" * 66)
    acc = out.get("accuracy") or 0.0
    ex = out.get("exact_rate")
    print(f"  方向命中率 : {acc*100:.2f}%   （随机基线 33.3%，45~55% 算良好）")
    print(f"  比分命中率 : {('%.2f%%' % (ex*100)) if ex is not None else '—'}"
          f"   （猜中具体比分如 2-1，业界 5~8%）")
    print(f"  Log Loss   : {out.get('log_loss')}")
    print(f"  RPS        : {out.get('rps')}")
    print(f"  评估场次   : {out.get('total_predictions')}")

    if acc < 0.30:
        print("\n  ⚠️ 方向命中率低于随机基线 —— 这不是「模型不准」，")
        print("     而是数据或口径出了问题。请重点看下面的混淆矩阵与实际结果分布。")

    # ---- 混淆矩阵 -------------------------------------------------------
    names = ("主胜", "平", "客胜")
    preds = out.get("predictions") or []
    if preds:
        mat = Counter()
        actual_dist = Counter()
        pred_dist = Counter()
        for r in preds:
            p = r.get("predicted")
            a = r.get("actual")
            mat[(p, a)] += 1
            pred_dist[p] += 1
            actual_dist[a] += 1
        n = len(preds)
        print("\n" + "=" * 66)
        print(f"混淆矩阵（{n} 场）  行=模型预测  列=实际结果")
        print("=" * 66)
        print("            " + "".join(f"{nm:>8}" for nm in names))
        for p in names:
            row = f"  预测{p:<4}"
            row += "".join(f"{mat[(p, a)]:>8}" for a in names)
            print(row)
        print("\n  实际结果分布：")
        for a in names:
            c = actual_dist[a]
            print(f"    {a:<4} {c:>6} 场 ({c/n*100:5.1f}%)  {_bar(c/n)}")
        print("  模型预测分布：")
        for p in names:
            c = pred_dist[p]
            print(f"    {p:<4} {c:>6} 场 ({c/n*100:5.1f}%)  {_bar(c/n)}")

        # 真实足球平局率约 25%；偏离太多说明语料有问题
        draw_rate = actual_dist["平"] / n
        if draw_rate > 0.45:
            print(f"\n  ⚠️ 实际平局率 {draw_rate*100:.1f}%，远高于真实足球的 ~25%。")
            print("     典型的成因：大量比赛比分被记为 0-0（未录入真实赛果）。")
        if pred_dist["平"] == 0:
            print("\n  ⚠️ 模型从不预测平局，平局场全部判错（约 25% 场次直接归零）。")
    else:
        print("\n  （本次回测未返回逐场明细，跳过混淆矩阵）")

    print("\n" + "=" * 66)
    print("结论")
    print("=" * 66)
    if acc >= 0.45:
        print("  方向命中率处于业界良好区间，模型是有效的。")
    elif acc >= 0.33:
        print("  方向命中率仅略高于随机，模型边缘有效，需继续调。")
    else:
        print("  方向命中率低于随机基线 → 先查数据，不要急着调模型。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
