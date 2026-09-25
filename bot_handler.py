"""消息模板与按钮键盘（只负责排版，不做网络请求）。所有来自数据源的文本都会做 HTML 转义。"""
from __future__ import annotations

import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from analyzer import OUTCOMES, overround
from service import MODEL_VERSION, parse_kickoff

SEP = "━━━━━━━━━━━━━━━━━━"
OUTCOME_LABEL = {"home": "主胜", "draw": "平局", "away": "客胜"}
DISCLAIMER = "⚠️ 模型仅基于进球数据估算，不构成投注建议。"
VALUE_FLAG = 0.05  # 价值偏差超过此值时打 🚀 标记
VALUE_HIGH = 0.07
VALUE_LOW = 0.03

# 视图标签页：(callback 前缀, 按钮文字)
TABS = (("home", "📈 预测"), ("deep", "🔍 深度分析"), ("h2h", "📊 历史交锋"), ("odds", "💰 赔率对比"))

# 主菜单：(callback key, 按钮文字) —— Inline 与底部 Reply 键盘共用同一份定义
MENU_ITEMS = (
    ("fixtures", "📅 今日赛程"),
    ("predict", "⚽ 比赛预测"),
    ("analysis", "📊 深度分析"),
    ("standings", "🏆 联赛排名"),
    ("refresh", "🔄 刷新数据"),
    ("help", "ℹ️ 使用帮助"),
)

# API-Football 的比赛状态缩写 → 中文
STATUS_TEXT = {
    "NS": "未开始",
    "TBD": "时间待定",
    "1H": "上半场",
    "HT": "中场休息",
    "2H": "下半场",
    "ET": "加时赛",
    "BT": "加时休息",
    "P": "点球大战",
    "PEN": "点球大战",
    "FT": "已完场",
    "AET": "加时完场",
    "PST": "已推迟",
    "CANC": "已取消",
    "ABD": "已中止",
    "SUSP": "已中断",
    "INT": "已中断",
    "LIVE": "进行中",
    "WO": "弃赛",
}

NO_DATA = "暂无可靠数据，不参与本次分析（缺什么就说明缺什么，不做填充）。"


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_summary(row: dict | None) -> dict | None:
    """积分榜一行 → 排名 / 积分 / 胜平负 / 场均进失球。缺字段时相应项为 None。"""
    if not row:
        return None
    all_, home, away = row.get("all") or {}, row.get("home") or {}, row.get("away") or {}
    played = _num(all_.get("played")) or (_num(home.get("played")) + _num(away.get("played")))
    gf = _num((all_.get("goals") or {}).get("for")) or (
        _num((home.get("goals") or {}).get("for")) + _num((away.get("goals") or {}).get("for"))
    )
    ga = _num((all_.get("goals") or {}).get("against")) or (
        _num((home.get("goals") or {}).get("against")) + _num((away.get("goals") or {}).get("against"))
    )
    win = _num(all_.get("win")) or (_num(home.get("win")) + _num(away.get("win")))
    draw = _num(all_.get("draw")) or (_num(home.get("draw")) + _num(away.get("draw")))
    lose = _num(all_.get("lose")) or (_num(home.get("lose")) + _num(away.get("lose")))
    return {
        "rank": row.get("rank"),
        "points": row.get("points"),
        "played": int(played),
        "win": int(win),
        "draw": int(draw),
        "lose": int(lose),
        "avg_for": gf / played if played else None,
        "avg_against": ga / played if played else None,
    }


def _form_mark(result: str) -> str:
    return {"win": "🟢", "draw": "🟡", "lose": "🔴"}.get(result, "⚪")


def _form_line(form: dict) -> str:
    """近期战绩一行摘要（没有足够样本时明确说没有数据）。"""
    if not form["played"]:
        return NO_DATA
    avg_for = f"{form['avg_for']:.1f}" if form["avg_for"] is not None else "-"
    avg_against = f"{form['avg_against']:.1f}" if form["avg_against"] is not None else "-"
    marks = " ".join(_form_mark(m["result"]) + m["score"] for m in form["matches"][:5])
    return f"{form['win']}胜 {form['draw']}平 {form['lose']}负 · 进 {form['goals_for']} / 失 {form['goals_against']} · 场均 {avg_for} / {avg_against}\n   {marks}"


def _row_line(row: dict | None) -> str:
    if not row:
        return NO_DATA
    summary = _row_summary(row)
    if not summary or not summary["played"]:
        return NO_DATA
    rank = f"第 {summary['rank']} 名" if summary["rank"] else "排名未知"
    points = f"{summary['points']} 分" if summary["points"] is not None else "积分未知"
    avg_for = f"{summary['avg_for']:.2f}" if summary["avg_for"] is not None else "-"
    avg_against = f"{summary['avg_against']:.2f}" if summary["avg_against"] is not None else "-"
    return (
        f"{rank} · {points} · {summary['win']}胜{summary['draw']}平{summary['lose']}负"
        f" · 场均进 {avg_for} / 失 {avg_against}"
    )


def _factors(report: dict) -> tuple[list[str], list[str], list[str]]:
    """从数据推导有利 / 不利 / 不确定因素。没有数据就不编，直接归入不确定。"""
    pros: list[str] = []
    cons: list[str] = []
    unknowns: list[str] = []
    hs = report["model"]["home_strength"]
    aws = report["model"]["away_strength"]
    hf, af, h2h = report["home_form"], report["away_form"], report["h2h"]

    if not report["has_team_data"]:
        unknowns.append("积分榜中缺少这两支球队的数据，强弱对比不成立")
        unknowns.append("数据源未提供伤停信息")
        return pros, cons, unknowns

    if hs.attack_home > 1.1:
        pros.append(f"主队主场进攻强度 {hs.attack_home:.2f}，高于联赛平均")
    elif hs.attack_home < 0.9:
        cons.append(f"主队主场进攻强度 {hs.attack_home:.2f}，低于联赛平均")
    if aws.defense_away > 1.1:
        pros.append(f"客队客场失球偏多（防守强度 {aws.defense_away:.2f}）")
    elif aws.defense_away < 0.9:
        cons.append(f"客队客场防守稳固（防守强度 {aws.defense_away:.2f}）")

    if hf["played"] >= 3:
        if hf["win"] > hf["lose"]:
            pros.append(f"主队近期 {hf['win']}胜{hf['draw']}平{hf['lose']}负，状态较好")
        elif hf["lose"] > hf["win"]:
            cons.append(f"主队近期 {hf['win']}胜{hf['draw']}平{hf['lose']}负，状态偏低")
    if af["played"] >= 3 and af["win"] > af["lose"]:
        cons.append(f"客队近期 {af['win']}胜{af['draw']}平{af['lose']}负，来势不弱")

    if h2h["played"] >= 3:
        if h2h["win"] > h2h["lose"]:
            pros.append(f"历史交锋占优：{h2h['win']}胜{h2h['draw']}平{h2h['lose']}负")
        elif h2h["lose"] > h2h["win"]:
            cons.append(f"历史交锋处于劣势：{h2h['win']}胜{h2h['draw']}平{h2h['lose']}负")

    if hf["played"] < 3 or af["played"] < 3:
        unknowns.append("近期已完场比赛不足 3 场，状态判断不稳定")
    if h2h["played"] == 0:
        unknowns.append("暂无历史交锋记录")
    if min(hs.games_home, aws.games_away) < 5:
        unknowns.append("主/客场已赛场次不足 5 场，强度估计不稳定")
    unknowns.append("数据源未提供伤停信息（当前套餐不支持）")
    return pros, cons, unknowns


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def bar(prob: float, width: int = 10) -> str:
    """概率条：▰▰▰▰▱▱▱▱▱▱"""
    filled = max(0, min(width, round(prob * width)))
    return "▰" * filled + "▱" * (width - filled)


class BotUI:
    # ---- 通用 -----------------------------------------------------------------
    @staticmethod
    def tz_label(tz, when: datetime | None = None) -> str:
        when = (when or datetime.now(tz)).astimezone(tz)
        hours = when.utcoffset().total_seconds() / 3600
        return f"UTC{hours:+g}"

    @staticmethod
    def fmt_time(dt: datetime, tz, pattern: str = "%m-%d %H:%M") -> str:
        return dt.astimezone(tz).strftime(pattern)

    @staticmethod
    def matchup(p, tz) -> str:
        """详情页顶部的一行比赛信息。"""
        return (
            f"🏠 <b>{esc(p.home)}</b> 🆚 <b>{esc(p.away)}</b> ✈️\n"
            f"🕐 <code>{BotUI.fmt_time(p.kickoff, tz)}</code> ({BotUI.tz_label(tz, p.kickoff)})"
        )

    # ---- 键盘：标签页 + 刷新 -----------------------------------------------------
    @staticmethod
    def get_main_keyboard(fixture_id: int, active: str = "home") -> InlineKeyboardMarkup:
        buttons = [
            InlineKeyboardButton(("● " if key == active else "") + label, callback_data=f"{key}:{fixture_id}")
            for key, label in TABS
        ]
        return InlineKeyboardMarkup(
            [buttons[:2], buttons[2:], [InlineKeyboardButton("🔄 刷新赔率", callback_data=f"refresh:{fixture_id}")]]
        )

    # ---- 视图：预测主消息 ---------------------------------------------------------
    @staticmethod
    def format_prediction(p, tz) -> str:
        a = p.analysis
        probs = (("主胜", a["win_prob"]), ("平局", a["draw_prob"]), ("客胜", a["loss_prob"]))
        top = max(prob for _, prob in probs)

        title = f"🏆 <b>{esc(p.league)}</b>" + (f" · {esc(p.round_label)}" if p.round_label else "")
        when = f"🕐 <code>{BotUI.fmt_time(p.kickoff, tz)}</code> ({BotUI.tz_label(tz, p.kickoff)})"
        if p.venue:
            when += f" · 🏟 {esc(p.venue)}"

        lines = [
            title,
            SEP,
            f"🏠 <b>{esc(p.home)}</b>",
            "      🆚",
            f"✈️ <b>{esc(p.away)}</b>",
            when,
            SEP,
            "📈 <b>胜平负概率</b>",
        ]
        for label, prob in probs:
            pct = f"<b>{prob:.1%}</b>" if prob == top else f"{prob:.1%}"
            lines.append(f"{label} <code>{bar(prob)}</code> {pct}")
        lines += [
            SEP,
            f"⚽ 预期进球 <code>{a['lambda_home']:.2f} - {a['lambda_away']:.2f}</code> · 预期比分 <code>{a['best_score']}</code>",
        ]
        if p.best:
            key, o = p.best
            flag = " 🚀 <b>Value Bet</b>" if o["edge"] > VALUE_FLAG else ""
            lines.append(
                f"💰 赔率 <code>{p.odds['home']:.2f} | {p.odds['draw']:.2f} | {p.odds['away']:.2f}</code>（{p.odds['n']} 家中位数）"
            )
            lines.append(f"🔎 价值偏差 <b>{OUTCOME_LABEL[key]}</b> <code>{o['edge']:+.2%}</code>{flag}")
        else:
            lines.append("💰 赔率：暂无（本场仅提供模型概率）")
        lines += [
            SEP,
            f"🎯 <b>建议</b>：{esc(BotUI.get_strategy(p))}",
            f"💎 <b>信心</b>：{BotUI.get_confidence(p)}",
            SEP,
            DISCLAIMER,
        ]
        return "\n".join(lines)

    @staticmethod
    def get_strategy(p) -> str:
        if not p.best:
            return "仅供参考（暂无赔率）"
        key, o = p.best
        if o["edge"] >= VALUE_HIGH:
            return f"{OUTCOME_LABEL[key]}（价值较高）"
        if o["edge"] >= VALUE_LOW:
            return f"{OUTCOME_LABEL[key]}（小幅价值）"
        return "无明显价值，观望"

    @staticmethod
    def get_confidence(p) -> str:
        a = p.analysis
        top = max(a["win_prob"], a["draw_prob"], a["loss_prob"])
        stars = "⭐⭐⭐⭐⭐" if top > 0.7 else "⭐⭐⭐" if top > 0.5 else "⭐⭐"
        return stars + "（样本较少）" if p.low_sample else stars

    # ---- 视图：单场预测卡片（菜单/赛程入口） ----------------------------------------
    @staticmethod
    def confidence_text(p) -> str:
        """置信度：高 / 中 / 低，并说明依据。"""
        if not p.has_team_data:
            return "低（缺少球队数据，仅按联赛平均估算）"
        top = max(p.analysis["win_prob"], p.analysis["draw_prob"], p.analysis["loss_prob"])
        if p.low_sample:
            return "低（近期样本不足）"
        if top >= 0.6:
            return "高"
        if top >= 0.45:
            return "中"
        return "低（三项概率接近）"

    @staticmethod
    def risk_lines(p) -> list[str]:
        """风险提示：缺什么就说什么，绝不虚构概率。"""
        lines = []
        if not p.model.teams:
            lines.append("⚠️ 未取到积分榜数据，概率仅来自联赛平均基准，参考价值有限。")
        elif not p.has_team_data:
            lines.append("⚠️ 积分榜中没有这两支球队，只能按联赛平均估算。")
        elif p.low_sample:
            lines.append("⚠️ 主/客场已赛场次不足 5 场，强度估计不稳定。")
        if not p.odds:
            lines.append("⚠️ 暂无赔率数据，未做价值偏差对比。")
        lines.append("⚠️ 本结果基于历史进球数据，未考虑伤停、赛程密度与临场变数。")
        return lines

    @staticmethod
    def format_prediction_card(p, tz, model_version: str = MODEL_VERSION) -> str:
        """完整预测卡片：包含时间、模型版本、数据更新时间、置信度、完整性与风险提示。"""
        a = p.analysis
        probs = (("主胜", a["win_prob"]), ("平局", a["draw_prob"]), ("客胜", a["loss_prob"]))
        top_label, top_prob = max(probs, key=lambda kv: kv[1])

        lines = [
            "⚽ <b>比赛预测</b>",
            f"🏠 <b>{esc(p.home)}</b> 🆚 <b>{esc(p.away)}</b> ✈️",
            f"🕐 比赛时间：<code>{BotUI.fmt_time(p.kickoff, tz)}</code>（{BotUI.tz_label(tz, p.kickoff)}）",
            SEP,
            "📈 <b>预测概率</b>",
        ]
        for label, prob in probs:
            mark = " <b>← 最可能</b>" if label == top_label else ""
            lines.append(f"{label} <code>{bar(prob)}</code> <b>{prob:.1%}</b>{mark}")
        lines += [
            SEP,
            f"🎯 <b>最可能结果</b>：{esc(top_label)}（{top_prob:.1%}）· 最可能比分 <code>{esc(a['best_score'])}</code>",
            f"⚽ 预期进球 <code>{a['lambda_home']:.2f} - {a['lambda_away']:.2f}</code>",
            f"💎 <b>置信度</b>：{esc(BotUI.confidence_text(p))}",
            f"🧩 <b>数据完整性</b>：{esc(p.data_completeness)}"
            + ("" if p.has_team_data else "（未使用球队实际数据）"),
            f"🤖 <b>模型版本</b>：<code>{esc(model_version)}</code>",
            f"🕑 <b>数据更新时间</b>：<code>{BotUI.fmt_time(p.created_at, tz, '%Y-%m-%d %H:%M:%S')}</code>"
            f"（{BotUI.tz_label(tz, p.created_at)}）",
        ]
        if p.best:
            key, o = p.best
            lines.append(
                f"💰 赔率 <code>{p.odds['home']:.2f} | {p.odds['draw']:.2f} | {p.odds['away']:.2f}</code>"
                f" · 价值偏差 {OUTCOME_LABEL[key]} <code>{o['edge']:+.2%}</code>"
            )
        lines += [SEP, *BotUI.risk_lines(p), SEP, DISCLAIMER]
        return "\n".join(lines)

    @staticmethod
    def prediction_keyboard(fixture_id: int) -> InlineKeyboardMarkup:
        """预测卡片下方：复用现有 4 个标签页（可继续看深度分析/交锋/赔率）+ 图表 + 返回。"""
        buttons = [
            InlineKeyboardButton(label, callback_data=f"{key}:{fixture_id}") for key, label in TABS
        ]
        return InlineKeyboardMarkup(
            [
                buttons[:2],
                buttons[2:],
                [
                    InlineKeyboardButton("🔄 刷新赔率", callback_data=f"refresh:{fixture_id}"),
                    InlineKeyboardButton("📈 概率图表", callback_data=f"chart:prob:{fixture_id}"),
                ],
                [InlineKeyboardButton("↩️ 返回赛程", callback_data="menu:fixtures"), InlineKeyboardButton("🏠 主菜单", callback_data="menu:home")],
            ]
        )

    @staticmethod
    def analysis_keyboard(fixture_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚽ 看预测", callback_data=f"fx:{fixture_id}"),
                    InlineKeyboardButton("📈 概率图表", callback_data=f"chart:prob:{fixture_id}"),
                ],
                [
                    InlineKeyboardButton("📊 战绩图", callback_data=f"chart:form:{fixture_id}"),
                    InlineKeyboardButton("🥅 进失球图", callback_data=f"chart:goals:{fixture_id}"),
                    InlineKeyboardButton("🤝 交锋图", callback_data=f"chart:h2h:{fixture_id}"),
                ],
                [
                    InlineKeyboardButton("↩️ 返回赛程", callback_data="menu:fixtures"),
                    InlineKeyboardButton("🏠 主菜单", callback_data="menu:home"),
                ],
            ]
        )

    @staticmethod
    def chart_keyboard(fixture_id: int, kind: str) -> InlineKeyboardMarkup:
        """图片消息下方的导航：可继续切换其它图表或回到分析页。"""
        others = [k for k in ("form", "goals", "h2h") if k != kind]
        rows = [
            [
                InlineKeyboardButton(
                    {"form": "📊 战绩图", "goals": "🥅 进失球图", "h2h": "🤝 交锋图"}[k],
                    callback_data=f"chart:{k}:{fixture_id}",
                )
                for k in others
            ]
        ]
        rows.append(
            [
                InlineKeyboardButton("🔍 回到分析", callback_data=f"fa:{fixture_id}"),
                InlineKeyboardButton("🏠 主菜单", callback_data="menu:home"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    # ---- 视图：深度分析报告 ------------------------------------------------------
    @staticmethod
    def format_deep_report(report: dict, tz, model_version: str = MODEL_VERSION) -> str:
        hs = report["model"]["home_strength"]
        aws = report["model"]["away_strength"]
        analysis = report["model"]["analysis"]
        h2h = report["h2h"]
        errors = report["errors"]
        integrity = (
            "完整"
            if report["has_team_data"] and min(hs.games_home, aws.games_away) >= 5
            else "部分"
            if report["has_team_data"]
            else "无数据"
        )

        lines = [
            "📊 <b>深度分析</b>",
            f"🏠 <b>{esc(report['home'])}</b> 🆚 <b>{esc(report['away'])}</b> ✈️",
            f"🕐 <code>{BotUI.fmt_time(report['kickoff'], tz)}</code>（{BotUI.tz_label(tz, report['kickoff'])}） · {esc(report['league'])}",
            SEP,
            "🕑 <b>近期状态</b>（近 5 场已完场）",
            f"🏠 {esc(report['home'])}：{_form_line(report['home_form'])}",
            f"✈️ {esc(report['away'])}：{_form_line(report['away_form'])}",
        ]
        # 可选数据拉取失败：附真实原因，不伪装成“没有数据”
        for key, prefix in (("home_form", "主队近期"), ("away_form", "客队近期"), ("h2h", "历史交锋")):
            if errors.get(key):
                lines.append(f"   ⚠️ {prefix}数据获取失败：{esc(errors[key])}")
        lines += [
            SEP,
            "🏆 <b>联赛信息</b>",
            f"🏠 {esc(report['home'])}：{_row_line(report['home_row'])}",
            f"✈️ {esc(report['away'])}：{_row_line(report['away_row'])}",
            SEP,
            "🤝 <b>历史交锋</b>",
        ]
        if h2h["played"]:
            lines.append(
                f"近 {h2h['played']} 次交锋（{esc(report['home'])} 视角）："
                f"<b>{h2h['win']} 胜 {h2h['draw']} 平 {h2h['lose']} 负</b>"
                f" · 进球 {h2h['goals_for']}-{h2h['goals_against']}"
            )
        else:
            lines.append(NO_DATA)
        lines += [
            SEP,
            "🧮 <b>模型因素</b>",
            f"🏠 主队主场：攻击 <code>{hs.attack_home:.2f}</code> · 防守 <code>{hs.defense_home:.2f}</code>（已赛 {hs.games_home} 场）",
            f"✈️ 客队客场：攻击 <code>{aws.attack_away:.2f}</code> · 防守 <code>{aws.defense_away:.2f}</code>（已赛 {aws.games_away} 场）",
            "（1.00 = 联赛平均；攻击 &gt;1 进球更多，防守 &lt;1 失球更少即防守更好）",
            f"⚖️ 主场优势：联赛主队场均 <code>{report['model']['avg_home_goals']:.2f}</code>"
            f" / 客队场均 <code>{report['model']['avg_away_goals']:.2f}</code>",
            f"⚽ 预期进球 λ：<code>{analysis['lambda_home']:.2f} - {analysis['lambda_away']:.2f}</code>",
            f"🧩 数据完整性：<b>{integrity}</b>",
            "🩹 伤停信息：数据源未提供（当前套餐不支持）",
            f"🤖 模型版本：<code>{esc(model_version)}</code>",
            f"🕑 数据更新时间：<code>{BotUI.fmt_time(report['created_at'], tz, '%Y-%m-%d %H:%M:%S')}</code>",
            SEP,
            "🧭 <b>分析总结</b>",
        ]
        pros, cons, unknowns = _factors(report)
        lines.append("✅ 有利：" + ("；".join(pros) if pros else NO_DATA))
        lines.append("⚠️ 不利：" + ("；".join(cons) if cons else NO_DATA))
        lines.append("❓ 不确定：" + "；".join(unknowns))
        top = max(
            (("主胜", analysis["win_prob"]), ("平局", analysis["draw_prob"]), ("客胜", analysis["loss_prob"])),
            key=lambda kv: kv[1],
        )
        lines += [f"🎯 <b>模型倾向</b>：{top[0]}（{top[1]:.1%}）", SEP, DISCLAIMER]
        return "\n".join(lines)

    # ---- 视图：联赛排名 ----------------------------------------------------------
    @staticmethod
    def standings_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 刷新", callback_data="menu:refresh"),
                    InlineKeyboardButton("🏠 主菜单", callback_data="menu:home"),
                ]
            ]
        )

    @staticmethod
    def format_standings_page(rows: list[dict], tz, limit: int = 20, league_label: str = "", updated=None) -> str:
        lines = ["🏆 <b>联赛排名</b>", f"{esc(league_label)} · 时区 <code>{esc(tz.zone)}</code>"]
        if updated is not None:
            lines.append(f"🕑 数据更新时间：<code>{BotUI.fmt_time(updated, tz, '%Y-%m-%d %H:%M')}</code>")
        lines.append(SEP)
        if not rows:
            lines.append(NO_DATA)
        else:
            for i, row in enumerate(rows[:limit], start=1):
                summary = _row_summary(row)
                name = (row.get("team") or {}).get("name") or "?"
                rank = (summary or {}).get("rank") or i
                points = f"{(summary or {}).get('points')}分" if (summary or {}).get("points") is not None else "-"
                record = f"{summary['win']}-{summary['draw']}-{summary['lose']}" if summary else "-"
                avg_for = f"{summary['avg_for']:.1f}" if summary and summary["avg_for"] is not None else "-"
                avg_against = f"{summary['avg_against']:.1f}" if summary and summary["avg_against"] is not None else "-"
                lines.append(
                    f"{rank:>2}. {esc(name)}  <code>{points}</code>  <code>{record}</code>  进 {avg_for} / 失 {avg_against}"
                )
        lines += [SEP, DISCLAIMER]
        return "\n".join(lines)

    # ---- 视图：深度分析 ---------------------------------------------------------
    @staticmethod
    def format_deep_analysis(p, tz) -> str:
        a, hs, aws = p.analysis, p.home_strength, p.away_strength
        lines = [
            "🔍 <b>深度分析</b>",
            BotUI.matchup(p, tz),
            SEP,
            f"⚙️ 预期进球 λ：<code>{a['lambda_home']:.2f} - {a['lambda_away']:.2f}</code>",
            "🎯 <b>最可能比分 Top 5</b>",
        ]
        for score, prob in a["top_scores"]:
            lines.append(f"<code>{score:<5}</code> <code>{bar(prob / a['top_scores'][0][1], 8)}</code> {prob:.1%}")
        lines += [
            SEP,
            f"📊 大 2.5 球 <code>{a['over_2_5']:.1%}</code> · 小 2.5 球 <code>{1 - a['over_2_5']:.1%}</code>",
            f"🤝 双方都进球 <code>{a['btts']:.1%}</code>",
            SEP,
            "🧮 <b>球队强度</b>（1.00 = 联赛平均）",
            f"🏠 {esc(p.home)} 主场：攻击 <code>{hs.attack_home:.2f}</code> · 防守 <code>{hs.defense_home:.2f}</code>（已赛 {hs.games_home} 场）",
            f"✈️ {esc(p.away)} 客场：攻击 <code>{aws.attack_away:.2f}</code> · 防守 <code>{aws.defense_away:.2f}</code>（已赛 {aws.games_away} 场）",
            "攻击 &gt;1：进球高于平均；防守 &lt;1：失球低于平均（防守更好）。",
            SEP,
            DISCLAIMER,
        ]
        return "\n".join(lines)

    # ---- 视图：赔率对比 ---------------------------------------------------------
    @staticmethod
    def format_odds_detail(p, tz) -> str:
        title = "💰 <b>赔率对比</b>"
        if not p.odds:
            return f"{title}\n{BotUI.matchup(p, tz)}\n{SEP}\n暂无赔率数据（该场比赛可能尚未开盘）。"
        a = p.analysis
        probs = {"home": a["win_prob"], "draw": a["draw_prob"], "away": a["loss_prob"]}
        table = [f"{'博彩公司':<12}{'主':>6}{'平':>6}{'客':>6}"]
        for row in sorted(p.bookmakers, key=lambda r: str(r["bookmaker"]))[:6]:
            table.append(f"{str(row['bookmaker'])[:12]:<12}{row['home']:>6.2f}{row['draw']:>6.2f}{row['away']:>6.2f}")
        o = p.odds
        table.append(f"{'中位数':<10}{o['home']:>6.2f}{o['draw']:>6.2f}{o['away']:>6.2f}")
        lines = [
            title,
            BotUI.matchup(p, tz),
            SEP,
            f"<pre>{esc(chr(10).join(table))}</pre>",
            "📐 <b>模型 vs 市场</b>（含抽水的隐含概率）",
        ]
        for key in OUTCOMES:
            entry = p.outcomes.get(key)
            if entry:
                lines.append(
                    f"{OUTCOME_LABEL[key]}：模型 <code>{probs[key]:.1%}</code> | 市场 <code>{1 / o[key]:.1%}</code>"
                    f" | 偏差 <code>{entry['edge']:+.1%}</code>"
                )
        lines += [f"庄家抽水约 <code>{overround(o):.1%}</code>（共 {o['n']} 家公司）", SEP, DISCLAIMER]
        return "\n".join(lines)

    # ---- 视图：历史交锋 ---------------------------------------------------------
    @staticmethod
    def format_h2h(p, matches: list[dict], tz) -> str:
        title = "📊 <b>历史交锋</b>"
        finished = [m for m in matches if (m.get("goals") or {}).get("home") is not None]
        if not finished:
            return f"{title}\n{BotUI.matchup(p, tz)}\n{SEP}\n暂无历史交锋记录。"
        finished.sort(key=lambda m: (m.get("fixture") or {}).get("date") or "", reverse=True)

        win = draw = loss = gf = ga = 0
        rows = []
        for m in finished:
            teams, goals = m.get("teams") or {}, m.get("goals") or {}
            home_side = (teams.get("home") or {}).get("id") == p.home_id
            ours = goals["home"] if home_side else goals["away"]
            theirs = goals["away"] if home_side else goals["home"]
            gf, ga = gf + ours, ga + theirs
            win, draw, loss = win + (ours > theirs), draw + (ours == theirs), loss + (ours < theirs)
            date = ""
            raw = (m.get("fixture") or {}).get("date")
            if raw:
                try:
                    date = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(tz).strftime("%Y-%m-%d")
                except ValueError:
                    date = str(raw)[:10]
            mark = "🟢" if ours > theirs else "🟡" if ours == theirs else "🔴"
            rows.append(
                f"{mark} <code>{date}</code> {esc((teams.get('home') or {}).get('name', '?'))} "
                f"<b>{goals['home']}-{goals['away']}</b> {esc((teams.get('away') or {}).get('name', '?'))}"
            )
        summary = f"近 {len(finished)} 次交锋（{esc(p.home)} 视角）：<b>{win} 胜 {draw} 平 {loss} 负</b>，进 {gf} / 失 {ga}"
        return "\n".join(
            [title, BotUI.matchup(p, tz), SEP, summary, *rows, SEP, "🟢 胜 · 🟡 平 · 🔴 负；球队阵容与状态可能已大不相同，仅供参考。"]
        )

    # ---- 主菜单 -----------------------------------------------------------------
    @staticmethod
    def menu_keyboard() -> InlineKeyboardMarkup:
        """Inline 主菜单：点击后在原消息上切换，不刷屏。"""
        buttons = [InlineKeyboardButton(label, callback_data=f"menu:{key}") for key, label in MENU_ITEMS]
        return InlineKeyboardMarkup([buttons[0:2], buttons[2:4], buttons[4:6]])

    @staticmethod
    def reply_menu_keyboard() -> ReplyKeyboardMarkup:
        """底部常驻键盘：与 Inline 菜单共用 MENU_ITEMS，保证两边一致。"""
        labels = [label for _, label in MENU_ITEMS]
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton(x) for x in labels[0:2]],
                [KeyboardButton(x) for x in labels[2:4]],
                [KeyboardButton(x) for x in labels[4:6]],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def format_menu(settings) -> str:
        return (
            "⚽ <b>足球量化预测机器人</b>\n"
            f"联赛 <code>{settings.league_id}</code> · 赛季 <code>{settings.season}</code>"
            f" · 时区 <code>{settings.timezone.zone}</code>\n"
            f"{SEP}\n请选择一个功能："
        )

    @staticmethod
    def format_help() -> str:
        return (
            "ℹ️ <b>使用帮助</b>\n"
            f"{SEP}\n"
            "📅 今日赛程：列出当天全部比赛（按联赛分组、支持翻页）\n"
            "⚽ 比赛预测：选择比赛，生成胜平负概率与建议\n"
            "📊 深度分析：近期状态、联赛数据、历史交锋\n"
            "🏆 联赛排名：当前积分榜\n"
            "🔄 刷新数据：清空缓存重新拉取\n\n"
            "命令：/menu 打开菜单 · /test 立即推送 · /status 运行状态\n"
            f"{SEP}\n{DISCLAIMER}"
        )

    @staticmethod
    def format_coming(key: str) -> str:
        label = dict(MENU_ITEMS).get(key, key)
        return f"🚧 <b>{esc(label)}</b>\n{SEP}\n该功能正在开发中，将在后续阶段上线。"

    @staticmethod
    def error_hint(exc) -> str:
        """把 APIError 翻译成人话，区分 Key / 套餐 / 限流 / 赛季 / 网络等原因。"""
        text = str(exc).lower()
        if "do not have access to this season" in text or "free plan" in text:
            return "🔎 原因：当前订阅套餐不支持该赛季（Free 套餐通常只开放 2022–2024），需升级套餐或改用可访问的赛季。"
        if "rate limit" in text or "429" in text or "too many" in text:
            return "🔎 原因：请求次数已超限，请稍后再试或升级套餐。"
        if "403" in text or "not subscribed" in text or "invalid" in text:
            return "🔎 原因：API Key 无效或未订阅该接口，请检查 Key 与订阅状态。"
        if "401" in text or "unauthor" in text:
            return "🔎 原因：API Key 鉴权失败，请检查 Key 是否正确。"
        if "timeout" in text or "timed out" in text or "network" in text or "connect" in text:
            return "🔎 原因：网络异常或数据源超时，请稍后重试。"
        return "🔎 如持续出现，请检查 API Key、套餐权限与网络连接。"

    # ---- 今日赛程 ---------------------------------------------------------------
    @staticmethod
    def format_fixtures_page(
        items: list[dict], tz, page: int = 0, per_page: int = 5, day_label: str = ""
    ) -> tuple[str, InlineKeyboardMarkup, int, int]:
        """按联赛分组渲染一页赛程。返回 (文本, 键盘, 实际页码, 总页数)。"""
        total_pages = max(1, -(-len(items) // per_page))
        page = min(max(page, 0), total_pages - 1)
        chunk = items[page * per_page : (page + 1) * per_page]

        lines = [
            "📅 <b>今日赛程</b>",
            f"日期：<code>{esc(day_label)}</code> · 时区：<code>{esc(tz.zone)}</code>",
            SEP,
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if not chunk:
            lines.append("今日暂无赛程。")
        else:
            current = None
            for offset, fx in enumerate(chunk):
                idx = page * per_page + offset + 1
                info = fx.get("fixture") or {}
                league = (fx.get("league") or {}).get("name") or "未知联赛"
                if league != current:  # 按联赛分组，只在切换联赛时打印标题
                    current = league
                    lines.append(f"🏆 <b>{esc(league)}</b>")
                teams = fx.get("teams") or {}
                home = (teams.get("home") or {}).get("name") or "?"
                away = (teams.get("away") or {}).get("name") or "?"
                kickoff = parse_kickoff(info.get("date"))
                when = kickoff.astimezone(tz).strftime("%H:%M") if kickoff else "--:--"
                short = (info.get("status") or {}).get("short") or ""
                status = STATUS_TEXT.get(short, short or "未知")
                goals = fx.get("goals") or {}
                gh, ga = goals.get("home"), goals.get("away")
                score = f" <b>{esc(gh)}-{esc(ga)}</b>" if gh is not None and ga is not None else ""
                lines.append(f"{idx}. <code>{when}</code> {esc(home)} 🆚 {esc(away)}{score}")
                lines.append(f"     状态：{esc(status)} · ID <code>{esc(info.get('id'))}</code>")
                rows.append(
                    [
                        InlineKeyboardButton(f"⚽ 预测 {idx}", callback_data=f"fx:{info.get('id')}"),
                        InlineKeyboardButton(f"📊 分析 {idx}", callback_data=f"fa:{info.get('id')}"),
                    ]
                )
        if total_pages > 1:
            rows.append(
                [
                    InlineKeyboardButton("⬅️ 上一页", callback_data=f"fxp:{max(0, page - 1)}"),
                    InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"),
                    InlineKeyboardButton("下一页 ➡️", callback_data=f"fxp:{min(total_pages - 1, page + 1)}"),
                ]
            )
        rows.append([InlineKeyboardButton("↩️ 返回主菜单", callback_data="menu:home")])
        return "\n".join(lines), InlineKeyboardMarkup(rows), page, total_pages
