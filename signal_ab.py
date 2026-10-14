"""赛前信号对照记录 / Pre-match signal A/B ledger.

为什么需要它 / Why this exists
-------------------------------
赛前信号（伤停 / 首发 / 球员评估 / 天气）的权重目前是**拍脑袋定的保守值**
——回测语料（英超 CSV）根本没有这些列，拟合不出来。要把它换成用真实数据
算出来的权重，唯一靠谱的办法是：每次施加信号时**同时算一份"假如不施加"的
预测**，两版都存下来，赛后比谁更准。

    Signal weights are currently hand-picked conservative values — the backtest
    corpus has no injury/lineup columns, so nothing can be fitted. The only
    credible way to replace guesswork with data is to store, alongside every
    signal-applied prediction, a counterfactual "what if we hadn't applied it",
    then compare the two after the match settles.

设计约束（务必遵守）/ Design constraints
----------------------------------------
1. **一行 = 一场比赛 × 一种信号类型**，不是一行一场比赛。
   同一场可能同时触发伤停与阵容，混在一起就无法拆开评估每种信号的价值。

   One row = one fixture × one signal type — NOT one row per fixture. A single
   match can trigger injuries and lineups at once; merging them makes it
   impossible to attribute value to each signal.

2. **signal_strength 存原始值**，不是归一化后的值。归一化逻辑会变，
   原始值永远能重新算。归一化值另外单列，仅作展示参考。

   Store the RAW signal strength, not the normalised one. Normalisation logic
   will change; raw values can always be re-derived. The normalised value is a
   separate, display-only column.

3. **所有时间戳统一 UTC**，展示层再转本地。混用 UTC 与本地时间是最容易
   出诡异 bug 的地方。

   All timestamps are UTC. Converting in the storage layer invites bugs.

4. **signal_type 用字符串**，不用枚举或外键 —— 将来新增信号类型
   （例如天气）不需要改表结构。

   signal_type is a plain string, not an enum/FK, so adding a signal type
   later needs no schema change.

5. **写入失败绝不能阻断主预测流程**。对照是"为了以后能变好"，不是
   预测链路的一环；它挂了最多是以后没数据，不能让今天的预测发不出去。

   A ledger write failure must never break prediction. The ledger exists so
   tomorrow can be better; if it fails we lose data, not today's predictions.

不变量 / Invariants
-------------------
* 无任何对照数据时不写"看起来正常"的假记录。没有 signal_prediction 就写
  NULL 并标 partial_failure —— 拿 baseline 概率冒充 signal 概率会让差值恒为 0，
  表现为"信号没用"的假阴性，比直接报错危险得多。

  Never fabricate a plausible-looking row. Missing signal_prediction means
  NULL + partial_failure. Filling it with the baseline would make every delta
  exactly zero, i.e. a false "signal does nothing" — far worse than an error.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# 变更类型 / Change types.
#   applied  信号存在并已施加
#   revoked  信号曾经存在但已消失（例如伤停球员赛前复出进入大名单）。
#            "信号消失"本身就是一次信号变化，必须单独记录，否则复盘会把
#            "信号撤销后仍然命中的比赛"错误地算成"信号有效"。
CHANGE_APPLIED = "applied"
CHANGE_REVOKED = "revoked"
VALID_CHANGE_TYPES = (CHANGE_APPLIED, CHANGE_REVOKED)

# 记录状态 / Row status.
#   ok               baseline 与 signal 版预测都有
#   partial_failure  baseline 有、signal 版算出错了（或反之）。
#                    必须区分"信号没用"和"信号根本没算出来"——后者是 bug。
STATUS_OK = "ok"
STATUS_PARTIAL_FAILURE = "partial_failure"
VALID_STATUSES = (STATUS_OK, STATUS_PARTIAL_FAILURE)

# 已注册的信号类型 / Registered signal types.
# 新增类型时在这里登记即可，不需要改表结构（signal_type 是字符串列）。
SIGNAL_INJURY = "injury"          # 伤停
SIGNAL_LINEUP = "lineup"          # 首发阵容（含阵型倾向）
SIGNAL_RATING = "rating"          # 首发球员评估
SIGNAL_WEATHER = "weather"        # 天气

SIGNAL_REGISTRY: tuple[tuple[str, str, str], ...] = (
    (SIGNAL_INJURY, "伤停", "按位置加权的缺阵负荷 → 攻防折损"),
    (SIGNAL_LINEUP, "首发阵容", "首发 11 人构成 → 攻守倾向"),
    (SIGNAL_RATING, "球员评估", "首发加权平均评分 → 真实出场实力"),
    (SIGNAL_WEATHER, "天气", "开赛时点气象 → 双方对称的进球率缩放"),
)

# 强信号阈值：命中率的主指标只统计强度达到该值的场次。
# 弱信号本来就不该对预测产生明显影响，把它们混进主指标只会稀释信噪比。
DEFAULT_STRONG_THRESHOLD = 0.15
# 复盘"样本不足"阈值：低于该场次只报数量、不报命中率。
DEFAULT_MIN_SAMPLES = 30


def utc_now_iso() -> str:
    """当前 UTC 时间（ISO-8601，秒精度）。存储层一律用这个。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite_or_none(value: Any) -> float | None:
    """转成有限浮点数；无法转换（None / NaN / inf / 非数字）时返回 None。"""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def normalise_probs(probs: Any) -> list[float] | None:
    """把任意顺序的三项概率规整成 [主胜, 平, 客胜]；非法返回 None。

    接受 list/tuple 或 {"home","draw","away"} 字典两种形态，因为调用方
    既有 analyzer 的字典口径也有集成器的元组口径。
    """
    if isinstance(probs, dict):
        vals = [probs.get("home"), probs.get("draw"), probs.get("away")]
    elif isinstance(probs, (list, tuple)):
        vals = list(probs[:3])
    else:
        return None
    out: list[float] = []
    for v in vals:
        f = _finite_or_none(v)
        if f is None or f < 0:
            return None
        out.append(f)
    if len(out) != 3 or sum(out) <= 0:
        return None
    total = sum(out)
    return [v / total for v in out]


def _dump_probs(probs: Any) -> str | None:
    """概率 → JSON 字符串；非法返回 None（绝不写入半截数据）。"""
    norm = normalise_probs(probs)
    if norm is None:
        return None
    return json.dumps({"home": norm[0], "draw": norm[1], "away": norm[2]},
                      ensure_ascii=False)


def _load_probs(raw: Any) -> list[float] | None:
    """JSON 字符串 → [主胜, 平, 客胜]；缺失或损坏返回 None。"""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return normalise_probs(data)


def describe(signal_type: str, detail: dict[str, Any] | None) -> str:
    """生成 human_readable 描述，复盘时看这句话比看原始数据快得多。

    刻意只挑一两个最关键的数字 —— 描述的目的是让人一眼判断"这场为什么
    会有信号"，不是把整份 JSON 复述一遍。
    """
    d = detail or {}
    label = dict((t, n) for t, n, _ in SIGNAL_REGISTRY).get(signal_type, signal_type)
    if signal_type == SIGNAL_INJURY:
        load = _finite_or_none(d.get("absence_load"))
        if load is None:
            return f"{label}信号：无有效缺阵负荷"
        return f"{label}信号：有效缺阵负荷 {load:.2f} 人，预计本队进攻下降"
    if signal_type == SIGNAL_LINEUP:
        form = d.get("formation") or "未知阵型"
        tilt = _finite_or_none(d.get("tilt"))
        tilt_txt = "中性" if not tilt else ("偏进攻" if tilt > 0 else "偏防守")
        return f"{label}信号：{form}，{tilt_txt}"
    if signal_type == SIGNAL_RATING:
        diff = _finite_or_none(d.get("rating_diff"))
        if diff is None:
            return f"{label}信号：无有效评分差"
        side = "主队" if diff > 0 else "客队"
        return f"{label}信号：{side}首发实力高出 {abs(diff):.2f} 分"
    if signal_type == SIGNAL_WEATHER:
        factor = _finite_or_none(d.get("weather_factor"))
        if factor is None:
            return f"{label}信号：无有效气象数据"
        pct = (factor - 1.0) * 100
        return f"{label}信号：进球率 {pct:+.1f}%"
    return f"{label}信号：已触发"


def signal_label(signal_type: str) -> str:
    """信号类型的中文名（registry 里没有就回落到类型字符串）。"""
    return dict((t, n) for t, n, _ in SIGNAL_REGISTRY).get(signal_type, signal_type)


def describe_revoked(signal_type: str) -> str:
    """信号被撤销时的 human_readable 描述。

    必须说清"本场不再计入该信号评估"——否则复盘时看到这一行会误以为
    它和其他行一样能用来判断信号有没有用。
    """
    return (f"{signal_label(signal_type)}信号已撤销（赛前状态变化，"
            f"如球员复出进入大名单），本场不再计入该信号评估")


def outcome_index(home: Any, away: Any) -> int | None:
    """比分 → 0/1/2（主胜/平/客胜）；比分缺失返回 None。"""
    h, a = _finite_or_none(home), _finite_or_none(away)
    if h is None or a is None:
        return None
    return 0 if h > a else (1 if h == a else 2)


def log_loss(probs: list[float] | None, idx: int | None,
             floor: float = 1e-9) -> float | None:
    """三项 Log Loss；概率或赛果缺失时返回 None（绝不猜测）。"""
    if probs is None or idx is None or not (0 <= idx < len(probs)):
        return None
    p = probs[idx]
    if p <= 0:
        return None
    return -math.log(max(p, floor))


def rps(probs: list[float] | None, idx: int | None) -> float | None:
    """有序概率评分（Brier 的足球版：承认胜平负有顺序）。越低越准。"""
    if probs is None or idx is None or not (0 <= idx < len(probs)):
        return None
    actual = [0.0, 0.0, 0.0]
    actual[idx] = 1.0
    cum_p = cum_a = 0.0
    total = 0.0
    for p, a in zip(probs, actual):
        cum_p += p
        cum_a += a
        total += (cum_p - cum_a) ** 2
    return total


def hit(probs: list[float] | None, idx: int | None) -> int | None:
    """是否命中（预测最高概率方向 == 实际赛果）；无法判定返回 None。"""
    if probs is None or idx is None or sum(probs) <= 0:
        return None
    return 1 if max(range(3), key=lambda i: probs[i]) == idx else 0


# ---- 统计口径 -------------------------------------------------------------
# 两个口径必须分开存，混在一起会得出错误结论：
#   all    该信号类型的**全部**对照行（含弱信号）
#   strong 只统计强度达标的"强信号场"——弱信号本来就不该产生明显影响，
#          把它们混进主指标只会稀释信噪比，故 strong 是**主指标**。
#
# Both views must be stored separately; merging them misleads. `strong` is the
# headline metric: weak signals are not expected to move anything, so including
# them only dilutes the signal-to-noise ratio.

def _mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def bootstrap_ci(pairs: list[tuple[float | None, float | None]],
                 *, resamples: int = 2000, ci: float = 0.95,
                 seed: int | None = None) -> tuple[float, float] | None:
    """对「signal − base」的成对差值做 bootstrap，返回 (lo, hi)。

    成对重采样（而不是分别对两组重采样）是因为两版预测来自同一场比赛，
    不独立 —— 分开重采样会低估方差、给出过窄的置信区间。

    差值 < 0 表示信号让指标**变好**（Log Loss / RPS 越低越准）。
    区间跨 0 即"看不出差异"，此时不能宣称信号有效。
    """
    deltas = [s - b for b, s in pairs if b is not None and s is not None]
    n = len(deltas)
    if n < 2:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(max(int(resamples), 1)):
        total = 0.0
        for _ in range(n):
            total += deltas[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    alpha = (1.0 - ci) / 2.0
    lo = means[int(math.floor(alpha * len(means)))]
    hi = means[int(math.ceil((1.0 - alpha) * len(means))) - 1]
    return (lo, hi)


def _view(rows: list[dict[str, Any]], *, base_key: str, sig_key: str,
          label: str, min_samples: int) -> dict[str, Any]:
    """在给定行集合上算一个口径的全部指标。"""
    pairs_ll: list[tuple[float | None, float | None]] = []
    hits_b: list[int | None] = []
    hits_s: list[int | None] = []
    ll_b: list[float | None] = []
    ll_s: list[float | None] = []
    rps_b: list[float | None] = []
    rps_s: list[float | None] = []
    for row in rows:
        idx = outcome_index(row.get("actual_home"), row.get("actual_away"))
        if idx is None:
            continue                       # 未结算的行不参与统计
        base = _load_probs(row.get(base_key))
        sig = _load_probs(row.get(sig_key))
        pairs_ll.append((log_loss(base, idx), log_loss(sig, idx)))
        hits_b.append(hit(base, idx))
        hits_s.append(hit(sig, idx))
        ll_b.append(log_loss(base, idx))
        ll_s.append(log_loss(sig, idx))
        rps_b.append(rps(base, idx))
        rps_s.append(rps(sig, idx))
    hb, hs = _mean(hits_b), _mean(hits_s)
    lb, ls = _mean(ll_b), _mean(ll_s)
    rb, rs_ = _mean(rps_b), _mean(rps_s)
    n = len([p for p in pairs_ll if p[0] is not None and p[1] is not None])
    return {
        "label": label,
        "n": n,
        "hit_base": hb,
        "hit_signal": hs,
        "hit_delta": (hs - hb) if (hb is not None and hs is not None) else None,
        "logloss_base": lb,
        "logloss_signal": ls,
        "logloss_delta": (ls - lb) if (lb is not None and ls is not None) else None,
        "rps_base": rb,
        "rps_signal": rs_,
        "rps_delta": (rs_ - rb) if (rb is not None and rs_ is not None) else None,
        "insufficient": n < max(int(min_samples), 1),
        "ci_logloss": bootstrap_ci(pairs_ll),
    }


def summarise(rows: list[Any], *, strong_threshold: float = DEFAULT_STRONG_THRESHOLD,
              min_samples: int = DEFAULT_MIN_SAMPLES,
              seed: int | None = 20261014) -> dict[str, Any]:
    """把对照行聚合成「每种信号有没有用」的结论。

    rows 可以是 sqlite3.Row 或 dict；无法识别的形态返回空结论而不是报错
    （复盘不能因为读不到对照数据就整个挂掉）。

    主指标是 **strong 口径**（强信号场）；all 口径一并给出，但不能拿它
    下结论 —— 弱信号本来就预期无效，混进来只会把真实效果稀释掉。

    对照 baseline 用 **partial_baseline**（有其他信号、独缺本类），这才是
    "这一类信号的增量价值"；pure_baseline 另给一列，用于看"所有信号合起来"。

    **撤销行（change_type='revoked'）一律排除在统计之外。** 信号被撤销后
    我们发出的预测里已经不含它了，但"如果它还在会怎么说"无法反推。把这类
    行算进去，会把"信号撤销后仍然命中"错误地记成"信号有效"，结论系统性偏乐观。
    """
    parsed: list[dict[str, Any]] = []
    revoked: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            item = row
        else:
            try:
                item = dict(row)
            except (TypeError, ValueError):
                continue
        if str(item.get("change_type") or CHANGE_APPLIED) == CHANGE_REVOKED:
            revoked.append(item)
        else:
            parsed.append(item)
    revoked_by_type: dict[str, int] = {}
    for r in revoked:
        key = str(r.get("signal_type"))
        revoked_by_type[key] = revoked_by_type.get(key, 0) + 1
    if not parsed:
        return {"total": 0, "by_signal": {}, "threshold": strong_threshold,
                "min_samples": min_samples, "revoked": len(revoked),
                "revoked_by_type": revoked_by_type}

    by_signal: dict[str, Any] = {}
    types = sorted({str(r.get("signal_type")) for r in parsed
                    if r.get("signal_type")})
    for sig_type in types:
        mine = [r for r in parsed if str(r.get("signal_type")) == sig_type]
        strong = []
        for r in mine:
            strength = _finite_or_none(r.get("signal_strength"))
            if strength is not None and abs(strength) >= strong_threshold:
                strong.append(r)
        by_signal[sig_type] = {
            "all": _view(mine, base_key="partial_baseline",
                         sig_key="signal_prediction", label="all",
                         min_samples=min_samples),
            "strong": _view(strong, base_key="partial_baseline",
                            sig_key="signal_prediction", label="strong",
                            min_samples=min_samples),
            "vs_pure": _view(mine, base_key="pure_baseline",
                             sig_key="signal_prediction", label="vs_pure",
                             min_samples=min_samples),
            "n_rows": len(mine),
            "n_strong": len(strong),
            "n_revoked": revoked_by_type.get(sig_type, 0),
        }
    return {"total": len(parsed), "by_signal": by_signal,
            "threshold": strong_threshold, "min_samples": min_samples,
            "revoked": len(revoked), "revoked_by_type": revoked_by_type}


def verdict(entry: dict[str, Any]) -> str:
    """把一种信号的统计结论翻译成一句话。样本不足时**明确说不足**。

    刻意不做自动调参：样本少时自动调参会把偶然波动当成规律，越学越差。
    这里只负责把结论说清楚，改不改由人决定。
    """
    strong = entry.get("strong") or {}
    n = strong.get("n") or 0
    if n < max(int(entry.get("min_samples", DEFAULT_MIN_SAMPLES) or 0), 1):
        return f"样本不足（{n} 场），暂不评估"
    delta = strong.get("logloss_delta")
    ci = strong.get("ci_logloss")
    if delta is None:
        return f"样本不足（{n} 场有效），暂不评估"
    if ci and ci[0] <= 0 <= ci[1]:
        return f"无显著差异（{n} 场，Log Loss Δ={delta:+.4f}，置信区间跨 0）"
    better = "更好" if delta < 0 else "更差"
    hit_d = strong.get("hit_delta")
    hit_txt = "" if hit_d is None else f"，命中率 {hit_d * 100:+.1f}pp"
    return f"信号使预测{better}（{n} 场，Log Loss Δ={delta:+.4f}{hit_txt}）"
