"""赛前信号对照采集 / Capturing the pre-match signal A/B rows.

为什么单独一个模块 / Why a separate module
-------------------------------------------
`repository.save_signal_ab` 只负责**存**，它不该知道 λ 怎么算、哪几种信号
被触发了。而 `service._predict_one` 已经很长了，再塞进"算三版预测"会把
预测主流程淹掉。于是把这段逻辑独立出来，service 只负责在拿到分类因子后
调一次 `capture()`。

    repository.save_signal_ab only *stores*; it must not know how λ is computed
    or which signals fired. service._predict_one is already long — inlining
    "compute three prediction variants" would bury the main flow. So it lives
    here, and service calls capture() once after it has the per-signal factors.

三版预测的含义 / What the three variants mean
---------------------------------------------
一行 = 一场比赛 × 一种信号。对每种触发的信号都存三版：

    pure_baseline      完全不施加任何赛前信号
    partial_baseline   施加除本类之外的其他信号（独缺本类）
    signal_prediction  施加全部信号（含本类）

比较 signal 与 partial 才是"这一类信号值不值"；比较 signal 与 pure 是
"所有信号合起来值不值"。只存两版的话，多信号同时触发时无法归因。

    Comparing signal vs partial isolates *this* signal's worth; signal vs pure
    measures all signals together. Two variants cannot attribute value when
    several signals fire at once.

诚实边界 / Honest limits
-----------------------
* partial_baseline 是**重算**而非"从全量里减掉本类"。伤停走半饱和曲线、
  阵型倾向是加性叠加，两者不是可分离的线性项，减法会得出错误的反事实。
  重算的代价是并行触发时近似成立（差异量级见 PREMATCH 强度，很小）。

  partial_baseline is **recomputed**, not "full minus this signal". Injuries run
  through a saturation curve and formation tilt is additive; they are not
  separable linear terms, so subtraction would produce a wrong counterfactual.
  Recomputation is approximate when signals co-fire (the gap is tiny).

* 采集失败一律只告警。对照是"为了以后能变好"，不能因为它挂了导致今天的
  预测发不出去。

  A capture failure only warns. The ledger exists so tomorrow can be better;
  it must never cost today's prediction.
"""

from __future__ import annotations

import logging
from typing import Any

from context import context_factors, merge_factors
from prematch import squad_factors
from signal_ab import (CHANGE_APPLIED, CHANGE_REVOKED, SIGNAL_INJURY,
                       SIGNAL_LINEUP, SIGNAL_RATING, SIGNAL_WEATHER,
                       describe, describe_revoked)

log = logging.getLogger(__name__)

# 因子中性判定阈值：小于该偏差视为"这一类信号没有真正触发"。
# 不能要求精确等于 0 —— 浮点运算必然有尾差。
NEUTRAL_EPS = 1e-12

# 四类信号的固定顺序：保证 merge 子集时顺序稳定，结果可复现。
SIGNAL_ORDER = (SIGNAL_INJURY, SIGNAL_LINEUP, SIGNAL_RATING, SIGNAL_WEATHER)


def _is_neutral(factors: dict[str, Any] | None) -> bool:
    """该类因子是否对所有 λ 都没有实际影响（未触发）。"""
    if not factors:
        return True
    for side in ("home", "away"):
        part = factors.get(side) or {}
        for key in ("attack", "concede"):
            try:
                if abs(float(part.get(key, 1.0)) - 1.0) > NEUTRAL_EPS:
                    return False
            except (TypeError, ValueError):
                continue
    try:
        if abs(float(factors.get("weather", 1.0)) - 1.0) > NEUTRAL_EPS:
            return False
    except (TypeError, ValueError):
        pass
    return True


def split_factors(
    home_id: Any,
    away_id: Any,
    *,
    injuries: list[dict[str, Any]] | None = None,
    lineups: list[dict[str, Any]] | None = None,
    loads: dict[str, float] | None = None,
    start_xi: dict[str, list[dict[str, Any]]] | None = None,
    ratings: dict[str, Any] | None = None,
    weather_sample: Any | None = None,
    prematch_strength: float = 0.0,
    prematch_tilt: float = 0.0,
    rating_strength: float = 0.0,
    weather_strength: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """把四类赛前信号**各自独立**算成一套因子。

    关键手法是把"不相关的那一路强度置 0"，让另一路的因子恒等中性：
      * 伤停   —— 只给 injuries/loads，阵型权重为 0
      * 阵容   —— 折损强度置 0（于是攻防恒 1），只留阵型倾向
      * 评分   —— 天气强度置 0
      * 天气   —— 评分强度置 0

    这样每一类都能单独施加、单独移除，partial_baseline 才成立。
    """
    out: dict[str, dict[str, Any]] = {}
    out[SIGNAL_INJURY] = squad_factors(
        home_id, away_id, injuries=injuries, lineups=None,
        strength=prematch_strength, tilt_weight=0.0, loads=loads)
    out[SIGNAL_LINEUP] = squad_factors(
        home_id, away_id, injuries=None, lineups=lineups,
        strength=0.0, tilt_weight=prematch_tilt)
    out[SIGNAL_RATING] = context_factors(
        home_id, away_id, start_xi=start_xi, ratings=ratings,
        rating_strength=rating_strength,
        weather_sample=None, weather_strength=0.0)
    out[SIGNAL_WEATHER] = context_factors(
        home_id, away_id, start_xi=None, ratings=None,
        rating_strength=0.0, weather_sample=weather_sample,
        weather_strength=weather_strength)
    return out


def raw_strength(signal_type: str, factors: dict[str, Any] | None) -> float | None:
    """取该类信号的**原始**强度（归一化前的量，将来口径变了还能重算）。"""
    if not factors:
        return None
    detail = factors.get("detail") or {}
    if signal_type == SIGNAL_INJURY:
        vals = [float((detail.get(s) or {}).get("absence_load") or 0.0)
                for s in ("home", "away")]
        return max(vals) if vals else None
    if signal_type == SIGNAL_LINEUP:
        vals = [abs(float((detail.get(s) or {}).get("tilt") or 0.0))
                for s in ("home", "away")]
        return max(vals) if vals else None
    if signal_type == SIGNAL_RATING:
        vals = [abs(float((detail.get(s) or {}).get("rating_diff") or 0.0))
                for s in ("home", "away")]
        return max(vals) if vals else None
    if signal_type == SIGNAL_WEATHER:
        w = (detail.get("weather") or {})
        try:
            return abs(float(w.get("multiplier") or 1.0) - 1.0)
        except (TypeError, ValueError):
            return None
    return None


def previous_applied(repo: Any, fixture_id: Any) -> set[str]:
    """查这场比赛此前**已写入且仍标为 applied** 的信号类型。

    用来识别"信号被撤销"：某类信号上一轮还在（例如球员标记伤停），这一轮
    消失了（赛前复出进入大名单）。信号消失本身就是一次变化，必须记一行
    revoked —— 否则复盘会把"信号撤销后仍然命中的比赛"错算成"信号有效"。

    读不到就返回空集合：查不到历史最多是识别不出撤销，不能因此让采集挂掉。
    """
    if repo is None:
        return set()
    try:
        rows = repo.signal_ab_for_fixture(fixture_id) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("读取历史信号对照失败（fixture=%s）：%s", fixture_id, exc)
        return set()
    out: set[str] = set()
    for row in rows:
        try:
            item = dict(row)
        except (TypeError, ValueError):
            continue
        if str(item.get("change_type") or CHANGE_APPLIED) == CHANGE_APPLIED:
            sig = item.get("signal_type")
            if sig:
                out.add(str(sig))
    return out


def _three_way(analysis: dict[str, Any] | None) -> tuple[float, float, float] | None:
    """预测结果 → (主胜, 平, 客胜)；任一分量非法返回 None（绝不猜）。"""
    if not analysis:
        return None
    try:
        out = (float(analysis["win_prob"]), float(analysis["draw_prob"]),
               float(analysis["loss_prob"]))
    except (KeyError, TypeError, ValueError):
        return None
    if any(v != v or v < 0 for v in out):      # NaN 或负数
        return None
    if sum(out) <= 0:
        return None
    return out


def capture(
    repo: Any,
    *,
    fixture_id: Any,
    analyzer: Any,
    model: Any,
    home_id: Any,
    away_id: Any,
    by_signal: dict[str, dict[str, Any]] | None,
    config_snapshot: dict[str, Any] | None = None,
    temperature: float | None = None,
    is_test: bool = False,
) -> int:
    """为每种**真正触发**的信号写一行对照记录，返回写入行数。

    未触发的信号不写行 —— 写"信号强度 0、两版概率完全相同"的空行会把
    后面的统计稀释成"信号没用"，那种假阴性比没有数据更糟。

    任何异常都被吞掉并告警：对照挂了最多是以后没数据，不能让今天的
    预测发不出去。
    """
    if not by_signal or repo is None or analyzer is None:
        return 0
    try:
        active = {k: v for k, v in by_signal.items() if not _is_neutral(v)}
        # 先查历史：即使这一轮一类信号都没触发，也可能存在"信号被撤销"
        # （例如伤停球员赛前复出）。此时仍然要写 revoked 行。
        already = previous_applied(repo, fixture_id)
        if not active and not already:
            return 0

        def _run(prematch: dict[str, Any] | None) -> tuple[float, float, float] | None:
            return _three_way(analyzer.predict_match(
                model, home_id, away_id, temperature=temperature, prematch=prematch))

        pure = _run(None)
        if pure is None:
            return 0
        full_error = ""
        if active:
            full = merge_factors(*[active[k] for k in SIGNAL_ORDER if k in active])
            try:
                signal_p = _run(full)
            except Exception as exc:  # noqa: BLE001
                # 全量版算不出来：仍把 pure 记下来并标 partial_failure，
                # 以便区分"信号没用"和"信号根本没算出来"。
                signal_p, full_error = None, str(exc)
        else:
            # 没有任何信号触发时，实际发出的预测就是无任何信号的那一版。
            signal_p = pure

        written = 0
        for sig_type in SIGNAL_ORDER:
            if sig_type not in active:
                continue
            # 每一类单独 try：某一类的反事实算不出来，不能连累其他类。
            # 否则一次失败会丢掉整场的对照数据，排查时只能看到"没数据"。
            partial_p: tuple[float, float, float] | None = pure
            partial_error: str | None = None
            try:
                others = [active[k] for k in SIGNAL_ORDER
                          if k in active and k != sig_type]
                partial_f = merge_factors(*others) if others else None
                if partial_f is not None:
                    partial_p = _run(partial_f)
            except Exception as exc:  # noqa: BLE001
                partial_p, partial_error = None, str(exc)
            error: str | None = None
            if signal_p is None:
                error = f"signal_prediction 计算失败（{full_error}）"
            elif partial_p is None:
                error = f"partial_baseline 计算失败（{partial_error}）"
            ok = repo.save_signal_ab(
                fixture_id=fixture_id,
                signal_type=sig_type,
                pure_baseline=pure,
                partial_baseline=partial_p,
                signal_prediction=signal_p,
                signal_strength=raw_strength(sig_type, active[sig_type]),
                human_note=describe(sig_type, active[sig_type].get("detail")),
                error=error,
                config_snapshot=config_snapshot,
                is_test=is_test,
            )
            written += 1 if ok else 0

        # ---- 撤销：此前 applied、这一轮消失了的信号 ------------------------
        # 信号消失本身是一次变化，必须单独写一行 revoked。这类行在统计里
        # 会被排除（见 signal_ab.summarise），但审计与排查 CLI 需要看得见。
        already = previous_applied(repo, fixture_id)
        current_p = signal_p if signal_p is not None else pure
        for sig_type in SIGNAL_ORDER:
            if sig_type in active or sig_type not in already:
                continue
            error = None
            if signal_p is None:
                error = f"signal_prediction 计算失败（{full_error}）"
            ok = repo.save_signal_ab(
                fixture_id=fixture_id,
                signal_type=sig_type,
                pure_baseline=pure,
                partial_baseline=current_p,
                signal_prediction=current_p,
                signal_strength=0.0,
                change_type=CHANGE_REVOKED,
                human_note=describe_revoked(sig_type),
                error=error,
                config_snapshot=config_snapshot,
                is_test=is_test,
            )
            written += 1 if ok else 0
        return written
    except Exception as exc:  # noqa: BLE001
        log.warning("采集赛前信号对照失败（fixture=%s）：%s", fixture_id, exc)
        return 0
