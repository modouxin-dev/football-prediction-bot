"""消息模板与按钮键盘（只负责排版，不做网络请求）。所有来自数据源的文本都会做 HTML 转义。"""
from __future__ import annotations

import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from analyzer import OUTCOMES, calculate_prediction_level, overround
from service import MODEL_VERSION, parse_kickoff

SEP = "━━━━━━━━━━━━━━━━━━"
THIN_SEP = "──────────────────"  # 次级分隔：用于分区内部，避免主分隔线过度重复
BULLET = "▸"  # 列表符号

# 联赛 ID → 中文名称（仅用于展示；未知 ID 直接显示数字，不猜测）
LEAGUE_NAMES = {
    39: "英格兰超级联赛",
    140: "西班牙甲级联赛",
    78: "德国甲级联赛",
    135: "意大利甲级联赛",
    61: "法国甲级联赛",
    2: "欧洲冠军联赛",
    88: "荷兰甲级联赛",
    94: "葡萄牙超级联赛",
    40: "英格兰冠军联赛",
    71: "巴西甲级联赛",
    1: "国际足联世界杯",
    4: "欧洲足球锦标赛",
}


# 队名中英对照表 / Team name mapping (CN ↔ EN)
# 说明 / Note: football-data.org 与 API-Football 返回的球队名均为英文原文，
# 这里按官方名建立中文对照，供界面双语展示。未收录的球队只显示英文原名。
TEAM_NAMES: dict[str, str] = {
    # 英超 / Premier League
    "Manchester City FC": "曼城", "Liverpool FC": "利物浦", "Arsenal FC": "阿森纳",
    "Manchester United FC": "曼联", "Chelsea FC": "切尔西", "Tottenham Hotspur FC": "托特纳姆热刺",
    "Newcastle United FC": "纽卡斯尔联", "Brighton & Hove Albion FC": "布莱顿",
    "Aston Villa FC": "阿斯顿维拉", "West Ham United FC": "西汉姆联",
    "Crystal Palace FC": "水晶宫", "Everton FC": "埃弗顿", "Fulham FC": "富勒姆",
    "Brentford FC": "布伦特福德", "Nottingham Forest FC": "诺丁汉森林",
    "AFC Bournemouth": "伯恩茅斯", "Wolverhampton Wanderers FC": "狼队",
    "Leeds United FC": "利兹联", "Sunderland AFC": "桑德兰", "Burnley FC": "伯恩利",
    # 西甲 / La Liga
    "Real Madrid CF": "皇家马德里", "FC Barcelona": "巴塞罗那",
    "Club Atlético de Madrid": "马德里竞技", "Sevilla FC": "塞维利亚",
    "Real Betis Balompié": "皇家贝蒂斯", "Valencia CF": "瓦伦西亚",
    "Villarreal CF": "比利亚雷亚尔", "Athletic Club": "毕尔巴鄂竞技",
    "Real Sociedad de Fútbol": "皇家社会", "RC Celta de Vigo": "塞尔塔",
    # 德甲 / Bundesliga
    "FC Bayern München": "拜仁慕尼黑", "Borussia Dortmund": "多特蒙德",
    "RB Leipzig": "莱比锡红牛", "Bayer 04 Leverkusen": "勒沃库森",
    "Eintracht Frankfurt": "法兰克福", "VfB Stuttgart": "斯图加特",
    "Borussia Mönchengladbach": "门兴格拉德巴赫", "VfL Wolfsburg": "沃尔夫斯堡",
    # 意甲 / Serie A
    "Juventus FC": "尤文图斯", "FC Internazionale Milano": "国际米兰",
    "AC Milan": "AC米兰", "SSC Napoli": "那不勒斯", "AS Roma": "罗马",
    "SS Lazio": "拉齐奥", "Atalanta BC": "亚特兰大", "ACF Fiorentina": "佛罗伦萨",
    # 法甲 / Ligue 1
    "Paris Saint-Germain FC": "巴黎圣日耳曼", "Olympique de Marseille": "马赛",
    "Olympique Lyonnais": "里昂", "AS Monaco FC": "摩纳哥", "LOSC Lille": "里尔",
}


def team_name(raw: str | None, bilingual: bool = True) -> str:
    """队名展示 / Display team name.

    双语模式返回「中文名 (English)」，未收录时只返回英文原名，绝不猜测或编造。
    Bilingual mode returns "中文 (English)"; unknown teams fall back to the
    original English name — never guessed or fabricated.
    """
    raw = (raw or "").strip()
    if not raw:
        return "?"
    cn = TEAM_NAMES.get(raw)
    if not cn or not bilingual:
        return raw
    return f"{cn} ({raw})"


def league_label(league_id: int) -> str:
    """展示用联赛名：已知 ID 显示中文名 + ID，未知则只显示 ID。"""
    name = LEAGUE_NAMES.get(int(league_id))
    return f"{name} · {league_id}" if name else f"联赛 {league_id}"
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
    ("web", "🌐 网页端"),
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


def build_prediction_payload(p, tz) -> dict:
    """统一预测结果数据结构（供格式化与未来的网页端复用）。"""
    a = p.analysis
    probabilities = {
        "home_team": team_name(p.home),
        "away_team": team_name(p.away),
        "home_win": float(a["win_prob"]),
        "draw": float(a["draw_prob"]),
        "away_win": float(a["loss_prob"]),
    }
    level = calculate_prediction_level(probabilities)
    probabilities.update(
        {
            "result": level["result"],
            "level_key": level["key"],
            "level_name": level["name"],
            "level_emoji": level["emoji"],
            "source": p.source,
            "season": p.season,
            "kickoff": BotUI.fmt_time(p.kickoff, tz),
        }
    )
    return probabilities


def split_html_blocks(text: str, limit: int = 3500) -> list[str]:
    """按行拆分超长 HTML 文本，避免超过 Telegram 单条 4096 字符限制。

    整段 <code>/<b> 标签不会被拆断（按行切分已足够安全，因为标签不跨行）。
    """
    if len(text) <= limit:
        return [text]
    blocks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            blocks.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        blocks.append(current.rstrip())
    return blocks


WEB_ENTRY_TEXT = (
    "🌐 <b>网页端</b>\n\n"
    "网页查询界面正在规划中，当前阶段以 Telegram 机器人的数据稳定性为主。\n\n"
    "开放后将支持：\n"
    "· 在浏览器里查看今日赛程与预测\n"
    "· 查询历史预测与命中情况\n"
    "· 多联赛切换\n\n"
    "目前请继续使用下方菜单功能。"
)


def _is_fallback(p) -> bool:
    """预测结果是否来自备用数据源 football-data.org。"""
    return "football-data" in str(getattr(p, "source", "")).lower()


def _is_fallback_source(report: dict) -> bool:
    """深度分析报告是否基于备用数据源。"""
    return "football-data" in str(report.get("source", "")).lower()


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
            f"🏠 <b>{esc(team_name(p.home))}</b> 🆚 <b>{esc(team_name(p.away))}</b> ✈️\n"
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
            f"🏠 <b>{esc(team_name(p.home))}</b>",
            "      🆚",
            f"✈️ <b>{esc(team_name(p.away))}</b>",
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
        if _is_fallback(p):
            lines.append("ℹ️ 当前为备用数据源，仅提供基础比赛数据，赔率等高级统计不可用。")
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
        """模型信心等级：🟢 高 / 🟡 中 / 🔴 低，只由概率计算。

        概率本身已经反映了数据完整性，因此不再叠加人工规则；
        仅在完全无球队数据时额外说明原因。
        """
        level = calculate_prediction_level(
            {
                "home_win": p.analysis["win_prob"],
                "draw": p.analysis["draw_prob"],
                "away_win": p.analysis["loss_prob"],
            }
        )
        text = f"{level['emoji']} {level['name']}"
        if not p.has_team_data:
            text += "（缺少球队数据，仅按联赛平均估算）"
        elif p.low_sample:
            text += "（近期样本不足）"
        return text

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
            f"🏠 <b>{esc(team_name(p.home))}</b> 🆚 <b>{esc(team_name(p.away))}</b> ✈️",
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
            f"💎 <b>模型信心等级</b>：{esc(BotUI.confidence_text(p))}",
            f"📅 <b>使用赛季</b>：<code>{esc(p.season or '未知')}</code>",
            f"🧩 <b>数据完整性</b>：{esc(p.data_completeness)}"
            + ("" if p.has_team_data else "（未使用球队实际数据）"),
            f"🛰 <b>数据源</b>：<code>{esc(getattr(p, 'source', 'API-Football'))}</code>",
            f"🤖 <b>模型版本</b>：<code>{esc(model_version)}</code>",
            f"🕑 <b>数据更新时间</b>：<code>{BotUI.fmt_time(p.created_at, tz, '%Y-%m-%d %H:%M:%S')}</code>"
            f"（{BotUI.tz_label(tz, p.created_at)}）",
        ]
        if _is_fallback(p):
            lines.append("ℹ️ 当前为备用数据源，仅提供基础比赛数据，赔率等高级统计不可用。")
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
            f"🛰 数据源：<code>{esc(report.get('source', 'API-Football'))}</code>",
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
        if _is_fallback_source(report):
            lines += [
                SEP,
                "ℹ️ <b>当前备用数据源仅提供基础比赛数据，暂无法生成完整深度分析</b>"
                "（无历史交锋、无赔率、无球员统计）。",
            ]
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
            f"🏠 {esc(team_name(p.home))} 主场：攻击 <code>{hs.attack_home:.2f}</code> · 防守 <code>{hs.defense_home:.2f}</code>（已赛 {hs.games_home} 场）",
            f"✈️ {esc(team_name(p.away))} 客场：攻击 <code>{aws.attack_away:.2f}</code> · 防守 <code>{aws.defense_away:.2f}</code>（已赛 {aws.games_away} 场）",
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
        summary = f"近 {len(finished)} 次交锋（{esc(team_name(p.home))} 视角）：<b>{win} 胜 {draw} 平 {loss} 负</b>，进 {gf} / 失 {ga}"
        return "\n".join(
            [title, BotUI.matchup(p, tz), SEP, summary, *rows, SEP, "🟢 胜 · 🟡 平 · 🔴 负；球队阵容与状态可能已大不相同，仅供参考。"]
        )

    # ---- 主菜单 -----------------------------------------------------------------
    @staticmethod
    def menu_keyboard() -> InlineKeyboardMarkup:
        """Inline 主菜单：点击后在原消息上切换，不刷屏。"""
        buttons = [InlineKeyboardButton(label, callback_data=f"menu:{key}") for key, label in MENU_ITEMS]
        return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])

    @staticmethod
    def reply_menu_keyboard() -> ReplyKeyboardMarkup:
        """底部常驻键盘：与 Inline 菜单共用 MENU_ITEMS，保证两边一致。"""
        labels = [label for _, label in MENU_ITEMS]
        # 每行 2 个，跟随 MENU_ITEMS 自动适配数量
        rows = [[KeyboardButton(x) for x in labels[i : i + 2]] for i in range(0, len(labels), 2)]
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def format_welcome(settings) -> str:
        """/start 欢迎文案：科技仪表盘风格（配套品牌头图，头图已含品牌名）。

        注意：Telegram 消息区不是等宽字体，因此这里只用「短边框 + 参数面板」
        这类容错较高的符号；主视觉由品牌头图承担，避免长边框错位。
        """
        return (
            "👋 <b>欢迎使用</b>\n"
            "\n"
            "用泊松分布拆解每一场比赛，\n"
            "让预测可量化、可追溯。\n"
            "\n"
            f"◆ <b>引擎参数</b>\n"
            f"{THIN_SEP}\n"
            f"▍模型　<code>Poisson Distribution</code>\n"
            f"▍数据　赛程 · 积分榜 · 赔率\n"
            f"▍输出　概率 · 信心评级 · 图表\n"
            "\n"
            f"◆ <b>核心功能</b>\n"
            f"{THIN_SEP}\n"
            f"│ 📅 今日赛程\n"
            f"│ ⚽ 比赛预测\n"
            f"│ 📊 深度分析\n"
            f"│ 🏆 联赛排名\n"
            f"└ 📈 数据图表\n"
            "\n"
            f"{SEP}\n"
            f"⏰ 每日 <code>{settings.push_time:%H:%M}</code>（{esc(BotUI.tz_label(settings.timezone))}）自动推送\n"
            "💡 点击下方菜单，或发送 /menu 开始"
        )

    @staticmethod
    def format_menu(settings) -> str:
        return (
            "⚽ <b>足球量化预测机器人</b>\n"
            f"{SEP}\n"
            "\n"
            "⚙️ <b>运行环境</b>\n"
            f"{THIN_SEP}\n"
            f"│ 联赛　<code>{esc(league_label(settings.league_id))}</code>\n"
            f"│ 赛季　<code>{settings.season}</code>\n"
            f"└ 时区　<code>{esc(settings.timezone.zone)}</code>\n"
            "\n"
            f"{SEP}\n"
            "📌 <b>请选择功能</b>"
        )

    @staticmethod
    def format_help() -> str:
        return (
            "ℹ️ <b>使用帮助</b>\n"
            f"{SEP}\n"
            "\n"
            f"◆ <b>功能说明</b>\n"
            f"{THIN_SEP}\n"
            "📅 <b>今日赛程</b>　当日比赛，按联赛分组、支持翻页\n"
            "⚽ <b>比赛预测</b>　胜平负概率、比分与信心等级\n"
            "📊 <b>深度分析</b>　近期状态、主客场、历史交锋\n"
            "🏆 <b>联赛排名</b>　实时积分榜与攻防数据\n"
            "🔄 <b>刷新数据</b>　清空缓存，重新拉取\n"
            "🌐 <b>网页端</b>　　浏览器查询入口（规划中）\n"
            "\n"
            f"◆ <b>命令列表 / Commands</b>\n"
            f"{THIN_SEP}\n"
            "<code>/start</code>　欢迎与推送时间 / Welcome\n"
            "<code>/menu</code>　功能菜单 / Main menu\n"
            "<code>/help</code>　本指南 / This help\n"
            "<code>/fixtures</code>　今日赛程 / Fixtures\n"
            "<code>/predict</code>　比赛预测 / Prediction\n"
            "<code>/standings</code>　联赛排名 / Standings\n"
            "<code>/refresh</code>　刷新数据 / Refresh\n"
            "<code>/web</code>　网页端 / Web app\n"
            "<code>/test</code>　立即推送（管理员）/ Push now (admin)\n"
            "<code>/status</code>　状态诊断（管理员）/ Status (admin)\n"
            "\n"
            f"{SEP}\n"
            f"{DISCLAIMER}"
        )

    @staticmethod
    def format_coming(key: str) -> str:
        label = dict(MENU_ITEMS).get(key, key)
        return (
            f"🚧 <b>{esc(label)}</b>\n"
            f"{SEP}\n"
            "\n"
            "该功能正在开发中，将在后续阶段上线。\n"
            "\n"
            f"{SEP}\n"
            "📌 可先使用其他功能，或发送 /help 查看完整说明。"
        )

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
        items: list[dict], tz, page: int = 0, per_page: int = 5, day_label: str = "",
        multi_day: bool = False,
    ) -> tuple[str, InlineKeyboardMarkup, int, int]:
        """按联赛分组渲染一页赛程。返回 (文本, 键盘, 实际页码, 总页数)。

        multi_day=True 表示这批赛程跨越多天（今日无比赛时扩展到未来），
        此时标题改为「近期赛程」，且每场比赛显示日期，避免用户误以为是今天的比赛。
        """
        total_pages = max(1, -(-len(items) // per_page))
        page = min(max(page, 0), total_pages - 1)
        chunk = items[page * per_page : (page + 1) * per_page]

        title = "📅 <b>近期赛程</b>" if multi_day else "📅 <b>今日赛程</b>"
        lines = [
            title,
            f"日期：<code>{esc(day_label)}</code> · 时区：<code>{esc(tz.zone)}</code>",
            SEP,
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if not chunk:
            lines.append("近期暂无赛程。" if multi_day else "今日暂无赛程。")
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
                home = team_name((teams.get("home") or {}).get("name"))
                away = team_name((teams.get("away") or {}).get("name"))
                kickoff = parse_kickoff(info.get("date"))
                if kickoff:
                    local = kickoff.astimezone(tz)
                    when = local.strftime("%m-%d %H:%M") if multi_day else local.strftime("%H:%M")
                else:
                    when = "--:--"
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
