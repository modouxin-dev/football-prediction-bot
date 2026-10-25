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

# 归因窗口（分组统计 / Brier 分解）的默认天数。
#
# 为什么不是 7 天：实测线上 7 天只结算约 40 场，而复盘要按
# 联赛 / 方向 / 信心档 / 口径 四个维度分组——40 场切成 6 个联赛后
# 每组不足 8 场，全部标 thin，于是"样本不足"天天报、且**永远自愈不了**：
# 窗口写死 7 天，样本量上限就被钉死，攒再多历史数据也进不来。
# 放到 30 天（约 160 场）后分组才有统计意义。
DEFAULT_ATTRIBUTION_DAYS = 30

# 近期状态窗口：只看时效，不参与归因。
# 归因要样本量、近期要时效性，两者不可兼得，因此拆成两层各司其职。
DEFAULT_RECENT_DAYS = 7

_OUTCOMES = ("主胜", "平局", "客胜")


def _outcome(hs: int, as_: int) -> str:
    """真实赛果方向。"""
    if hs > as_:
        return "主胜"
    if hs < as_:
        return "客胜"
    return "平局"


# ============================================================================
# 一·五、Brier 的 Murphy 分解（REL − RES + UNC）
# ============================================================================
# 命中率只回答「猜对几场」，答不了「失分失在哪」。Murphy 分解把多分类
# Brier 拆成三项，让每日复盘能直接指出今天该修什么：
#
#   REL（可靠性 / calibration）
#       预测说 60% 的那些场次，实际是不是真有 60% 发生。
#       **越低越好**，只靠概率重标定就能降（代价极低）。
#   RES（区分度 / resolution）
#       预测有没有把「高概率场次」和「低概率场次」真正分开。
#       **越高越好**，只能靠引入新信息提升（加特征、换信号源）。
#   UNC（不确定性）
#       赛果本身的随机性，与模型无关，是这块的天花板。
#       平局多的联赛 UNC 天然更高。
#
# 恒等式：Brier_binned = REL − RES + UNC
#
# 为什么是 Brier_binned 而不是原始 Brier（重要，别用错口径）：
#   分解要求箱内预测概率恒定。把箱内每个样本的预测换成箱内均值 p̄_k 后，
#   箱内离差 δ=0，三项之和**精确等于**分箱口径的 Brier，不再是近似。
#   原始 Brier 与它的差（within_bin）是模型在箱内的细粒度区分，
#   属于「多说出来的信息」，不是失分，因此不进 REL/RES/UNC。
#
#   诊断归因看 brier/REL/RES/UNC；和历史数字横向对比看 brier_raw。
#   两者混用会导致「三项加起来对不上」的假故障。
#
# 为什么按 top 概率分箱而不是三项各分：
#   三项各自分箱会得到互相重叠的样本，REL/RES 无法加总回 Brier。
MURPHY_BINS = 10
MURPHY_MIN_PER_BIN = 5     # 箱内样本太少时该箱不参与（频率本身不可信）


def murphy_decomposition(items: list[dict],
                         bins: int = MURPHY_BINS) -> dict | None:
    """对一批已结算预测做 Brier 的 Murphy 分解。

    items 需含 probs=(主胜,平,客胜) 与 actual ∈ _OUTCOMES。
    样本不足（为空 / 有效箱 < 2）返回 None——不猜、不补零。
    """
    if not items:
        return None
    n_bins = max(2, int(bins))
    buckets: list[list[dict]] = [[] for _ in range(n_bins)]
    for it in items:
        top = max(it["probs"])
        # top 可能略微越界（浮点/脏数据），夹住避免 IndexError
        idx = min(int(top * n_bins), n_bins - 1)
        idx = max(0, idx)
        buckets[idx].append(it)

    n_total = 0
    rel = 0.0
    res = 0.0
    used_bins = 0
    # 全局实际频率 ō_j
    global_freq = {
        name: sum(1 for it in items if it["actual"] == name) / len(items)
        for name in _OUTCOMES
    }
    per_bin: list[dict] = []
    for b in buckets:
        n_k = len(b)
        if n_k < MURPHY_MIN_PER_BIN:
            continue
        used_bins += 1
        n_total += n_k
        # 箱内平均预测 p̄_kj 与实际频率 ō_kj
        mean_pred = [sum(it["probs"][j] for it in b) / n_k for j in range(3)]
        obs_freq = [sum(1 for it in b if it["actual"] == _OUTCOMES[j]) / n_k
                    for j in range(3)]
        rel += n_k * sum((mean_pred[j] - obs_freq[j]) ** 2 for j in range(3))
        res += n_k * sum((obs_freq[j] - global_freq[_OUTCOMES[j]]) ** 2
                         for j in range(3))
        # 该箱的「说得多准」：平均 top 概率 vs 实际命中率。
        # 正 = 预测比实际更自信（过度自信），负 = 低估。
        mean_top = sum(max(it["probs"]) for it in b) / n_k
        hit_rate = sum(1 for it in b if it["hit"]) / n_k
        per_bin.append({
            "n": n_k,
            "mean_pred": mean_pred,
            "obs_freq": obs_freq,
            "mean_top": mean_top,
            "hit_rate": hit_rate,
            "bias": mean_top - hit_rate,
        })
    if used_bins < 2 or n_total == 0:
        return None

    rel /= n_total
    res /= n_total
    unc = sum(global_freq[name] * (1 - global_freq[name]) for name in _OUTCOMES)

    # 恒等式成立的对象是**分箱后**的 Brier，不是原始 Brier。
    # 把箱内每个样本的预测替换成箱内均值 p̄_k 后，箱内 δ=0，
    # 于是 Brier_binned = REL − RES + UNC 精确成立（不再是近似）。
    binned_brier = rel - res + unc
    raw_brier = sum(
        sum((it["probs"][j] - (1.0 if it["actual"] == _OUTCOMES[j] else 0.0)) ** 2
            for j in range(3))
        for it in items
    ) / len(items)

    return {
        "n": len(items),
        "bins_used": used_bins,
        # 分箱口径：三项之和恰等于它，用于归因
        "brier": binned_brier,
        # 原始口径：逐场概率直接算，用于横向对比（与 backtest.brier 同口径）
        "brier_raw": raw_brier,
        "reliability": rel,
        "resolution": res,
        "uncertainty": unc,
        # 箱内变异：模型在箱内的细粒度区分。这部分**不是失分**，
        # 是模型比「箱内均值」多说出来的信息，因此不计入 REL/RES/UNC。
        "within_bin": raw_brier - binned_brier,
        "global_freq": global_freq,
        "per_bin": per_bin,
    }


def murphy_verdict(dec: dict | None,
                   rel_threshold: float = 0.02) -> str | None:
    """把分解结果翻译成一句可执行的结论。

    返回 None 表示样本不足，不下结论。
    """
    if not dec:
        return None
    rel, res = dec["reliability"], dec["resolution"]
    if rel >= rel_threshold:
        return ("⚠️ 失分主要来自<b>标定偏差</b>（REL 偏高）——"
                "概率没说真话，重标定即可改善，代价最低")
    if res <= 0.001:
        return ("⚠️ 失分主要来自<b>区分度不足</b>（RES 过低）——"
                "概率没把强弱分开，只能靠引入新信息改善")
    return "✅ 标定与区分度都在正常区间，暂无单点瓶颈"


def _hit_rate(hits: int, total: int, min_total: int = 1) -> float | None:
    """命中率；样本不足返回 None（不报数字比报错觉的数字更诚实）。

    Args:
        hits: 命中场次数。
        total: 总场次数。
        min_total: 报率所需的最小样本量，默认 1（即只要有样本就报）。
            分组归因必须显式传 MIN_SAMPLE——3 场全中显示 100% 会误导人
            去相信一个根本不存在的规律。

    Returns:
        float 或 None。None 表示"样本不足，不下结论"。
    """
    # 原实现只在 total=0 时返回 None，与 docstring 承诺的"样本不足返回 None"
    # 不符——MIN_SAMPLE 的判定根本没有生效，导致 thin 分组照样报出
    # 100%（3/3）这种假规律。这里补齐。
    if total < min_total:
        return None
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
            # 显式传 MIN_SAMPLE：thin 分组的 rate 必须是 None，
            # 否则 _fmt_group 会把 100%（3/3）当成真规律报出去。
            "rate": _hit_rate(hits, len(group), MIN_SAMPLE),
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

def daily_review(db_path: str, since_days: int = 7,
                 recent_days: int | None = None) -> dict:
    """生成复盘报告。

    Args:
        db_path: SQLite 路径。
        since_days: **归因窗口**天数，决定分组统计、Brier 分解、标定判定
            用哪些样本。<=0 表示不限时间（全量）。
        recent_days: **近期窗口**天数，只用于报"最近这几天手感如何"。
            None（默认）= 不额外计算。近期窗口只报数字、不参与归因，
            因为它的样本量不足以支撑任何分组结论。

    Returns:
        dict，status 为 "empty"（无已结算样本）或 "ok"。
        另含 "recent" 键：近 recent_days 天的 {days, settled, hits, accuracy}，
        样本不足 8 场时 accuracy 为 None（不报率，避免假规律）。
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

    # 近期状态：只报数字，不做任何分组归因。
    # 样本不足 MIN_SAMPLE 时 accuracy 为 None——宁可不报，也不用 3 场 100%
    # 去暗示一个根本不存在的规律。
    recent: dict | None = None
    if recent_days:
        r_items = _load(db_path, recent_days)
        if r_items:
            r_hits = sum(1 for it in r_items if it["hit"])
            recent = {
                "days": recent_days,
                "settled": len(r_items),
                "hits": r_hits,
                # 显式传 MIN_SAMPLE：近期窗口必然样本不足（7 天约 40 场），
                # 但报一个 3 场 100% 的"近期命中率"会让人误判手感。
                "accuracy": _hit_rate(r_hits, len(r_items), MIN_SAMPLE),
            }

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
        "recent": recent,
        # Brier 分解：回答「失分失在哪」，而不是只报命中率
        "murphy": murphy_decomposition(items),
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
    """渲染一个分组。样本不足时**只报数不报率**。

    原实现把 rate 和「⚠️样本少」一起显示——问题是 100%（3/3）这个数字
    本身就足够误导，加个警告标记并不能抵消它。不足 MIN_SAMPLE 时
    直接不显示百分比，只说「n 场（不足 8 场）」。
    """
    if not g:
        return "—"
    if g.get("thin") or g.get("rate") is None:
        return f"{g['name']} {g['n']} 场（不足 {MIN_SAMPLE} 场，不报率）"
    return f"{g['name']} {_pct(g['rate'])}（{g['hits']}/{g['n']}）"


def render_review(rv: dict) -> str:
    """渲染成 Telegram 文案（HTML 子集）。"""
    if rv.get("status") != "ok":
        return (
            "📊 <b>每日复盘</b>\n"
            f"暂无可复盘的样本。\n"
            f"<i>{rv.get('hint', '')}</i>"
        )

    b = rv["baselines"]
    wd = rv['window_days']
    win = "全量" if (not wd or wd <= 0) else f"近 {wd} 天"
    lines = [
        f"📊 <b>每日复盘</b> · 归因 {win}（{rv['settled']} 场）",
        "",
        f"✅ 命中 <b>{rv['hits']}/{rv['settled']}</b> · <b>{_pct(rv['accuracy'])}</b>",
    ]
    # 近期状态单独一行：归因要样本量、近期要时效性，两者不可兼得，
    # 分开报才不会让人误以为「最近不准 = 模型不行」。
    rc = rv.get("recent")
    if rc:
        if rc["accuracy"] is None:
            lines.append(
                f"🕐 近 {rc['days']} 天 {rc['settled']} 场（不足 {MIN_SAMPLE} 场，暂不报率）"
            )
        else:
            lines.append(
                f"🕐 近 {rc['days']} 天 {rc['hits']}/{rc['settled']} · "
                f"<b>{_pct(rc['accuracy'])}</b>"
            )
    lines.extend([
        f"📉 基线：永远猜主胜 {_pct(b['always_home'])} · 猜最高频 {_pct(b['best_freq'])} · 瞎猜 33.3%",
        f"📈 相对基线 <b>{rv['edge_vs_home'] * 100:+.1f}pt</b>（vs 主胜）"
        f" / {rv['edge_vs_freq'] * 100:+.1f}pt（vs 高频）",
        "",
        "<b>哪类准 / 哪类不准</b>",
    ])
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

    # Brier 分解：命中率答不了「为什么不准」，这块给出可执行的归因
    dec = rv.get("murphy")
    if dec:
        lines.append("")
        lines.append("<b>失分归因（Brier 分解）</b>")
        lines.append(
            f"· 标定 REL {dec['reliability']:.4f} · "
            f"区分 RES {dec['resolution']:.4f} · "
            f"固有 UNC {dec['uncertainty']:.4f}"
        )
        verdict = murphy_verdict(dec)
        if verdict:
            lines.append(f"· {verdict}")
        # 过度自信最严重的箱：直接指出「哪档概率在吹牛」
        worst = max(dec["per_bin"], key=lambda b: b["bias"], default=None)
        if worst and worst["bias"] > 0.05:
            lines.append(
                f"· 最过度自信档：预测 {worst['mean_top'] * 100:.0f}% "
                f"实际只中 {worst['hit_rate'] * 100:.0f}%"
                f"（{worst['n']} 场）"
            )

    lines.append("")
    if rv["confidence_meaningful"] is True:
        lines.append("✅ 高信心档确实比低信心档准 —— 模型有自知之明")
    elif rv["confidence_meaningful"] is False:
        lines.append("⚠️ 高信心档并不比低信心档准 —— 概率标定可能有问题")
    else:
        lines.append("ℹ️ 信心档样本还不足，暂不判断标定好坏")

    # 把「样本不足」讲具体：哪一维、差多少、多久能补上。
    # 原来那句「样本不足 8 场的分组只计数不报率」只解释规则，
    # 不告诉用户该等还是该改，天天发就成了噪音。
    thin_dims = []
    for dim, groups in (("联赛", rv["by_league"]), ("方向", rv["by_dir"]

                        if "by_dir" in rv else rv["by_direction"]),
                        ("信心档", rv["by_confidence"]), ("口径", rv["by_source"])):
        t = [g for g in groups if g["thin"]]
        if t:
            need = max(MIN_SAMPLE - g["n"] for g in t)
            thin_dims.append(f"{dim}（还差 {need} 场）")
    if thin_dims:
        lines.append(f"<i>· {'、'.join(thin_dims)}样本不足 {MIN_SAMPLE} 场，"
                     f"只计数不报率，避免假规律</i>")
    else:
        lines.append(f"<i>· 各分组样本均≥{MIN_SAMPLE} 场，分组结论可用</i>")
    return "\n".join(lines)
