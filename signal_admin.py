"""信号权重的治理：模拟、确认、留痕 / Weight governance.

为什么单独一层 / Why this layer exists
--------------------------------------
调权重是本项目**唯一会真实改变预测结果**的操作，比改代码更危险：
代码错了会测试失败，权重错了只是悄悄变差，半年后才发现。所以这里
刻意把"能看"和"能改"分开，并要求**先模拟再确认**。

    Tuning weights is the only operation here that silently changes real
    predictions — worse than a code bug, which at least fails a test. So
    "read" and "write" are separated, and simulate-before-apply is mandatory.

三条硬约束 / Three hard rules
-----------------------------
1. **默认只有读权限**。改权重要显式开 `SIGNAL_AB_WRITE_ENABLED`，线上服务
   不开 —— 这样任何代码 bug 都不可能意外改掉权重。

   Read-only by default. Writing needs SIGNAL_AB_WRITE_ENABLED, which the
   production service does not set, so no bug can accidentally retune weights.

2. **必须先模拟**。应用前要算出"如果应用这个权重，过去 N 场会怎样"，
   看模拟结果再决定。直接应用等于拿线上预测做实验。

   Simulate first. Applying without seeing the counterfactual is running the
   experiment on live predictions.

3. **历史只追加**。每次变更（含"仅建议"）都写 `signal_weight_log`，
   旧值永久保留 —— 否则回答不了"那段时间差是不是权重不好"。

   Append-only history, including suggestions.

诚实边界 / Honest limit
-----------------------
**模拟是线性近似，不是重跑模型。** 我们存的是三版概率，没有存 λ，
于是只能按 `partial + (signal − partial) · (w_new / w_cur)` 缩放。真实
模型走半饱和曲线、非线性，所以模拟只能判断**方向**，不能当作最终数值。
要精确就得重跑模型，那需要原始输入数据，对照表里没有（也不该有，太大）。

    Simulation is a LINEAR approximation, not a model re-run. We store three
    probability vectors, not λ, so we can only scale the delta. The real model
    is non-linear (saturation curve), so trust the *sign*, not the magnitude.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any

from signal_ab import (_finite_or_none, _load_probs, bootstrap_ci, hit,
                       log_loss, outcome_index, rps, utc_now_iso)

log = logging.getLogger(__name__)

# 模拟时允许的最小/最大缩放倍率，防止手滑填出荒谬权重。
SCALE_MIN, SCALE_MAX = 0.0, 5.0


def _mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def scale_probs(base: list[float], signal: list[float],
                scale: float) -> list[float]:
    """把"信号带来的偏移"按倍率缩放，再归一化回合法概率。

    scale=1 → 等于 signal；scale=0 → 等于 base（信号完全不生效）。
    归一化后仍可能出现极小负值（外推时），夹到 0 并重新归一化。
    """
    out = [b + (s - b) * scale for b, s in zip(base, signal)]
    out = [max(v, 0.0) for v in out]
    total = sum(out)
    if total <= 0:
        return base
    return [v / total for v in out]


def simulate_weight(rows: list[Any], signal_type: str, *, new_weight: float,
                    current_weight: float = 1.0,
                    strong_threshold: float = 0.15,
                    min_samples: int = 30,
                    seed: int | None = 20261014) -> dict[str, Any]:
    """模拟：把该类信号的强度换成 new_weight，过去这些场会变成什么样。

    只统计已结算、且强度达标的强信号场 —— 弱信号场本来就不动，
    把它们算进来只会让模拟结果显得"没变化"。
    """
    parsed = []
    for row in rows or []:
        if isinstance(row, dict):
            parsed.append(row)
        else:
            try:
                parsed.append(dict(row))
            except (TypeError, ValueError):
                continue
    mine = [r for r in parsed
            if str(r.get("signal_type")) == str(signal_type)
            and abs(_finite_or_none(r.get("signal_strength")) or 0.0) >= strong_threshold]
    settled = [r for r in mine
               if outcome_index(r.get("actual_home"), r.get("actual_away")) is not None]

    cur_w = _finite_or_none(current_weight) or 1.0
    new_w = _finite_or_none(new_weight)
    if new_w is None or cur_w <= 0:
        return {"n": len(settled), "error": "权重非法", "sufficient": False}
    scale = min(max(new_w / cur_w, SCALE_MIN), SCALE_MAX)

    ll_cur, ll_new = [], []
    hit_cur, hit_new = [], []
    for row in settled:
        idx = outcome_index(row.get("actual_home"), row.get("actual_away"))
        base = _load_probs(row.get("partial_baseline"))
        sig = _load_probs(row.get("signal_prediction"))
        if base is None or sig is None:
            continue
        # 当前状态 = signal 版（scale=1）；模拟 = 按倍率缩放后的版本
        sim = scale_probs(base, sig, scale)
        ll_cur.append(log_loss(sig, idx))
        ll_new.append(log_loss(sim, idx))
        hit_cur.append(hit(sig, idx))
        hit_new.append(hit(sim, idx))

    pairs = list(zip(ll_cur, ll_new))
    n = len([p for p in pairs if p[0] is not None and p[1] is not None])
    lc, ln = _mean(ll_cur), _mean(ll_new)
    return {
        "signal_type": signal_type,
        "current_weight": cur_w,
        "new_weight": new_w,
        "scale": scale,
        "n": n,
        "logloss_current": lc,
        "logloss_simulated": ln,
        "logloss_delta": (ln - lc) if (lc is not None and ln is not None) else None,
        "hit_current": _mean(hit_cur),
        "hit_simulated": _mean(hit_new),
        "sufficient": n >= max(int(min_samples), 1),
        "ci": bootstrap_ci([(b, s) for b, s in pairs if b is not None
                            and s is not None], seed=seed),
    }


def simulate_text(sim: dict[str, Any]) -> str:
    """把模拟结果说成一句话，供"应用前人工确认"时看。"""
    if sim.get("error"):
        return f"无法模拟：{sim['error']}"
    n = sim.get("n") or 0
    if not sim.get("sufficient"):
        return (f"样本不足（{n} 场），模拟不足以支撑判断 —— "
                f"先攒样本，不要改权重")
    delta = sim.get("logloss_delta")
    ci = sim.get("ci")
    if delta is None:
        return f"有效样本 {n} 场，但无法计算差异"
    tail = ""
    if ci:
        cross = ci[0] <= 0 <= ci[1]
        tail = f"，95% 置信区间 [{ci[0]:+.4f}, {ci[1]:+.4f}]" + \
               ("（跨 0，看不出差异）" if cross else "")
    direction = "更好" if delta < 0 else ("更差" if delta > 0 else "无变化")
    return (f"按新权重 {sim['new_weight']}（{n} 场）：Log Loss {delta:+.4f} "
            f"→ {direction}{tail}")


def apply_weight(repo: Any, *, signal_type: str, old_weight: float,
                 new_weight: float, write_enabled: bool = False,
                 confirm: bool = False, sim: dict[str, Any] | None = None,
                 reason: str = "") -> tuple[bool, str]:
    """写入一次权重变更。**默认拒绝**，必须同时满足两个条件才真正应用。

    返回 (是否应用, 说明)。即使只写"建议"（confirm=False）也会留痕，
    这样"当时考虑过但没采纳"也有据可查。
    """
    if not write_enabled:
        # 仍记录建议，标记为未应用 —— 便于回溯"曾经想调但没调"
        if repo is not None:
            repo.log_signal_weight(
                signal_type=signal_type, old_weight=old_weight,
                new_weight=new_weight, applied=False,
                n_samples=(sim or {}).get("n"), metric="logloss",
                reason="只读模式，未应用")
        return False, "只读模式（SIGNAL_AB_WRITE_ENABLED 未开启），未应用"
    if not confirm:
        if repo is not None:
            repo.log_signal_weight(
                signal_type=signal_type, old_weight=old_weight,
                new_weight=new_weight, applied=False,
                n_samples=(sim or {}).get("n"), metric="logloss",
                reason="缺少二次确认，未应用")
        return False, "缺少二次确认（confirm=True），未应用"
    ok = True
    if repo is not None:
        ok = repo.log_signal_weight(
            signal_type=signal_type, old_weight=old_weight,
            new_weight=new_weight, applied=True,
            n_samples=(sim or {}).get("n"), metric="logloss",
            reason=reason or "人工确认后应用")
    return bool(ok), "已应用并留痕" if ok else "写入权重历史失败"


def save_config_snapshot(repo: Any, *, version: str,
                         payload: dict[str, Any] | None = None,
                         settings: Any = None) -> bool:
    """记录发布级配置快照。

    半年后回答"那时对照结果为什么长那样"必须靠它 —— 代码里只有今天的
    默认值，而当时的强度/阈值/融合权重可能完全不同。
    """
    if repo is None:
        return False
    payload = dict(payload or {})
    if settings is not None:
        for key in ("prematch_enabled", "prematch_strength", "prematch_tilt",
                    "rating_strength", "weather_strength", "market_weight",
                    "temperature", "signal_ab_enabled"):
            payload.setdefault(key, getattr(settings, key, None))
    try:
        return repo.save_config_snapshot(version=version, payload=payload,
                                         created_at=utc_now_iso())
    except Exception as exc:  # noqa: BLE001
        log.warning("写入配置快照失败：%s", exc)
        return False


CSV_COLUMNS = ("fixture_id", "signal_type", "signal_strength", "change_type",
               "status", "error", "pure_baseline", "partial_baseline",
               "signal_prediction", "actual_home", "actual_away",
               "human_note", "created_at", "updated_at")


def export_csv(rows: list[Any]) -> str:
    """对照数据导出成 CSV 文本。

    提前留好导出口原因很简单：真要做统计检验（配对 t 检验、McNemar 之类）
    不会在这个仓库里做，得导出去用专门工具。临时写脚本会漏字段、漏口径。
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS),
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows or []:
        try:
            item = dict(row) if not isinstance(row, dict) else row
        except (TypeError, ValueError):
            continue
        writer.writerow({k: item.get(k) for k in CSV_COLUMNS})
    return buf.getvalue()


def rows_to_jsonl(rows: list[Any]) -> str:
    """JSONL 导出（保留嵌套结构，比 CSV 更适合程序化处理）。"""
    out = []
    for row in rows or []:
        try:
            item = dict(row) if not isinstance(row, dict) else row
        except (TypeError, ValueError):
            continue
        out.append(json.dumps({k: item.get(k) for k in CSV_COLUMNS},
                              ensure_ascii=False))
    return "\n".join(out)
