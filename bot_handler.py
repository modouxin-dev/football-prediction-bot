"""消息模板与按钮键盘（只负责排版，不做网络请求）。所有来自数据源的文本都会做 HTML 转义。"""
from __future__ import annotations

import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from analyzer import OUTCOMES, overround

SEP = "━━━━━━━━━━━━━━━━━━"
OUTCOME_LABEL = {"home": "主胜", "draw": "平局", "away": "客胜"}
DISCLAIMER = "⚠️ 模型仅基于进球数据估算，不构成投注建议。"
VALUE_FLAG = 0.05  # 价值偏差超过此值时打 🚀 标记
VALUE_HIGH = 0.07
VALUE_LOW = 0.03

# 视图标签页：(callback 前缀, 按钮文字)
TABS = (("home", "📈 预测"), ("deep", "🔍 深度分析"), ("h2h", "📊 历史交锋"), ("odds", "💰 赔率对比"))


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
