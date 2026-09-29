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
    league_label,
    team_name,
    web_entry_text,
)

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
    
            lines = [
                "📊 <b>深度分析</b>",
                f"🏠 <b>{esc(report['home'])}</b> 🆚 <b>{esc(report['away'])}</b> ✈️",
                f"🕐 <code>{CommonView.fmt_time(report['kickoff'], tz)}</code>（{CommonView.tz_label(tz, report['kickoff'])}） · {esc(report['league'])}",
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
                f"🕑 数据更新时间：<code>{CommonView.fmt_time(report['created_at'], tz, '%Y-%m-%d %H:%M:%S')}</code>",
                SEP,
                "🧭 <b>分析总结</b>",
            ]
            pros, cons, unknowns = _factors(report)
            # 逐条分行：手机上「；」拼接的长句会被折行成一坨，看不出有几条
            lines.append("✅ <b>有利</b>")
            lines += [f"▸ {esc(x)}" for x in pros] or [f"▸ {NO_DATA}"]
            lines.append("⚠️ <b>不利</b>")
            lines += [f"▸ {esc(x)}" for x in cons] or [f"▸ {NO_DATA}"]
            lines.append("❓ <b>不确定</b>")
            lines += [f"▸ {esc(x)}" for x in unknowns] or [f"▸ {NO_DATA}"]
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
    
        @staticmethod
        def format_deep_analysis(p, tz) -> str:
            a, hs, aws = p.analysis, p.home_strength, p.away_strength
            lines = [
                "🔍 <b>深度分析</b>",
                CommonView.matchup(p, tz),
                SEP,
                f"⚙️ 预期进球 λ：<code>{a['lambda_home']:.2f} - {a['lambda_away']:.2f}</code>",
                "🎯 <b>最可能比分 Top 5</b>",
            ]
            top_score_prob = a["top_scores"][0][1] or 1.0  # 防除零：全 0 概率时不炸
            for score, prob in a["top_scores"]:
                # 三段都放等宽块：比分左对齐、条形定长、百分比右对齐补位，
                # 这样「9.8%」和「12.4%」才不会把右侧数字推得一前一后
                lines.append(
                    f"<code>{score:<5}</code> <code>{hbar(prob / top_score_prob, 8)}</code>"
                    f" <code>{prob:>5.1%}</code>"
                )
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
            summary = f"近 {len(finished)} 次交锋（{esc(team_name(p.home))} 视角）：<b>{win} 胜 {draw} 平 {loss} 负</b>，进 {gf} / 失 {ga}"
            return "\n".join(
                [title, CommonView.matchup(p, tz), SEP, summary, *rows, SEP, "🟢 胜 · 🟡 平 · 🔴 负；球队阵容与状态可能已大不相同，仅供参考。"]
            )
