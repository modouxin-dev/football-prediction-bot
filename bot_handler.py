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
        """预测卡片下方：复用现有 4 个标签页（可继续看深度分析/交锋/赔率）+ 返回。"""
        buttons = [
            InlineKeyboardButton(label, callback_data=f"{key}:{fixture_id}") for key, label in TABS
        ]
        return InlineKeyboardMarkup(
            [
                buttons[:2],
                buttons[2:],
                [InlineKeyboardButton("🔄 刷新赔率", callback_data=f"refresh:{fixture_id}")],
                [InlineKeyboardButton("↩️ 返回赛程", callback_data="menu:fixtures"), InlineKeyboardButton("🏠 主菜单", callback_data="menu:home")],
            ]
        )

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
