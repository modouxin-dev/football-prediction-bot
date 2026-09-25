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

log = logging.getLogger(__name__)

# Telegram 深色背景下的配色
BG = "#17212B"
FG = "#E8E8E8"
ACCENT = "#5DADE2"
WIN = "#2ECC71"
DRAW = "#F5B041"
LOSE = "#E74C3C"
GRID = "#2C3A47"

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
