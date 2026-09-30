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
    kv_line,
    _factors,
    _form_line,
    _is_fallback,
    _is_fallback_source,
    _num,
    _row_line,
    _row_summary,
    align_cjk,
    bar,
    esc,
    BLANK,
    hbar,
    league_label,
    pad_cjk,
    section,
    section_join,
    team_name,
    web_entry_text,
)

# 三种结果对应的图标。键是视图内部使用的中文标签，与 OUTCOME_LABEL 的值一致。
# 放在模块级而不是类里：views/ 下多个模块都要用，且它是纯常量不会变。
OUTCOME_ICON = {"主胜": "🏠", "平局": "🤝", "客胜": "✈️"}

# 核心数据区的标签列宽（按显示宽度算，中文占 2 列）。
# 取最长标签「最可能比分」的宽度 10 再加 2 列间隔：若只取 10，5 字标签会
# 与数值贴死，看上去像没分隔。
CORE_LABEL_WIDTH = 12

class PredictionView:
        @staticmethod
        def format_prediction(p, tz) -> str:
            a = p.analysis
            probs = (("主胜", a["win_prob"]), ("平局", a["draw_prob"]), ("客胜", a["loss_prob"]))
    
            title = f"🏆 <b>{esc(p.league)}</b>" + (f" · {esc(p.round_label)}" if p.round_label else "")
            when = f"🕐 <code>{CommonView.fmt_time(p.kickoff, tz)}</code> ({CommonView.tz_label(tz, p.kickoff)})"
            if p.venue:
                when += f" · 🏟 {esc(p.venue)}"
    
            top_label, top_prob = max(probs, key=lambda kv: kv[1])

            lines = [
                title,
                when,
                BLANK,
                SEP,
                BLANK,
                # 对阵块：居中一个 ⚔️，两行队名，比挤在一行更容易扫读
                f"🏠 <b>{esc(team_name(p.home))}</b>",
                "　　　⚔️",
                f"✈️ <b>{esc(team_name(p.away))}</b>",
                BLANK,
                SEP,
                BLANK,
                "📈 <b>胜平负概率</b>",
                SEP,
            ]
            for label, prob in probs:
                # 百分比放等宽块内并用 >3 补位，避免「9%」与「100%」错开。
                # 条形仍用 bar()（▰▱）：/predict 与每日推送卡片刻意区分字形，
                # 卡片用 hbar() 的实心方块更醒目，这里是常规查询用圆角条。
                mark = " 👈" if label == top_label else ""
                lines.append(
                    f"{OUTCOME_ICON[label]} {label}　<code>{bar(prob)} {prob:>3.0%}</code>{mark}"
                )
            # 核心数据独立成节：预期进球与比分是用户最常看的两个数。
            # 标签统一用 pad_cjk 补到同一列宽（与主卡一致），数值放进 <code>——
            # 直接写全角空格遇到 4 字与 5 字标签混排时仍会差一列。
            lines += [BLANK, SEP, BLANK, "⚡ <b>核心数据</b>", SEP]
            core_rows = [
                ("⚽", "预期进球", f"{a['lambda_home']:.2f} | {a['lambda_away']:.2f}"),
                ("🎲", "最可能比分", esc(a["best_score"])),
                ("📈", "大 2.5 球", f"{a['over_2_5']:.1%} | 小 {1 - a['over_2_5']:.1%}"),
            ]
            if p.has_team_data:
                core_rows.append((
                    "💪", "攻防强度",
                    f"主 {p.home_strength.attack_home:.2f} | 客 {p.away_strength.attack_away:.2f}",
                ))
            core_rows.append(("🧩", "数据完整性", esc(p.data_completeness)))
            for icon, label, value in core_rows:
                lines.append(kv_line(icon, label, value, CORE_LABEL_WIDTH))
            # 数据不足：必须明确告知结论不可信，不能与正常预测同等呈现
            if getattr(p, "insufficient", False):
                lines += [
                    BLANK,
                    SEP,
                    BLANK,
                    "⚠️ <b>数据不足</b>",
                    SEP,
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
                # 赔率与偏差也进核心数据的标签列：与主卡不同，这里两者都是
                # 同一节内的补充指标，列宽对齐后才不会比上面的行突出一格。
                lines.append(
                    kv_line("💰", "赔率",
                            f"{p.odds['home']:.2f} | {p.odds['draw']:.2f} | {p.odds['away']:.2f}",
                            CORE_LABEL_WIDTH)
                    + f"（{p.odds['n']} 家中位数）"
                )
                # 价值偏差的值含 <b> 与 🚀，不能整行进 <code>（不支持嵌套），
                # 因此标签沿用等宽块、值留在外层，两侧标签同宽故起点一致
                lines.append(
                    f"🔎 <code>{pad_cjk('价值偏差', CORE_LABEL_WIDTH)}</code>"
                    f"<b>{OUTCOME_LABEL[key]}</b> <code>{o['edge']:+.2%}</code>{flag}"
                )
            else:
                lines.append(kv_line("💰", "赔率", "暂无（本场仅提供模型概率）", CORE_LABEL_WIDTH))

            # 结论：建议与信心用同一套标签列，读起来是一组而不是两句散话
            lines += [
                BLANK,
                SEP,
                BLANK,
                "🎯 <b>结论</b>",
                SEP,
                f"🎯 <code>建议　</code><b>{esc(PredictionView.get_strategy(p))}</b>",
                f"💎 <code>信心　</code>{PredictionView.get_confidence(p)}",
            ]

            # 风险因素：与主卡共用 risk_bullets，两处说法永远一致，不会各写一套
            risks = PredictionView.risk_bullets(p)
            if risks:
                lines += [BLANK, SEP, BLANK, "⚠️ <b>风险因素</b>", SEP]
                lines += [f"• {r}" for r in risks]

            lines += [
                BLANK,
                f"<code>MODEL: {esc(MODEL_VERSION)} · SOURCE: "
                f"{esc(getattr(p, 'source', 'API-Football'))} · SEASON: {esc(p.season or '未知')}</code>",
                BLANK,
                SEP,
                BLANK,
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
        def risk_bullets(p) -> list[str]:
            """风险提示正文（不带符号）：缺什么就说什么，绝不虚构概率。

            与 risk_lines 分开的原因：主卡用「•」列表排版，符号由渲染处决定；
            符号若写死在正文里，复用时就会出现「• ⚠️」双重前缀。
            """
            lines = []
            if not p.model.teams:
                lines.append("未取到积分榜数据，概率仅来自联赛平均基准，参考价值有限。")
            elif not p.has_team_data:
                lines.append("积分榜中没有这两支球队，只能按联赛平均估算。")
            elif p.low_sample:
                lines.append("主/客场已赛场次不足 5 场，强度估计不稳定。")
            if not p.odds:
                lines.append("暂无赔率数据，未做价值偏差对比。")
            lines.append("本结果基于历史进球数据，未考虑伤停、赛程密度与临场变数。")
            return lines

        @classmethod
        def risk_lines(cls, p) -> list[str]:
            """风险提示：每行自带 ⚠️，供需要独立成行的场景使用。"""
            return [f"⚠️ {line}" for line in cls.risk_bullets(p)]
    
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
    
            # 概率行：图标 + 两字标签 + 等宽条与百分比。
            # 百分比放进 <code> 而不是用 <b> 包在外面——正文是比例字体，
            # 「9%」和「100%」宽度不同会错开；等宽块里用 >3 补位才真正对齐。
            # 最高概率那行用 👈 标出，比加粗更醒目（加粗在等宽块里不生效）。
            prob_lines = []
            for label, prob in probs:
                mark = " 👈" if prob == top_prob else ""
                prob_lines.append(
                    f"{OUTCOME_ICON[label]} {label}　<code>{hbar(prob)} {prob:>3.0%}</code>{mark}"
                )

            # 统一分节：标题 → 分隔线 → 空行 → 内容。空行是排版的关键，
            # 没有它各块会挤成一坨；分隔线长度固定（SEP），相邻两块不会糊在一起。
            lines = [
                "<b>⚽ FOOTBALL INSIGHT</b>",
                f"🏆 <code>{esc(p.league)} · {CommonView.fmt_time(p.kickoff, tz, '%m-%d %H:%M')}</code>",
                "",
                SEP,
                "",
                f"🏠 <b>{home}</b>",
                "　　　⚔️",
                f"✈️ <b>{away}</b>",
                "",
                SEP,
                "",
                "<b>🔮 比赛预测</b>",
                "",
                *prob_lines,
                "",
                f"🎯 预测结果　<b>{verdict}</b>",
                f"⚽ 预计比分　<b>{esc(a['best_score'])}</b>",
                f"💎 信心等级　<b>{level['emoji']} {level['name']}</b>",
                "",
                SEP,
                "",
                "<b>📊 核心数据</b>",
                "",
            ]

            # 核心数据：标签按显示宽度左对齐，数值统一放等宽块并用 | 分隔。
            # 标签列宽固定，数值起点才一致——直接写「预期进球　」这种全角空格，
            # 遇到 4 字与 5 字标签混排时仍会差一列。
            core_rows = [("⚽", "预期进球", f"{a['lambda_home']:.2f} | {a['lambda_away']:.2f}")]
            if p.has_team_data:
                core_rows.append((
                    "💪", "攻防强度",
                    f"主 {p.home_strength.attack_home:.2f} | 客 {p.away_strength.attack_away:.2f}",
                ))
            core_rows.append(("🧩", "数据完整性", esc(p.data_completeness)))
            for icon, label, value in core_rows:
                lines.append(kv_line(icon, label, value, CORE_LABEL_WIDTH))
    
            # 模型分析：结论式短句，不做长篇说明
            notes = cls._model_notes(p, home, away, verdict)
            if notes:
                lines += ["", SEP, "", "<b>📝 模型分析</b>", ""]
                lines += [f"• {n}" for n in notes]
    
            # 风险因素：与主卡的免责声明不同，这里说的是「本次预测具体缺什么」
            risks = cls.risk_bullets(p)
            if risks:
                lines += ["", SEP, "", "<b>⚠️ 风险因素</b>", ""]
                lines += [f"• {r}" for r in risks]
    
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
                    "",
                    SEP,
                    "",
                    "⚠️ <b>数据不足</b>：积分榜未包含这两支球队，",
                    "当前概率仅基于联赛平均估算，<b>不代表双方真实实力</b>。",
                ]
            if _is_fallback(p):
                lines += ["", "ℹ️ 备用数据源：仅基础比赛数据，赔率等高级统计不可用。"]
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
            # 列宽按显示宽度算，且表头与数据行必须同宽：原来表头用 12、
            # 「中位数」行用 10，两列直接错开两格。
            name_w, num_w = 12, 6
            header = (
                pad_cjk("博彩公司", name_w)
                + align_cjk("主", num_w, "right")
                + align_cjk("平", num_w, "right")
                + align_cjk("客", num_w, "right")
            )
            table = [header]
            for row in sorted(p.bookmakers, key=lambda r: str(r["bookmaker"]))[:6]:
                table.append(
                    pad_cjk(row["bookmaker"], name_w)
                    + f"{row['home']:>{num_w}.2f}{row['draw']:>{num_w}.2f}{row['away']:>{num_w}.2f}"
                )
            o = p.odds
            table.append(
                pad_cjk("中位数", name_w)
                + f"{o['home']:>{num_w}.2f}{o['draw']:>{num_w}.2f}{o['away']:>{num_w}.2f}"
            )
            compare_rows = []
            for key in OUTCOMES:
                entry = p.outcomes.get(key)
                if entry:
                    # 三项都放等宽块并右对齐补位：「+5.2%」与「-12.0%」宽度不同，
                    # 不补位的话后面的符号会被推得一前一后。
                    compare_rows.append(
                        f"<code>{OUTCOME_LABEL[key]:<2}</code>"
                        f"  模型 <code>{probs[key]:>5.1%}</code>"
                        f"  市场 <code>{1 / o[key]:>5.1%}</code>"
                        f"  偏差 <code>{entry['edge']:>+6.1%}</code>"
                    )
            blocks = [
                [title, CommonView.matchup(p, tz), BLANK],
                section("🏦", "各家公司赔率", f"<pre>{esc(chr(10).join(table))}</pre>"),
                section("📐", "模型 vs 市场 · 含抽水的隐含概率", *compare_rows),
                section(
                    "🧾", "抽水与样本",
                    f"庄家抽水约 <code>{overround(o):.1%}</code>（共 {o['n']} 家公司）",
                ),
            ]
            lines = section_join(blocks).split("\n")
            lines += [SEP, BLANK, DISCLAIMER]
            return "\n".join(lines)
