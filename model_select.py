"""模型选择实验：把「该不该启用 Elo」「市场权重给多少」变成有置信区间的证据。

为什么需要这个文件
------------------
``ELO_BLEND=0.6``、市场权重 ``0.75`` 都是**产品参数**，不是理论解：

* Dixon & Coles (1997) 只给了 ``exp(−ξt)`` 的加权形式，最优半衰期随联赛与
  时代变化；Ley et al. (2019) 在英超十年样本上得到 390 天，是**该联赛的
  结果**，不能作为西甲 / 葡超 / 巴甲 / 克甲 / 丹超的默认值。
* Hvattum & Arntzen (2010, n=30 524) 表明 Elo 优于若干统计基准，但**弱于
  赔率**——故 Elo 与泊松是**互补**而非替代关系。该不该融合、融合多强，
  必须本地定，文献给不出数值。
* 市场权重同样无统一解：Štrumbelj & Robnik Šikonja (2010) 明确指出庄家质量
  随**联赛与时间**变化，不存在可移植常数。

方法学约束（不得放宽）
----------------------
1. **walk-forward（rolling origin）**，不用随机 K 折——后者会把未来比赛
   泄漏给过去的模型，而足球数据存在跨队、同赛季相关。
2. **配对 bootstrap** 给 95% 区间，不只看单点差值。以**整场比赛**为单位
   重采样，保持同场配对。
3. **主指标 Brier，次指标 RPS，诊断 Log Loss / ECE**。
   Constantinou & Fenton (2012)：单一得分规则不足以评估足球概率预测。
4. 判定阈值**预先注册**，不允许看到结果后放宽。

判定规则（三条同时满足才算胜出）
--------------------------------
① 主指标 Brier 改善（更低）
② 次指标 RPS 不退化
③ 配对 bootstrap 95% CI 不跨 0

否则一律返回 ``inconclusive``——宁可维持现状，也不把噪声写进生产参数。

样本量声明
----------
模拟研究显示 Log Loss 在 200 场时识别较优模型的正确率约 93%，100 场约
84~87%。因此本框架设 ``min_matches`` 下限（默认 150），低于该值直接返回
``insufficient``，不给结论。

⚠️ 合成数据只能验证本框架的**判定逻辑**，不能用来确定生产参数。
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

# 样本量下限：低于此值不给结论（见模块 docstring 的样本量声明）
MIN_MATCHES = 150
# bootstrap 重采样次数，文献建议 5000
BOOTSTRAP_N = 5000


@dataclass(frozen=True)
class Candidate:
    """一路候选配置。

    Attributes:
        key: 唯一标识，用于报告与比较
        use_elo: 是否启用 Elo 修正
        elo_blend: Elo 差 → λ 系数的强度（0 表示不介入）
        market_weight: 市场权重 w，None 表示纯市场、0 表示纯模型
        use_dc: 是否启用 Dixon-Coles 修正
        use_calib: 是否滚动拟合温度校准
    """

    key: str
    use_elo: bool = False
    elo_blend: float = 0.0
    market_weight: float | None = 0.0
    use_dc: bool = True
    use_calib: bool = False


# ---- 逐场指标 ---------------------------------------------------------------


def _probs_of(record: dict[str, Any]) -> tuple[float, float, float] | None:
    """取三项概率 (主, 平, 客)。

    ``records`` 里既有旧字段 ``prob``（仅预测方向的单项概率）也有新字段
    ``probs``（三项）。只有后者能算 Brier / RPS——缺它时返回 None，
    由调用方显式处理，绝不拿单项概率凑。
    """
    raw = record.get("probs")
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]))
        except (TypeError, ValueError):
            return None
    return None


def _actual_index(record: dict[str, Any]) -> int | None:
    """实际结果下标 0=主胜 1=平 2=客胜。"""
    name = record.get("actual")
    mapping = {"home": 0, "draw": 1, "away": 2}
    if isinstance(name, str):
        idx = mapping.get(name.strip().lower())
        if idx is not None:
            return idx
    raw = record.get("actual_index")
    return int(raw) if isinstance(raw, int) and 0 <= raw <= 2 else None


def brier_of(probs: Sequence[float], actual: int) -> float:
    """三分类 Brier = Σ(q_c − o_c)²，越低越好。"""
    return sum((probs[i] - (1.0 if i == actual else 0.0)) ** 2 for i in range(3))


def rps_of(probs: Sequence[float], actual: int) -> float:
    """Ranked Probability Score：利用主/平/客的序数关系。

    把「强队误判为客胜」的惩罚高于「误判为平局」——这是足球预测特有的
    要求，Brier 与 Log Loss 都不具备该性质。
    """
    cum_p = cum_o = 0.0
    total = 0.0
    for i in range(3):
        cum_p += float(probs[i])
        cum_o += 1.0 if i == actual else 0.0
        total += (cum_p - cum_o) ** 2
    return total / 2.0  # 归一化到与 3 分类可比


def log_loss_of(probs: Sequence[float], actual: int) -> float:
    """Log Loss，对过度自信惩罚最重。"""
    p = float(probs[actual])
    if p <= 0.0:
        return 5.0  # 截断，避免 inf 污染均值
    import math

    return -math.log(p)


def per_match_scores(records: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    """逐场三项得分；无法评估的场次（缺三项概率）**直接丢弃**并记录。

    丢弃而不是填 0——填 0 会人为压低均值，等于伪造改善。
    """
    out: list[dict[str, float]] = []
    dropped = 0
    for r in records:
        probs = _probs_of(r)
        actual = _actual_index(r)
        if probs is None or actual is None:
            dropped += 1
            continue
        out.append({
            "brier": brier_of(probs, actual),
            "rps": rps_of(probs, actual),
            "log_loss": log_loss_of(probs, actual),
        })
    if dropped:
        log.warning("逐场得分：%d 场缺三项概率，未计入", dropped)
    return out


# ---- 配对 bootstrap ---------------------------------------------------------


def paired_bootstrap(base: Sequence[dict[str, float]],
                     chal: Sequence[dict[str, float]],
                     *, metric: str = "brier",
                     n: int = BOOTSTRAP_N,
                     seed: int = 42) -> dict[str, Any]:
    """配对 bootstrap：以整场比赛为单位重采样，保持同场配对。

    为什么必须配对：两路模型在同一批比赛上评估，场次间不独立
    （同队、同赛季、同期状态相关）。独立二项近似会过度乐观。

    Args:
        base: 基线逐场得分
        chal: 候选逐场得分，必须与 base **长度相同且同序**
        metric: 评估指标名
        n: 重采样次数
        seed: 随机种子（固定，保证可复现）

    Returns:
        {n, mean_diff, ci_low, ci_high, better}。
        mean_diff < 0 表示候选优于基线（该指标越低越好）。
        长度不一致时返回 {'error': ...}，不猜测对齐方式。
    """
    if len(base) != len(chal):
        return {"error": f"样本数不一致：base={len(base)} chal={len(chal)}"}
    if not base:
        return {"error": "无样本"}
    diffs = [float(c[metric]) - float(b[metric])
             for b, c in zip(base, chal)]
    rng = random.Random(seed)
    k = len(diffs)
    means = []
    for _ in range(int(n)):
        s = 0.0
        for _ in range(k):
            s += diffs[rng.randrange(k)]
        means.append(s / k)
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(int(0.975 * len(means)), len(means) - 1)]
    mean_diff = sum(diffs) / k
    return {
        "metric": metric,
        "n": k,
        "mean_diff": round(mean_diff, 6),
        "ci_low": round(lo, 6),
        "ci_high": round(hi, 6),
        # CI 不跨 0 = 方向一致（候选更好）
        "significant": bool(hi < 0.0 or lo > 0.0),
    }


# ---- 实验编排 ---------------------------------------------------------------


def _mean(scores: Sequence[dict[str, float]], metric: str) -> float | None:
    vals = [s[metric] for s in scores]
    return sum(vals) / len(vals) if vals else None


@dataclass
class ExperimentResult:
    """一次模型选择实验的完整结论。"""

    verdict: str = "inconclusive"      # keep_baseline / adopt / inconclusive / insufficient
    baseline_key: str = ""
    winner_key: str = ""
    n_matches: int = 0
    table: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "baseline_key": self.baseline_key,
            "winner_key": self.winner_key,
            "n_matches": self.n_matches,
            "reason": self.reason,
            "table": self.table,
        }


def run_experiment(matches: list[dict[str, Any]],
                   candidates: Sequence[Candidate],
                   *, baseline_key: str,
                   min_history: int = 200,
                   min_matches: int = MIN_MATCHES,
                   n_boot: int = BOOTSTRAP_N) -> ExperimentResult:
    """跑网格并给出带置信区间的结论。

    判定规则预先注册（见模块 docstring），三条同时满足才 adopt：
    主指标 Brier 改善 + 次指标 RPS 不退化 + Brier 的 95% CI 不跨 0。

    Args:
        matches: 历史比赛，须按时间**正序**；每项含 utc_date /
            home_team_id / away_team_id / home_score / away_score
        candidates: 候选配置，必须包含 baseline_key 对应项
        baseline_key: 当前线上配置，作为比较基准
        min_history: 预热场次数（不参与评估）
        min_matches: 低于该样本量直接返回 insufficient

    Returns:
        ExperimentResult。样本不足或基线缺失时给出明确 reason，不猜。
    """
    from backtest import WalkForwardBacktester

    if len(matches) < int(min_matches):
        return ExperimentResult(
            verdict="insufficient", n_matches=len(matches),
            reason=(f"样本 {len(matches)} 场 < 下限 {min_matches} 场。"
                    f"该量级只能判架构级差异，不足以定参数。"))

    keys = {c.key for c in candidates}
    if baseline_key not in keys:
        return ExperimentResult(
            verdict="inconclusive", n_matches=len(matches),
            reason=f"候选集合中缺少基线 {baseline_key}")

    scores_by_key: dict[str, list[dict[str, float]]] = {}
    table: list[dict[str, Any]] = []
    for cand in candidates:
        tester = WalkForwardBacktester(
            min_history=min_history, use_elo=cand.use_elo,
            elo_blend=cand.elo_blend, use_dc=cand.use_dc,
            use_calib=cand.use_calib, market_weight=cand.market_weight)
        report = tester.run(matches)
        scores = per_match_scores(report.records)
        scores_by_key[cand.key] = scores
        table.append({
            "key": cand.key,
            "n": len(scores),
            "brier": _mean(scores, "brier"),
            "rps": _mean(scores, "rps"),
            "log_loss": _mean(scores, "log_loss"),
            "accuracy": (sum(1 for r in report.records if r.get("hit")) / report.n
                         if report.n else None),
        })

    base_scores = scores_by_key[baseline_key]
    if not base_scores:
        return ExperimentResult(
            verdict="inconclusive", n_matches=len(matches),
            reason="基线无有效逐场得分（records 缺三项概率）")

    best_key, best_row = baseline_key, None
    for cand in candidates:
        if cand.key == baseline_key:
            continue
        chal = scores_by_key[cand.key]
        if len(chal) != len(base_scores):
            continue
        boot = paired_bootstrap(base_scores, chal, metric="brier", n=n_boot)
        row = next((t for t in table if t["key"] == cand.key), None)
        if row is None:
            continue
        row["vs_baseline"] = boot
        b_brier = _mean(base_scores, "brier")
        b_rps = _mean(base_scores, "rps")
        c_brier, c_rps = row["brier"], row["rps"]
        if None in (b_brier, b_rps, c_brier, c_rps):
            continue
        # 三条判定规则
        improves = c_brier < b_brier
        rps_ok = c_rps <= b_rps + 1e-9
        significant = bool(boot.get("significant")) and boot.get("mean_diff", 0) < 0
        row["rules"] = {
            "brier_improves": improves,
            "rps_not_worse": rps_ok,
            "ci_significant": significant,
        }
        if improves and rps_ok and significant:
            if best_row is None or c_brier < best_row["brier"]:
                best_key, best_row = cand.key, row

    if best_row is not None:
        return ExperimentResult(
            verdict="adopt", baseline_key=baseline_key, winner_key=best_key,
            n_matches=len(base_scores), table=table,
            reason=("Brier 改善、RPS 未退化且配对 bootstrap 95% CI 不跨 0，"
                    "三条判定规则同时满足。"))
    return ExperimentResult(
        verdict="keep_baseline", baseline_key=baseline_key,
        winner_key=baseline_key, n_matches=len(base_scores), table=table,
        reason="无候选同时满足三条判定规则，维持当前线上配置。")
