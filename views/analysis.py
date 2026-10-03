"""深度分析与历史交锋 / Analysis & head-to-head

本文件内的文案与生成它的逻辑同在一处，改文案只动这一个文件。
"""

from __future__ import annotations

from datetime import datetime

from analyzer import OUTCOMES, calculate_prediction_level, overround
from service import MODEL_VERSION, parse_kickoff
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from views.common import CommonView
from templates import (
    BRAND_CN,
    BRAND_EN,
    BULLET,
    DISCLAIMER,
    MENU_ITEMS,
    NO_DATA,
    OUTCOME_LABEL,
    SEP,
    STATUS_TEXT,
    TABS,
    THIN_SEP,
    VALUE_FLAG,
    VALUE_HIGH,
    VALUE_LOW,
)
from formatkit import (
    BLANK,
    _factors,
    _form_line,
    _is_fallback,
    _is_fallback_source,
    _num,
    _row_line,
    _row_summary,
    bar,
    esc,
    hbar,
    kv_line,
    league_label,
    section,
    section_join,
    team_mobile,
    team_name,
    web_entry_text,
)

def _injuries_body(report: dict, errors: dict) -> list[str]:
    """伤停信息文案：有就列名单，没有就说明原因（不伪造、不隐藏失败）。"""
    injuries = report.get("injuries") or {}
    home = injuries.get("home") or []
    away = injuries.get("away") or []
    if not home and not away:
        if errors.get("injuries"):
            return [f"⚠️ 伤停数据获取失败：{esc(errors['injuries'])}"]
        return [NO_DATA, "　当前数据源未提供本场伤停名单"]
    body: list[str] = []
    if home:
        body.append(f"🏠 {esc(report['home'])}（{len(home)} 人）")
        body += [f"▸ {esc(x)}" for x in home[:8]]
        if len(home) > 8:
            body.append(f"　…另有 {len(home) - 8} 人")
    else:
        body.append(f"🏠 {esc(report['home'])}：{NO_DATA}")
    if away:
        body.append(f"✈️ {esc(report['away'])}（{len(away)} 人）")
        body += [f"▸ {esc(x)}" for x in away[:8]]
        if len(away) > 8:
            body.append(f"　…另有 {len(away) - 8} 人")
    else:
        body.append(f"✈️ {esc(report['away'])}：{NO_DATA}")
    return body


class AnalysisView:
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
    
            # 分节统一走 section()：图标+标题 → 分隔线 → 内容 → 空行。
            # 原来各段直接堆 SEP，相邻两块之间没有任何留白，手机上读起来
            # 是一整片；空行才是移动端可读性的关键。
            form_body = [
                f"🏠 {esc(report['home'])}：{_form_line(report['home_form'])}",
                f"✈️ {esc(report['away'])}：{_form_line(report['away_form'])}",
            ]
            # 可选数据拉取失败：附真实原因，不伪装成“没有数据”
            for key, prefix in (("home_form", "主队近期"), ("away_form", "客队近期"), ("h2h", "历史交锋")):
                if errors.get(key):
                    form_body.append(f"　 ⚠️ {prefix}数据获取失败：{esc(errors[key])}")

            if h2h["played"]:
                h2h_body = [
                    f"近 {h2h['played']} 次交锋（{esc(report['home'])} 视角）：",
                    f"<b>{h2h['win']} 胜 {h2h['draw']} 平 {h2h['lose']} 负</b>"
                    f" · 进球 {h2h['goals_for']}-{h2h['goals_against']}",
                ]
            else:
                h2h_body = [NO_DATA]

            pros, cons, unknowns = _factors(report)
            # 逐条分行：手机上「；」拼接的长句会被折行成一坨，看不出有几条
            summary_body = ["✅ <b>有利</b>"]
            summary_body += [f"▸ {esc(x)}" for x in pros] or [f"▸ {NO_DATA}"]
            summary_body += ["", "⚠️ <b>不利</b>"]
            summary_body += [f"▸ {esc(x)}" for x in cons] or [f"▸ {NO_DATA}"]
            summary_body += ["", "❓ <b>不确定</b>"]
            summary_body += [f"▸ {esc(x)}" for x in unknowns] or [f"▸ {NO_DATA}"]

            blocks = [
                [
                    "📊 <b>深度分析</b>",
                    f"🏠 <b>{esc(report['home'])}</b> 🆚 <b>{esc(report['away'])}</b> ✈️",
                    f"🕐 <code>{CommonView.fmt_time(report['kickoff'], tz)}</code>"
                    f"（{CommonView.tz_label(tz, report['kickoff'])}） · {esc(report['league'])}",
                    BLANK,
                ],
                section("🕑", "近期状态 · 近 5 场已完场", *form_body),
                section(
                    "🏆", "联赛信息",
                    f"🏠 {esc(report['home'])}：{_row_line(report['home_row'])}",
                    f"✈️ {esc(report['away'])}：{_row_line(report['away_row'])}",
                ),
                section("🤝", "历史交锋", *h2h_body),
                section("🚑", "伤停信息", *_injuries_body(report, errors)),
                section(
                    "🧮", "模型因素",
                    f"🏠 主队主场：攻击 <code>{hs.attack_home:.2f}</code> · 防守 <code>{hs.defense_home:.2f}</code>（已赛 {hs.games_home} 场）",
                    f"✈️ 客队客场：攻击 <code>{aws.attack_away:.2f}</code> · 防守 <code>{aws.defense_away:.2f}</code>（已赛 {aws.games_away} 场）",
                    "（1.00 = 联赛平均；攻击 &gt;1 进球更多，防守 &lt;1 失球更少即防守更好）",
                    "",
                    f"⚖️ 主场优势：联赛主队场均 <code>{report['model']['avg_home_goals']:.2f}</code>"
                    f" / 客队场均 <code>{report['model']['avg_away_goals']:.2f}</code>",
                    f"⚽ 预期进球 λ：<code>{analysis['lambda_home']:.2f} - {analysis['lambda_away']:.2f}</code>",
                    f"🧩 数据完整性：<b>{integrity}</b>",
                    "🩹 伤停信息：数据源未提供（当前套餐不支持）",
                    f"🛰 数据源：<code>{esc(report.get('source', 'API-Football'))}</code>",
                    f"🤖 模型版本：<code>{esc(model_version)}</code>",
                    f"🕑 更新时间：<code>{CommonView.fmt_time(report['created_at'], tz, '%Y-%m-%d %H:%M:%S')}</code>",
                ),
                section("🧭", "分析总结", *summary_body),
            ]

            top = max(
                (("主胜", analysis["win_prob"]), ("平局", analysis["draw_prob"]), ("客胜", analysis["loss_prob"])),
                key=lambda kv: kv[1],
            )
            lines = section_join(blocks).split("\n")
            if _is_fallback_source(report):
                lines += [
                    SEP,
                    BLANK,
                    "ℹ️ <b>当前备用数据源仅提供基础比赛数据，暂无法生成完整深度分析</b>",
                    "（无历史交锋、无赔率、无球员统计）。",
                ]
            lines += [
                SEP,
                BLANK,
                f"🎯 <code>模型倾向　</code><b>{top[0]}</b> <code>{top[1]:.1%}</code>",
                BLANK,
                SEP,
                BLANK,
                DISCLAIMER,
            ]
            return "\n".join(lines)
    
        @staticmethod
        def format_deep_analysis(p, tz) -> str:
            a, hs, aws = p.analysis, p.home_strength, p.away_strength
            top_score_prob = a["top_scores"][0][1] or 1.0  # 防除零：全 0 概率时不炸
            score_rows = []
            for score, prob in a["top_scores"]:
                # 三段都放等宽块：比分左对齐、条形定长、百分比右对齐补位，
                # 这样「9.8%」和「12.4%」才不会把右侧数字推得一前一后
                score_rows.append(
                    f"<code>{score:<5}</code> <code>{hbar(prob / top_score_prob, 8)}</code>"
                    f" <code>{prob:>5.1%}</code>"
                )

            blocks = [
                ["🔍 <b>深度分析</b>", CommonView.matchup(p, tz), BLANK],
                section(
                    "⚙️", "预期进球 λ",
                    f"<code>{a['lambda_home']:.2f} - {a['lambda_away']:.2f}</code>",
                ),
                section("🎯", "最可能比分 Top 5", *score_rows),
                section(
                    "📊", "进球指标",
                    kv_line("⚽", "大 2.5 球",
                            f"{a['over_2_5']:.1%}　{hbar(a['over_2_5'])}", 12),
                    kv_line("🤝", "双方都进球",
                            f"{a['btts']:.1%}　{hbar(a['btts'])}", 12),
                ),
                section(
                    "🧮", "球队强度 · 1.00 = 联赛平均",
                    f"🏠 {esc(team_mobile(p.home))} 主场：攻击 <code>{hs.attack_home:.2f}</code>"
                    f" · 防守 <code>{hs.defense_home:.2f}</code>",
                    f"✈️ {esc(team_mobile(p.away))} 客场：攻击 <code>{aws.attack_away:.2f}</code>"
                    f" · 防守 <code>{aws.defense_away:.2f}</code>",
                    f"　 主场已赛 {hs.games_home} 场 · 客场已赛 {aws.games_away} 场",
                    "",
                    "攻击 &gt;1：进球高于平均",
                    "防守 &lt;1：失球低于平均",
                ),
            ]
            lines = section_join(blocks).split("\n")
            lines += [SEP, BLANK, DISCLAIMER]
            return "\n".join(lines)
    
        @staticmethod
        def format_h2h(p, matches: list[dict], tz) -> str:
            title = "📊 <b>历史交锋</b>"
            finished = [m for m in matches if (m.get("goals") or {}).get("home") is not None]
            if not finished:
                return f"{title}\n{CommonView.matchup(p, tz)}\n{SEP}\n暂无历史交锋记录。"
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
            # 战绩与进球分两行：挤在一行时「3胜1平1负」和「进8/失5」会在窄屏
            # 折行，读起来像第四场比赛。
            blocks = [
                [title, CommonView.matchup(p, tz), BLANK],
                section(
                    "📈", f"近 {len(finished)} 次交锋战绩",
                    f"<b>{win} 胜 {draw} 平 {loss} 负</b>（{esc(team_name(p.home))} 视角）",
                    f"进 <code>{gf}</code> / 失 <code>{ga}</code>",
                ),
                section("🗓", "近期交手记录", *rows),
            ]
            lines = section_join(blocks).split("\n")
            lines += [
                SEP,
                BLANK,
                "🟢 胜 · 🟡 平 · 🔴 负；球队阵容与状态可能已大不相同，仅供参考。",
            ]
            return "\n".join(lines)
