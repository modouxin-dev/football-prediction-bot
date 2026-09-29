"""预测视图 / Prediction views

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
    league_label,
    team_name,
    web_entry_text,
)

class PredictionView:
        @staticmethod
        def format_prediction(p, tz) -> str:
            a = p.analysis
            probs = (("主胜", a["win_prob"]), ("平局", a["draw_prob"]), ("客胜", a["loss_prob"]))
            top = max(prob for _, prob in probs)
    
            title = f"🏆 <b>{esc(p.league)}</b>" + (f" · {esc(p.round_label)}" if p.round_label else "")
            when = f"🕐 <code>{CommonView.fmt_time(p.kickoff, tz)}</code> ({CommonView.tz_label(tz, p.kickoff)})"
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
            # 数据不足：必须明确告知结论不可信，不能与正常预测同等呈现
            if getattr(p, "insufficient", False):
                lines += [
                    SEP,
                    "⚠️ <b>数据不足</b>",
                    "积分榜中未包含这两支球队，",
                    "当前概率仅基于联赛平均水平估算，<b>不代表双方真实实力</b>。",
                ]
            if _is_fallback(p):
                lines.append("ℹ️ 当前为备用数据源，仅提供基础比赛数据，赔率等高级统计不可用。")
            # 同时校验 odds：best 由 evaluate_outcomes(analysis, odds) 产生，
            # odds 为空时它必然返回 {}（best 亦为 None）。但这是**跨模块的隐式不变量**，
            # 一旦有人另处构造 Prediction（如从库里回填、未来新增代码路径）就会在此
            # 抛 TypeError，导致整条推送失败。显式校验让降级逻辑自洽：
            # 任一缺失都走「赔率暂无」，而不是让整条消息崩掉。
            if p.best and p.odds:
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
                f"🎯 <b>建议</b>：{esc(PredictionView.get_strategy(p))}",
                f"💎 <b>信心</b>：{PredictionView.get_confidence(p)}",
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
    
        @classmethod
        def format_prediction_card(cls, p, tz, model_version: str = MODEL_VERSION) -> str:
            """预测主卡：少文字 + 大结论 + 数据卡片。
    
            结构固定为四段：顶部看比赛 → 中部看预测 → 下部看依据 → 底部小字来源。
            详细数据（近期战绩、预期进球）集中在「核心数据」，避免正文塞满。
            """
            a = p.analysis
            probs = (("主胜", a["win_prob"]), ("平局", a["draw_prob"]), ("客胜", a["loss_prob"]))
            top_label, top_prob = max(probs, key=lambda kv: kv[1])
            home, away = esc(team_name(p.home)), esc(team_name(p.away))
            level = p.level
    
            # 大结论：不败 / 单一结果，比「最可能结果」更接近用户直觉
            if top_label == "主胜":
                verdict = f"{home}不败" if top_prob + a["draw_prob"] >= 0.7 else "主胜"
            elif top_label == "客胜":
                verdict = f"{away}不败" if top_prob + a["draw_prob"] >= 0.7 else "客胜"
            else:
                verdict = "平局"
    
            # 数据可信度： GOOD / FAIR / POOR，一眼看出结论能不能信
            if getattr(p, "insufficient", False):
                quality = "POOR"
            elif p.low_sample:
                quality = "FAIR"
            else:
                quality = "GOOD"
    
            lines = [
                "<b>⚽ FOOTBALL INSIGHT</b>",
                f"<code>{esc(p.league)} · {CommonView.fmt_time(p.kickoff, tz, '%Y-%m-%d %H:%M')}</code>",
                "",
                f"<b>{home}</b>  <code>VS</code>  <b>{away}</b>",
                "",
                SEP,
                "<b>🔮 比赛预测</b>",
                "",
            ]
            for label, prob in probs:
                lines.append(f"{label}　<b>{prob:.0%}</b>  {cls._hbar(prob)}")
            lines += [
                "",
                f"预测结果：<b>{verdict}</b>",
                f"预计比分：<b>{esc(a['best_score'])}</b>",
                f"信心等级：<b>{level['emoji']} {level['name']}</b>",
            ]
    
            # 核心数据：详细指标集中在此，正文不再堆砌
            core = [SEP, "", "<b>📊 核心数据</b>", ""]
            core.append(f"预期进球：<b>{a['lambda_home']:.2f} - {a['lambda_away']:.2f}</b>")
            if p.has_team_data:
                core.append(
                    f"攻防强度：<b>主 {p.home_strength.attack_home:.2f} / 客 {p.away_strength.attack_away:.2f}</b>"
                )
            core.append(f"数据完整性：<b>{esc(p.data_completeness)}</b>")
            lines += core
    
            # 模型分析：结论式短句，不做长篇说明
            notes = cls._model_notes(p, home, away, verdict)
            if notes:
                lines += [SEP, "", "<b>📝 模型分析</b>", ""]
                lines += [f"• {n}" for n in notes]
    
            # 脚注：来源/赛季/模型版本/更新时间一律小字，不占正文篇幅
            lines += [
                "",
                f"<code>DATA QUALITY: {quality} · SOURCE: {esc(getattr(p, 'source', 'API-Football'))}"
                f" · SEASON: {esc(p.season or '未知')}</code>",
                f"<code>MODEL: {esc(model_version)} · UPDATED: "
                f"{CommonView.fmt_time(p.created_at, tz, '%H:%M')} UTC+8</code>",
            ]
    
            # 数据不足：必须明确告知结论不可信，不能与正常预测同等呈现
            if getattr(p, "insufficient", False):
                lines += [
                    SEP,
                    "⚠️ <b>数据不足</b>：积分榜未包含这两支球队，",
                    "当前概率仅基于联赛平均估算，<b>不代表双方真实实力</b>。",
                ]
            if _is_fallback(p):
                lines.append("ℹ️ 备用数据源：仅基础比赛数据，赔率等高级统计不可用。")
            lines += ["", DISCLAIMER]
            return "\n".join(lines)
    
        @staticmethod
        def _model_notes(p, home: str, away: str, verdict: str) -> list[str]:
            """模型分析短句：每条结论都对应真实数据，缺数据时不编造。"""
            notes: list[str] = []
            if not p.has_team_data:
                notes.append("缺少两队实际数据，结论参考价值有限")
                return notes
            hs, as_ = p.home_strength, p.away_strength
            if hs.attack_home >= as_.attack_away:
                notes.append(f"{home}近期进攻状态更稳定")
            else:
                notes.append(f"{away}近期进攻状态更稳定")
            if as_.defense_away > hs.defense_home:
                notes.append(f"{away}客场防守存在波动")
            else:
                notes.append(f"{home}主场防守更稳固")
            notes.append(f"综合数据倾向：{verdict}")
            return notes
    
        @staticmethod
        def format_odds_detail(p, tz) -> str:
            title = "💰 <b>赔率对比</b>"
            if not p.odds:
                return f"{title}\n{CommonView.matchup(p, tz)}\n{SEP}\n暂无赔率数据（该场比赛可能尚未开盘）。"
            a = p.analysis
            probs = {"home": a["win_prob"], "draw": a["draw_prob"], "away": a["loss_prob"]}
            table = [f"{'博彩公司':<12}{'主':>6}{'平':>6}{'客':>6}"]
            for row in sorted(p.bookmakers, key=lambda r: str(r["bookmaker"]))[:6]:
                table.append(f"{str(row['bookmaker'])[:12]:<12}{row['home']:>6.2f}{row['draw']:>6.2f}{row['away']:>6.2f}")
            o = p.odds
            table.append(f"{'中位数':<10}{o['home']:>6.2f}{o['draw']:>6.2f}{o['away']:>6.2f}")
            lines = [
                title,
                CommonView.matchup(p, tz),
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
