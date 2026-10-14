"""赛前信号对照的排查工具 / Debug CLI for the signal A/B ledger.

用法 / Usage
------------
    python signal_ab_cli.py <match_id>          打印这场比赛的全部对照记录
    python signal_ab_cli.py --stats             按信号类型汇总"有没有用"
    python signal_ab_cli.py --export out.csv    导出全部对照数据
    python signal_ab_cli.py --simulate injury 0.8   模拟把某类信号强度改成 0.8

为什么需要它 / Why
------------------
排查"为什么这场预测长这样"时，去数据库翻 JSON 列是十倍的工作量：要自己
拼 SQL、自己解析 JSON、自己算差值。这个脚本把最常用的三个动作做成一条命令。

    Digging through JSON columns by hand costs ten times as much: write SQL,
    parse JSON, compute deltas yourself. One command replaces all of it.

设计上刻意**只读** —— 排查工具不该有能力改数据，改权重走 signal_admin。

    Deliberately read-only. Tuning weights belongs to signal_admin.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from repository import PredictionRepository
from signal_ab import (_load_probs, outcome_index, summarise, verdict)
from signal_admin import export_csv, simulate_text, simulate_weight

# 概率列的三段标签，打印时对齐用。
_LABELS = ("主胜", "平局", "客胜")


def _fmt(probs: list[float] | None) -> str:
    if not probs:
        return "—"
    return "  ".join(f"{lab} {p * 100:5.1f}%" for lab, p in zip(_LABELS, probs))


def _delta(base: list[float] | None, sig: list[float] | None) -> str:
    if not base or not sig:
        return "—"
    return "  ".join(f"{(s - b) * 100:+5.1f}pp" for b, s in zip(base, sig))


def render_fixture(repo: PredictionRepository, fixture_id: str) -> int:
    rows = repo.signal_ab_for_fixture(fixture_id, include_test=False)
    if not rows:
        print(f"没有找到 {fixture_id} 的对照记录。")
        print("可能原因：信号未触发（四类因子全中性）、SIGNAL_AB_ENABLED 关闭、"
              "或这场还没被预测过。")
        return 1
    print(f"=== {fixture_id} 的赛前信号对照 ===")
    for row in rows:
        pure = _load_probs(row["pure_baseline"])
        partial = _load_probs(row["partial_baseline"])
        signal = _load_probs(row["signal_prediction"])
        idx = outcome_index(row["actual_home"], row["actual_away"])
        print()
        print(f"信号类型   {row['signal_type']}  "
              f"强度={row['signal_strength']}  状态={row['status']}  "
              f"变更={row['change_type']}")
        print(f"  pure      {_fmt(pure)}")
        print(f"  partial   {_fmt(partial)}")
        print(f"  signal    {_fmt(signal)}")
        print(f"  增量      {_delta(partial, signal)}   （signal − partial）")
        if row["error"]:
            print(f"  错误      {row['error']}")
        if row["human_note"]:
            print(f"  说明      {row['human_note']}")
        if idx is None:
            print("  赛果      未结算")
        else:
            name = _LABELS[idx]
            hit_b = "✓" if pure and max(range(3), key=lambda i: pure[i]) == idx else "✗"
            hit_s = "✓" if signal and max(range(3), key=lambda i: signal[i]) == idx else "✗"
            print(f"  赛果      {row['actual_home']}-{row['actual_away']}"
                  f"（{name}）  pure {hit_b} / signal {hit_s}")
    return 0


def render_stats(repo: PredictionRepository) -> int:
    rows = repo.signal_ab_rows(settled_only=True)
    if not rows:
        print("没有已结算的对照记录 —— 样本要靠时间攒，不是补出来的。")
        return 1
    out = summarise(rows)
    print(f"=== 对照汇总（共 {out['total']} 行，阈值 {out['threshold']}）===")
    if out.get("revoked"):
        # 撤销行不计入上面的统计（信号已经不在预测里了），但必须让人看见，
        # 否则"明明触发过信号怎么没进统计"会变成无从排查的疑问。
        detail = "、".join(f"{k} {v} 场" for k, v
                           in sorted((out.get("revoked_by_type") or {}).items()))
        print(f"（另有 {out['revoked']} 场信号被撤销，不计入评估：{detail}）")
    for sig_type in sorted(out["by_signal"]):
        entry = out["by_signal"][sig_type]
        strong = entry["strong"]
        print()
        print(f"{sig_type}: {verdict(entry)}")
        print(f"  全部口径  n={entry['all']['n']}  命中 "
              f"{entry['all']['hit_base']} → {entry['all']['hit_signal']}")
        print(f"  强信号场  n={strong['n']}  Log Loss "
              f"{strong['logloss_base']} → {strong['logloss_signal']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="赛前信号对照排查工具")
    parser.add_argument("match_id", nargs="?", help="比赛 ID")
    parser.add_argument("--db", default=None, help="数据库路径（默认按配置）")
    parser.add_argument("--stats", action="store_true", help="汇总各类信号")
    parser.add_argument("--export", metavar="PATH", help="导出 CSV")
    parser.add_argument("--simulate", nargs=2, metavar=("TYPE", "WEIGHT"),
                        help="模拟把某类信号强度改成给定值")
    args: Any = parser.parse_args(argv)

    repo = PredictionRepository(args.db) if args.db else PredictionRepository(None)

    if args.export:
        text = export_csv(repo.signal_ab_view())
        with open(args.export, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已导出 {len(text.splitlines()) - 1} 行到 {args.export}")
        return 0
    if args.simulate:
        sig_type, weight = args.simulate
        try:
            w = float(weight)
        except ValueError:
            print("权重必须是数字")
            return 1
        sim = simulate_weight(repo.signal_ab_rows(settled_only=True),
                              sig_type, new_weight=w)
        print(simulate_text(sim))
        return 0
    if args.stats:
        return render_stats(repo)
    if not args.match_id:
        parser.print_help()
        return 1
    return render_fixture(repo, args.match_id)


if __name__ == "__main__":
    sys.exit(main())
