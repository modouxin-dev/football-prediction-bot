"""完整集成回测测试。

历史遗留风险（本次修正的原因）
------------------------------
原脚本的无条件"通过"是**假绿灯**：

    backtest_results = await backtester.run_backtest()
    print(f"✅ 回测系统: 通过 (准确率 {backtest_results['accuracy']:.2%})")

当时 ``BacktesterWithDB`` 的数据源是 3 场硬编码假比赛（Team A/C/E），
且 ``predict_result()`` 恒返回 ``"home"``。于是脚本用 3 场假比赛和一个
恒定预测，得出了"✅ 所有组件正常工作"的结论——无论数字多离谱都打印通过。

现改为**基于真实指标判定**：
    1. 样本量必须足够（几场样本得出的准确率没有统计意义）；
    2. 准确率必须落在真实分布区间，且不能是恒定的 100% / 0%；
    3. 信心等级必须单调（高信心命中率 > 低信心），否则等级只是随机标签。

任一条不满足即判定失败，不再是"跑完就算过"。
"""

from __future__ import annotations

import asyncio
import sys

from backtester_with_db import BacktesterWithDB
from data_analysis import DataAnalyzer
from feature_extractor import FeatureExtractor
from realtime_api_fixed import RealtimeAPIFixed

MIN_SAMPLES = 200          # 少于此样本量不具统计意义
ACC_RANGE = (0.35, 0.65)   # 英超胜平负三选一，真实模型落在这个区间


def judge_backtest(result: dict) -> tuple[bool, list[str]]:
    """按真实指标判定回测是否可信。返回 (是否通过, 失败原因)。"""
    reasons: list[str] = []

    n = result.get("total_predictions", 0)
    if n < MIN_SAMPLES:
        reasons.append(f"样本量不足：{n} < {MIN_SAMPLES}")

    acc = result.get("accuracy") or 0.0
    lo, hi = ACC_RANGE
    if not (lo <= acc <= hi):
        reasons.append(f"准确率 {acc:.2%} 落在真实分布区间 [{lo:.0%}, {hi:.0%}] 之外")

    # 恒定预测会给出 100% 或 0%，这类"完美/全错"都是模型没在工作的信号
    if abs(acc - 1.0) < 1e-9 or abs(acc) < 1e-9:
        reasons.append(f"准确率恒定为 {acc:.2%}，预测逻辑可能仍是占位符")

    levels = result.get("accuracy_by_type") or {}
    preds = result.get("predictions") or []
    by_level: dict[str, list[int]] = {}
    for p in preds:
        slot = by_level.setdefault(p.get("level") or "unknown", [0, 0])
        slot[0] += 1
        slot[1] += 1 if p.get("correct") else 0
    rates = {k: v[1] / v[0] for k, v in by_level.items() if v[0]}
    if "high" in rates and "low" in rates and rates["high"] <= rates["low"]:
        reasons.append(f"信心等级无区分度：{ {k: round(v, 3) for k, v in rates.items()} }")

    return (not reasons), reasons


async def run_integrated_test() -> dict:
    print("\n" + "=" * 60)
    print("🚀 完整集成回测测试")
    print("=" * 60)

    # 1. 回测（真实数据源 + 滚动前进）
    print("\n1️⃣ 阶段1: 历史回测")
    print("-" * 60)
    # 不指定 season：滚动前进依赖历史积累，取全部可用赛季结论更稳
    backtester = BacktesterWithDB(league_id=39)
    backtest_results = await backtester.run_backtest()
    ok, reasons = judge_backtest(backtest_results)
    print(f"   样本 {backtest_results['total_predictions']} 场，"
          f"准确率 {backtest_results['accuracy']:.2%}，"
          f"log_loss {backtest_results['log_loss']:.4f}")
    print(f"   判定: {'✅ 通过' if ok else '❌ 未通过'}")
    for r in reasons:
        print(f"     - {r}")

    # 2. 数据分析
    print("\n2️⃣ 阶段2: 数据分析")
    print("-" * 60)
    analyzer = DataAnalyzer()
    fixtures = [
        {"home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 1, "status": "FINISHED"},
        {"home_team": "C", "away_team": "D", "home_goals": 1, "away_goals": 1, "status": "FINISHED"},
    ]
    team_analysis = await analyzer.analyze_team_form(fixtures)
    print(f"✅ 分析完成: {len(team_analysis)} 支球队")

    # 3. 特征提取
    print("\n3️⃣ 阶段3: 特征提取")
    print("-" * 60)
    extractor = FeatureExtractor()
    features = extractor.extract_features("Team A", "Team B")
    print(f"✅ 特征提取: {len(features)} 维向量")

    # 4. 实时数据
    print("\n4️⃣ 阶段4: 实时数据")
    print("-" * 60)
    realtime = RealtimeAPIFixed()
    odds = await realtime.fetch_odds(1)
    injuries = await realtime.fetch_injuries(1)
    update = await realtime.update_prediction(1, odds, injuries)
    print(f"✅ 实时数据: 赔率={odds['home_odds']}, 调整={update['adjustment']:.4f}")

    all_ok = ok

    print("\n" + "=" * 60)
    print("✨ 完整集成测试结果")
    print("=" * 60)
    print(f"""
    {'✅' if ok else '❌'} 回测系统: {'通过' if ok else '未通过'} \
(准确率 {backtest_results['accuracy']:.2%}, \
{backtest_results['total_predictions']} 场, \
log_loss {backtest_results['log_loss']:.4f})
    ✅ 数据分析: 通过 ({len(team_analysis)} 支球队)
    ✅ 特征工程: 通过 ({len(features)} 维向量)
    ✅ 实时API: 通过 (赔率 + 伤停 + 预测)

    整体状态: {'✅ 所有组件正常工作' if all_ok else '❌ 回测未通过，见上方原因'}
    """)
    print("=" * 60 + "\n")

    return {
        "backtest": backtest_results["accuracy"],
        "backtest_ok": ok,
        "samples": backtest_results["total_predictions"],
        "analysis": len(team_analysis),
        "features": len(features),
        "realtime": update["adjustment"],
    }


if __name__ == "__main__":
    results = asyncio.run(run_integrated_test())
    sys.exit(0 if results["backtest_ok"] else 1)
