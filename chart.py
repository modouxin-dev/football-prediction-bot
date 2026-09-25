"""图表生成（Matplotlib，无界面模式，深色配色，输出 PNG 字节流）。

设计约束：
- 必须使用 Agg 后端（容器内无显示设备）
- 必须配置中文字体，否则中文渲染成方块
- 只在内存里生成（BytesIO），不落盘，不留临时文件
- 数据不足时返回 None，由上层只发文字结果，绝不生成误导性图表
"""
from __future__ import annotations

import io
import logging

import matplotlib

matplotlib.use("Agg")  # 必须在 pyplot 之前设置

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

log = logging.getLogger(__name__)

# 深色体育数据中心配色 / Dark sports data-center palette
BG = "#07111F"        # 深海军蓝背景 / deep navy background
CARD = "#101D2E"      # 玻璃卡片 / glass card
FG = "#EAF2F8"        # 主文字 / primary text
MUTED = "#7A93A8"     # 次要文字 / secondary text
ACCENT = "#39E58C"    # 荧光绿主色 / neon green primary
BLUE = "#4BA3FF"      # 电光蓝辅色 / electric blue secondary
AMBER = "#FFB547"     # 琥珀风险色 / amber risk color
WIN = "#39E58C"       # 主胜 / home win（沿用主色）
DRAW = "#FFB547"      # 平局 / draw（琥珀）
LOSE = "#4BA3FF"      # 客胜 / away win（电光蓝）
GRID = "#1B2B3F"      # 极淡网格 / faint grid

FONT_CANDIDATES = ["Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK TC", "WenQuanYi Zen Hei", "SimHei"]


def _setup_font() -> str:
    """挑一个可用的中文字体，返回实际使用的字体名（找不到时返回空串，由调用方降级）。"""
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in FONT_CANDIDATES:
        if name in available:
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False  # 负号不用 Unicode，避免方块
            return name
    # 兜底：直接注册字体文件（有些环境 ttflist 名字不一致）
    installed = {f.name for f in font_manager.fontManager.ttflist}
    log.warning("未找到常用中文字体，已安装字体：%s", sorted(installed)[:10])
    plt.rcParams["axes.unicode_minus"] = False
    return ""


FONT_IN_USE = _setup_font()


def _new_fig(width: float = 7.0, height: float = 4.2):
    fig, ax = plt.subplots(figsize=(width, height), dpi=140)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.tick_params(colors=FG)
    ax.xaxis.label.set_color(FG)
    ax.yaxis.label.set_color(FG)
    ax.title.set_color(FG)
    return fig, ax


def _finish(fig) -> bytes | None:
    """渲染成 PNG 字节流并立即释放内存（不产生任何临时文件）。"""
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
        return buf.getvalue()
    except Exception as exc:  # 图表失败不能连累文字结果
        log.warning("图表渲染失败：%s", exc)
        return None
    finally:
        plt.close(fig)


def _footer(fig, text: str) -> None:
    fig.text(0.01, 0.01, text, color="#8FA3B0", fontsize=7)


def schedule_chart(fixtures: list, tz, day_label: str = "", source: str = "") -> bytes | None:
    """赛程总览图 / Fixtures overview chart.

    同一天 → 按开赛小时分布；跨多天 → 按日期分布。
    无比赛时不生成图（返回 None），由调用方显示文字说明。
    """
    from bot_handler import BotUI  # 局部导入避免循环依赖
    from service import parse_kickoff

    buckets: dict[str, int] = {}
    for fx in fixtures:
        kickoff = (fx.get("fixture") or {}).get("date")
        dt = parse_kickoff(kickoff)
        if dt is None:
            continue
        local = dt.astimezone(tz)
        # 先按日期归集，稍后判断是否同一天（同一天则细化到小时）
        buckets.setdefault(local.strftime("%Y-%m-%d"), []).append(local.hour)

    if not buckets:
        return None

    days = sorted(buckets)
    if len(days) == 1:
        # 单日：按小时分布 / Single day → hourly distribution
        hours = sorted(set(buckets[days[0]]))
        labels = [f"{h:02d}:00" for h in hours]
        values = [buckets[days[0]].count(h) for h in hours]
        xlabel = "开赛时间（本地时区）"
    else:
        labels = [d[5:] for d in days]  # MM-DD
        values = [len(buckets[d]) for d in days]
        xlabel = "比赛日期"

    if not any(v > 0 for v in values):
        return None

    fig, ax = _new_fig(7.4, 3.6)
    bars = ax.bar(labels, values, color=ACCENT, width=0.6)
    ax.set_ylabel("比赛场次")
    ax.set_xlabel(xlabel)
    ax.grid(axis="y", color=GRID, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + max(values) * 0.04,
                str(value), ha="center", color=FG, fontweight="bold")
    ax.set_ylim(0, max(values) * 1.3 + 0.6)
    title = f"赛程分布 · {day_label}" if day_label else "赛程分布"
    ax.set_title(title, pad=12)
    _footer(fig, f"共 {sum(values)} 场 · {BotUI.tz_label(tz)}" + (f" · {source}" if source else ""))
    return _finish(fig)


def _card_frame(fig, left=0.045, right=0.955, bottom=0.06, top=0.93):
    """玻璃卡片底衬 / Glass card backdrop（圆角矩形 + 低透明度蓝晕）。

    返回卡片 axes 与圆角矩形 patch，供上层叠放内容。
    """
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    rect = FancyBboxPatch(
        (left, bottom), right - left, top - bottom,
        boxstyle="round,pad=0,rounding_size=0.055",
        linewidth=1.2, edgecolor="#1E3555", facecolor=CARD,
        transform=ax.transAxes, zorder=0,
    )
    ax.add_patch(rect)
    return ax


def prob_ring(prediction, tz, model_version: str = "poisson-v0.1") -> bytes | None:
    """胜平负概率环 / Win-draw-loss probability ring.

    中心显示最大概率，外圈三段依次为主胜 / 平局 / 客胜；
    数字不抢戏，突出"一个结论"而非三个大数字。
    """
    from bot_handler import BotUI

    a = prediction.analysis
    if not a:
        return None
    values = [a["win_prob"] * 100, a["draw_prob"] * 100, a["loss_prob"] * 100]
    if not any(v > 0 for v in values):
        return None

    fig = plt.figure(figsize=(7.2, 3.9), dpi=150)
    fig.patch.set_facecolor(BG)
    _card_frame(fig)

    ax = fig.add_axes([0.06, 0.16, 0.42, 0.62])
    ax.set_facecolor(CARD)
    colors = [WIN, DRAW, LOSE]
    wedges, _ = ax.pie(
        values, colors=colors, startangle=90, counterclock=False,
        wedgeprops=dict(width=0.30, edgecolor=CARD, linewidth=2.5),
    )
    best = max(values)
    ax.text(0, 0.10, f"{best:.0f}%", ha="center", va="center",
            color=FG, fontsize=25, fontweight="bold")
    ax.text(0, -0.22, "胜率峰值", ha="center", va="center", color=MUTED, fontsize=8.5)
    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-1.25, 1.25)
    ax.set_aspect("equal")

    # 右侧图例：三段概率 + 标签
    tx = fig.add_axes([0.55, 0.16, 0.38, 0.62])
    tx.set_axis_off()
    tx.set_xlim(0, 1)
    tx.set_ylim(0, 1)
    labels = ["主胜", "平局", "客胜"]
    for i, (label, value, color) in enumerate(zip(labels, values, colors)):
        y = 0.78 - i * 0.30
        tx.add_patch(FancyBboxPatch(
            (0.02, y - 0.055), 0.055, 0.055,
            boxstyle="round,pad=0,rounding_size=0.02",
            facecolor=color, edgecolor="none", transform=tx.transAxes,
        ))
        tx.text(0.13, y - 0.028, label, color=MUTED, fontsize=11, va="center")
        tx.text(0.98, y - 0.028, f"{value:.1f}%", color=FG, fontsize=15,
                fontweight="bold", va="center", ha="right")

    title_ax = fig.add_axes([0.06, 0.80, 0.88, 0.12])
    title_ax.set_axis_off()
    title_ax.text(0, 0.5, "WIN / DRAW / LOSS", color=ACCENT, fontsize=9.5,
                  fontweight="bold", va="center")
    title_ax.text(1, 0.5, f"MODEL CERTAINTY {best:.0f}%", color=MUTED,
                  fontsize=8.5, va="center", ha="right")
    _footer(fig, f"{model_version} · {BotUI.fmt_time(prediction.created_at, tz, '%Y-%m-%d %H:%M')} · {BotUI.tz_label(tz)}")
    return _finish(fig)


def match_card(prediction, tz, level: dict, league_name: str = "",
               matchday: str = "", source: str = "",
               model_version: str = "poisson-v0.1") -> bytes | None:
    """高级版比赛主卡 / Premium match card.

    顶部赛事标签 + 队名 VS + 三段概率 + AI 结论 + 信心徽章 + xG。
    """
    from bot_handler import BotUI

    a = prediction.analysis
    if not a:
        return None
    home, away = str(prediction.home), str(prediction.away)
    probs = [a["win_prob"] * 100, a["draw_prob"] * 100, a["loss_prob"] * 100]

    fig = plt.figure(figsize=(7.4, 5.0), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = _card_frame(fig, top=0.955, bottom=0.04)

    # 顶部：联赛名 + 赛事标签 / header: league + status badge
    badge = "PREDICTION"
    ax.text(0.075, 0.905, (league_name or "MATCH").upper(), color=MUTED,
            fontsize=9, fontweight="bold", va="center")
    ax.text(0.925, 0.905, badge, color=ACCENT, fontsize=9, fontweight="bold",
            va="center", ha="right")
    if matchday:
        ax.text(0.925, 0.862, matchday.upper(), color=MUTED, fontsize=8,
                va="center", ha="right")

    kickoff = BotUI.fmt_time(prediction.created_at, tz, "%d %b %Y · %H:%M")
    ax.text(0.075, 0.855, kickoff.upper(), color=MUTED, fontsize=8.5, va="center")

    # 分隔线 / divider
    ax.plot([0.075, 0.925], [0.815, 0.815], color="#1E3555", linewidth=1.1)

    # 队名 VS / teams
    ax.text(0.30, 0.715, home, color=FG, fontsize=17, fontweight="bold",
            ha="center", va="center")
    ax.text(0.70, 0.715, away, color=FG, fontsize=17, fontweight="bold",
            ha="center", va="center")
    ax.text(0.50, 0.715, "VS", color=MUTED, fontsize=11, ha="center", va="center")

    # 三段概率 / probabilities
    for i, (value, label, color) in enumerate(zip(probs, ["主胜", "平", "客胜"], [WIN, DRAW, LOSE])):
        x = 0.25 + i * 0.25
        ax.text(x, 0.60, f"{value:.0f}%", color=color, fontsize=20,
                fontweight="bold", ha="center", va="center")
        ax.text(x, 0.545, label, color=MUTED, fontsize=9, ha="center", va="center")

    ax.plot([0.075, 0.925], [0.505, 0.505], color="#1E3555", linewidth=1.1)

    # AI 结论 / AI prediction
    ax.text(0.075, 0.455, "AI PREDICTION", color=ACCENT, fontsize=8.5,
            fontweight="bold", va="center")
    top_name = ["主胜", "平局", "客胜"][probs.index(max(probs))]
    score = a.get("scoreline") or ""
    verdict = f"{top_name}" + (f" · 预计比分 {score}" if score else "")
    ax.text(0.075, 0.395, verdict, color=FG, fontsize=13.5, fontweight="bold", va="center")

    # 信心徽章 / confidence badge
    lname = (level or {}).get("name", "")
    lkey = (level or {}).get("key", "")
    bcolor = {"high": WIN, "medium": AMBER, "low": LOSE}.get(lkey, AMBER)
    ax.add_patch(FancyBboxPatch(
        (0.075, 0.30), 0.30, 0.062,
        boxstyle="round,pad=0,rounding_size=0.03",
        facecolor=bcolor, alpha=0.16, edgecolor=bcolor, linewidth=1.1,
        transform=ax.transAxes,
    ))
    ax.text(0.225, 0.331, f"{lname} CONFIDENCE".strip(), color=bcolor,
            fontsize=9, fontweight="bold", ha="center", va="center")

    # xG / expected goals
    hg = a.get("home_xg") or a.get("lambda_home")
    ag = a.get("away_xg") or a.get("lambda_away")
    if hg is not None and ag is not None:
        ax.text(0.075, 0.225, f"xG  {float(hg):.2f}", color=BLUE, fontsize=11, va="center")
        ax.text(0.925, 0.225, f"xG  {float(ag):.2f}", color=BLUE, fontsize=11,
                va="center", ha="right")

    ax.plot([0.075, 0.925], [0.175, 0.175], color="#1E3555", linewidth=1.1)
    _footer(fig, f"{model_version} · {source or 'API'} · {BotUI.tz_label(tz)}")
    return _finish(fig)


def empty_state(title: str = "NO FIXTURE IN THIS WINDOW",
                subtitle: str = "当前时间范围暂无比赛") -> bytes | None:
    """高级空状态图 / Premium empty state（空数据时不再是一片空白）。"""
    fig = plt.figure(figsize=(7.0, 2.8), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = _card_frame(fig, top=0.94, bottom=0.06)
    ax.text(0.5, 0.62, title, color=MUTED, fontsize=13, fontweight="bold",
            ha="center", va="center")
    ax.text(0.5, 0.40, subtitle, color=FG, fontsize=11, ha="center", va="center")
    # 装饰：一道荧光绿细线
    ax.plot([0.42, 0.58], [0.26, 0.26], color=ACCENT, linewidth=2.2)
    return _finish(fig)


def prob_chart(prediction, tz, model_version: str = "poisson-v0.1") -> bytes | None:
    """预测概率柱状图（主胜 / 平局 / 客胜）。"""
    a = prediction.analysis
    if not a:
        return None
    labels = ["主胜", "平局", "客胜"]
    values = [a["win_prob"] * 100, a["draw_prob"] * 100, a["loss_prob"] * 100]
    if not any(v > 0 for v in values):  # 数据不足不画误导性图
        return None

    fig, ax = _new_fig()
    bars = ax.bar(labels, values, color=[WIN, DRAW, LOSE], width=0.55)
    ax.set_ylim(0, max(values) * 1.25 + 5)
    ax.set_ylabel("概率 (%)")
    ax.grid(axis="y", color=GRID, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    for bar, value in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + max(values) * 0.03,
            f"{value:.1f}%",
            ha="center",
            color=FG,
            fontweight="bold",
        )
    ax.set_title(f"{prediction.home} vs {prediction.away} · 胜平负概率", pad=14)
    from bot_handler import BotUI

    _footer(fig, f"模型 {model_version} · {BotUI.fmt_time(prediction.created_at, tz, '%Y-%m-%d %H:%M')}")
    return _finish(fig)


def form_chart(report: dict, tz) -> bytes | None:
    """近期战绩对比图（主队 vs 客队，胜/平/负计数）。"""
    home, away = report["home_form"], report["away_form"]
    if not home["played"] and not away["played"]:
        return None
    categories = ["胜", "平", "负"]
    x = range(len(categories))
    width = 0.38
    fig, ax = _new_fig()
    h_vals = [home["win"], home["draw"], home["lose"]]
    a_vals = [away["win"], away["draw"], away["lose"]]
    ax.bar([i - width / 2 for i in x], h_vals, width, label=report["home"], color=ACCENT)
    ax.bar([i + width / 2 for i in x], a_vals, width, label=report["away"], color="#AF7AC5")
    ax.set_xticks(list(x))
    ax.set_xticklabels(categories)
    ax.set_ylabel("场次")
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=8)
    ax.grid(axis="y", color=GRID, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    ax.set_title(f"近 5 场战绩对比：{report['home']} vs {report['away']}", pad=14)
    from bot_handler import BotUI

    _footer(fig, f"数据更新 {BotUI.fmt_time(report['created_at'], tz, '%Y-%m-%d %H:%M')}")
    return _finish(fig)


def goals_chart(report: dict, tz) -> bytes | None:
    """场均进失球对比图。"""
    home, away = report["home_form"], report["away_form"]
    if home["avg_for"] is None and away["avg_for"] is None:
        return None

    def pick(form, key, fallback=None):
        value = form[key]
        return value if value is not None else (fallback or 0.0)

    # 近期数据不足时退用联赛均值，并在脚注说明，避免画出全 0 的误导图
    labels = ["场均进球", "场均失球"]
    h_vals = [pick(home, "avg_for"), pick(home, "avg_against")]
    a_vals = [pick(away, "avg_for"), pick(away, "avg_against")]
    x = range(len(labels))
    width = 0.38
    fig, ax = _new_fig()
    ax.bar([i - width / 2 for i in x], h_vals, width, label=report["home"], color=WIN)
    ax.bar([i + width / 2 for i in x], a_vals, width, label=report["away"], color=LOSE)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("球数")
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=8)
    ax.grid(axis="y", color=GRID, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    ax.set_title(f"进失球对比：{report['home']} vs {report['away']}", pad=14)
    note = "近期数据不足，已按联赛平均估算" if not home["played"] or not away["played"] else ""
    from bot_handler import BotUI

    _footer(fig, f"数据更新 {BotUI.fmt_time(report['created_at'], tz, '%Y-%m-%d %H:%M')} {note}")
    return _finish(fig)


def h2h_chart(report: dict, tz) -> bytes | None:
    """历史交锋图（主队胜 / 平 / 客队胜）。"""
    h2h = report["h2h"]
    if not h2h["played"]:
        return None
    labels = [f"{report['home']}胜", "平局", f"{report['away']}胜"]
    values = [h2h["win"], h2h["draw"], h2h["lose"]]
    fig, ax = _new_fig(width=6.4, height=4.0)
    bars = ax.bar(labels, values, color=[WIN, DRAW, LOSE], width=0.55)
    ax.set_ylabel("场次")
    ax.grid(axis="y", color=GRID, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.08, str(value), ha="center", color=FG, fontweight="bold")
    ax.set_title(f"历史交锋（近 {h2h['played']} 次，{report['home']} 视角）", pad=14)
    from bot_handler import BotUI

    _footer(fig, f"进球 {h2h['goals_for']}-{h2h['goals_against']} · {BotUI.fmt_time(report['created_at'], tz, '%Y-%m-%d')}")
    return _finish(fig)


def brand_banner(title: str = "FOOTBALL QUANT",
                 subtitle: str = "足球量化预测 · 数据驱动赛事洞察",
                 tagline: str = "Poisson Model") -> bytes | None:
    """品牌头图：深色卡片 + 品牌名 + 副标题，纯内存生成 PNG。

    用于 /start 欢迎页。生成失败返回 None，调用方降级为纯文字，不影响主流程。
    """
    try:
        fig = plt.figure(figsize=(7.0, 2.6), dpi=140)
        fig.patch.set_facecolor(BG)

        # 顶部装饰条：蓝 → 黄 → 绿 → 红（品牌四色，依次呼应 数据/平局/胜/负 语义）
        strip = (ACCENT, DRAW, WIN, LOSE)
        width = 0.21
        for i, color in enumerate(strip):
            fig.add_axes([0.06 + i * width, 0.90, width, 0.035]).set_facecolor(color)

        ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
        ax.set_axis_off()
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

        # 品牌名：无中文字体时英文仍可正常渲染，中文会降级但不影响布局
        ax.text(0.06, 0.62, title, fontsize=27, fontweight="bold",
                color=FG, va="center", ha="left")
        ax.text(0.06, 0.38, subtitle, fontsize=12.5, color=ACCENT, va="center", ha="left")
        ax.text(0.06, 0.17, tagline, fontsize=9.5, color=GRID if False else "#8A9AA8",
                va="center", ha="left", family="monospace")

        # 右侧装饰：同心圆靶心（外→内：蓝 黄 绿，中心红点，与顶部四色呼应）
        for r, alpha, color in ((0.085, 0.30, ACCENT), (0.055, 0.55, DRAW), (0.028, 0.85, WIN)):
            ax.add_patch(plt.Circle((0.855, 0.50), r, color=color, alpha=alpha, fill=True))
        ax.add_patch(plt.Circle((0.855, 0.50), 0.010, color=LOSE, alpha=1.0, fill=True))

        return _finish(fig)
    except Exception:  # 图表永远不能拖垮主流程
        log.exception("生成品牌头图失败")
        return None
