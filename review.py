"""每日复盘与归因 / Daily review & attribution.

为什么独立成模块：
    回测回答的是「模型在历史语料上好不好」，复盘回答的是
    「模型**昨天真实推送的那些比赛**准不准、准在哪、不准在哪」。
    两者口径不同：回测跑的是内置语料，复盘跑的是线上真实预测流水。
    只有复盘才能驱动「明天该不该信它、哪类比赛该信」。

设计原则（重要）：
    本模块**只读不改**。它不调整任何模型参数、不修改 predictions 表。
    「深度学习进步」的第一步是先看得见自己在哪准，而不是急着自动调参——
    小样本下自动调参极易过拟合（几十场比赛的波动会被当成信号），
    因此这里只产出归因结论，是否据此调参由人决定。

归因维度（全部只用赛前已知信息，无前视偏差）：
    - 联赛：不同联赛的可预测性差异很大
    - 预测方向：主胜 / 平局 / 客胜 各自的命中率
    - 置信度档：高 / 中 / 低，用来验证「模型说自己有把握时是否真的更准」
    - 口径：有赔率（市场融合）vs 无赔率（泊松回落）
    - 基线对照：永远猜主胜、按历史频率随机猜——没有基线就没有「好」的定义
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bot")

# 置信度分档：概率上限所在区间。
# 0.5 是三选一的理论均分点，达到它才算「模型真有倾向」。
HIGH_CONF = 0.50
MID_CONF = 0.40

# 归因结论的最小样本量。低于此值的分组只报数量不报命中率——
# 3 场全中显示 100% 会误导人去相信一个根本不存在的规律。
MIN_SAMPLE = 8

_OUTCOMES = ("主胜", "平局", "客胜")


def _outcome(hs: int, as_: int) -> str:
    """真实赛果方向。"""
    if hs > as_:
        return "主胜"
    if hs < as_:
        return "客胜"
    return "平局"


def _hit_rate(hits: int, total: int) -> float | None:
    """命中率；样本不足返回 None（不报数字比报错觉的数字更诚实）。"""
    return hits / total if total else None


def _conf_bucket(probs: tuple[float, float, float]) -> str:
    """按最大概率分档。"""
    top = max(probs)
    if top >= HIGH_CONF:
        return "高信心"
    if top >= MID_CONF:
        return "中信心"
    return "低信心"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


# ============================================================================
# 一、读取已结算样本
# ============================================================================

def _load(db_path: str, since_days: int) -> list[dict]:
    """取近 since_days 天已结算的预测。

    since_days <= 0 表示不限时间（全量）。
    """
    from analytics import _connect

    cutoff = None
    if since_days and since_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

    try:
        conn = _connect(db_path)
        if cutoff:
            rows = list(conn.execute(
                "SELECT fixture_id, league, home, away, kickoff, result, source,"
                "       home_prob, draw_prob, away_prob, best_score,"
                "       actual_home, actual_away, settled_at "
                "FROM predictions "
                "WHERE actual_home IS NOT NULL AND actual_away IS NOT NULL "
                "  AND kickoff >= ? ORDER BY kickoff", (cutoff,),
            ))
        else:
            rows = list(conn.execute(
                "SELECT fixture_id, league, home, away, kickoff, result, source,"
                "       home_prob, draw_prob, away_prob, best_score,"
                "       actual_home, actual_away, settled_at "
                "FROM predictions "
                "WHERE actual_home IS NOT NULL AND actual_away IS NOT NULL "
                "ORDER BY kickoff",
            ))
    except Exception as exc:  # 复盘失败绝不能拖垮机器人
        log.warning("复盘读取失败：%s", exc)
        return []

    out: list[dict] = []
    for r in rows:
        hs, as_ = r["actual_home"], r["actual_away"]
        if hs is None or as_ is None:
            continue
        hp = float(r["home_prob"] or 0)
        dp = float(r["draw_prob"] or 0)
        ap = float(r["away_prob"] or 0)
        # result 非法（历史遗留 pending/NULL）时按概率方向恢复，
        # 否则这场会被永远算作未命中，把整体命中率压低。
        pred = r["result"]
        if pred not in _OUTCOMES:
            pred = "主胜" if hp >= dp and hp >= ap else ("客胜" if ap >= dp else "平局")
        actual = _outcome(int(hs), int(as_))
        out.append({
            "league": r["league"] or "未标注",
            "home": r["home"], "away": r["away"], "kickoff": r["kickoff"],
            "pred": pred, "actual": actual, "hit": pred == actual,
            "probs": (hp, dp, ap), "conf": _conf_bucket((hp, dp, ap)),
            # 市场口径判定：有赔率才可能走上市场融合，否则是泊松回落
            "market": bool(r["source"]) and "football-data" not in str(r["source"] or ""),
            "score": f"{hs}-{as_}",
        })
    return out


# ============================================================================
# 二、分组归因
# ============================================================================

def _group(items: list[dict], key) -> list[dict]:
    """按 key 分组统计命中率，样本不足的组标 thin。"""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        buckets[key(it)].append(it)
    out = []
    for name, group in buckets.items():
        hits = sum(1 for g in group if g["hit"])
        out.append({
            "name": str(name), "n": len(group), "hits": hits,
            "rate": _hit_rate(hits, len(group)),
            "thin": len(group) < MIN_SAMPLE,
        })
    # 样本多的排前面：让结论先落在有统计意义的地方
    out.sort(key=lambda d: (-d["n"], -(d["rate"] or 0)))
    return out


def _baselines(items: list[dict]) -> dict:
    """朴素基线。没有基线，命中率就只是一个孤零零的数字。"""
    n = len(items)
    if not n:
        return {}
    always_home = sum(1 for it in items if it["actual"] == "主胜")
    freq: dict[str, int] = defaultdict(int)
    for it in items:
        freq[it["actual"]] += 1
    best_freq = max(freq.values()) if freq else 0
    return {
        "n": n,
        "always_home": always_home / n,          # 永远猜主胜
        "best_freq": best_freq / n,              # 永远猜最高频结果
        "random": 1 / 3,                         # 三选一瞎猜
        "dist": {k: v / n for k, v in freq.items()},
    }


# ============================================================================
# 三、主入口
# ============================================================================

def daily_review(db_path: str, since_days: int = 7) -> dict:
    """生成复盘报告。

    返回 status=empty（无已结算样本）/ ok。
    """
    items = _load(db_path, since_days)
    if not items:
        return {
            "status": "empty", "window_days": since_days, "settled": 0,
            "hint": "还没有已结算的预测。自动预测任务每天生成并入库，"
                    "比赛结束后由结算任务回写真实比分，次日即可看到复盘。",
        }

    n = len(items)
    hits = sum(1 for it in items if it["hit"])
    overall = hits / n
    base = _baselines(items)

    by_league = _group(items, lambda it: it["league"])
    by_dir = _group(items, lambda it: it["pred"])
    by_conf = _group(items, lambda it: it["conf"])
    by_src = _group(items, lambda it: "市场融合" if it["market"] else "泊松回落")

    # 高信心档是否真的更准：这是「模型有没有自知之明」的核心检验
    conf_ok = None
    hi = next((g for g in by_conf if g["name"] == "高信心"), None)
    lo = next((g for g in by_conf if g["name"] == "低信心"), None)
    if hi and lo and not hi["thin"] and not lo["thin"]:
        conf_ok = (hi["rate"] or 0) > (lo["rate"] or 0)

    # 最准 / 最不准：**只在同一个维度内部**比较。
    # 跨维度比较毫无意义——「欧冠 65%」和「高信心 65%」不是一类东西，
    # 把它们放一起评最好/最差只会得出荒谬结论。
    # 且每个维度至少要有 2 个样本充足的组才比较，否则就是自己跟自己比。
    def _extreme(groups: list[dict]) -> dict | None:
        solid = [g for g in groups if not g["thin"]]
        if len(solid) < 2:
            return None
        best_g = max(solid, key=lambda g: g["rate"] or 0)
        worst_g = min(solid, key=lambda g: g["rate"] or 0)
        if best_g["name"] == worst_g["name"]:
            return None
        # 差距太小时不值得报告：1% 的差别是噪音，不是规律
        if abs((best_g["rate"] or 0) - (worst_g["rate"] or 0)) < 0.05:
            return None
        return {"dim": None, "best": best_g, "worst": worst_g}

    extremes = {}
    for dim, groups in (("联赛", by_league), ("方向", by_dir),
                        ("信心档", by_conf), ("口径", by_src)):
        e = _extreme(groups)
        if e:
            e["dim"] = dim
            extremes[dim] = e
    # 保留旧字段以兼容既有调用方（测试 / 看板），取差距最大的那个维度
    headline = max(extremes.values(),
                   key=lambda e: (e["best"]["rate"] or 0) - (e["worst"]["rate"] or 0),
                   default=None)

    return {
        "status": "ok",
        "window_days": since_days,
        "settled": n,
        "hits": hits,
        "accuracy": overall,
        "baselines": base,
        "edge_vs_home": overall - base["always_home"],
        "edge_vs_freq": overall - base["best_freq"],
        "by_league": by_league,
        "by_direction": by_dir,
        "by_confidence": by_conf,
        "by_source": by_src,
        "confidence_meaningful": conf_ok,
        "best_group": (headline or {}).get("best"),
        "worst_group": (headline or {}).get("worst"),
        "extremes": extremes,
    }


# ============================================================================
# 四、渲染
# ============================================================================

def _fmt_group(g: dict | None) -> str:
    if not g:
        return "—"
    thin = " ⚠️样本少" if g["thin"] else ""
    return f"{g['name']} {_pct(g['rate'])}（{g['hits']}/{g['n']}）{thin}"


def render_review(rv: dict) -> str:
    """渲染成 Telegram 文案（HTML 子集）。"""
    if rv.get("status") != "ok":
        return (
            "📊 <b>每日复盘</b>\n"
            f"暂无可复盘的样本。\n"
            f"<i>{rv.get('hint', '')}</i>"
        )

    b = rv["baselines"]
    lines = [
        f"📊 <b>每日复盘</b> · 近 {rv['window_days']} 天",
        "",
        f"✅ 命中 <b>{rv['hits']}/{rv['settled']}</b> · <b>{_pct(rv['accuracy'])}</b>",
        f"📉 基线：永远猜主胜 {_pct(b['always_home'])} · 猜最高频 {_pct(b['best_freq'])} · 瞎猜 33.3%",
        f"📈 相对基线 <b>{rv['edge_vs_home'] * 100:+.1f}pt</b>（vs 主胜）"
        f" / {rv['edge_vs_freq'] * 100:+.1f}pt（vs 高频）",
        "",
        "<b>哪类准 / 哪类不准</b>",
    ]
    if rv["best_group"] and rv["worst_group"]:
        lines.append(f"· 最好：{_fmt_group(rv['best_group'])}")
        lines.append(f"· 最差：{_fmt_group(rv['worst_group'])}")
    else:
        lines.append("· 分组还不足两个，暂不比较优劣")
    lines.extend(["", "<b>按信心档</b>"])
    for g in rv["by_confidence"]:
        lines.append(f"· {_fmt_group(g)}")

    lg = [g for g in rv["by_league"] if not g["thin"]][:4]
    if lg:
        lines.append("")
        lines.append("<b>按联赛</b>")
        for g in lg:
            lines.append(f"· {_fmt_group(g)}")

    src = rv["by_source"]
    if src:
        lines.append("")
        lines.append("<b>按口径</b>")
        for g in src:
            lines.append(f"· {_fmt_group(g)}")

    lines.append("")
    if rv["confidence_meaningful"] is True:
        lines.append("✅ 高信心档确实比低信心档准 —— 模型有自知之明")
    elif rv["confidence_meaningful"] is False:
        lines.append("⚠️ 高信心档并不比低信心档准 —— 概率标定可能有问题")
    else:
        lines.append("ℹ️ 信心档样本还不足，暂不判断标定好坏")

    lines.append("<i>样本不足 8 场的分组只计数不报率，避免假规律</i>")
    return "\n".join(lines)
