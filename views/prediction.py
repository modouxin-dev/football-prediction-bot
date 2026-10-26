"""预测视图 / Prediction views

本文件内的文案与生成它的逻辑同在一处，改文案只动这一个文件。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from analyzer import (OUTCOMES, calculate_prediction_level,
                      implied_probabilities, market_gap, overround)
from formatkit import (BLANK, _is_fallback, align_cjk, bar, esc, hbar, kv_line,
                       pad_cjk, section, section_join, team_mobile)
from service import MODEL_VERSION
from templates import DISCLAIMER, OUTCOME_LABEL, SEP, VALUE_HIGH, VALUE_LOW
from views.common import CommonView

# 三种结果对应的图标。键是视图内部使用的中文标签，与 OUTCOME_LABEL 的值一致。
# 放在模块级而不是类里：views/ 下多个模块都要用，且它是纯常量不会变。
OUTCOME_ICON = {"主胜": "🏠", "平局": "🤝", "客胜": "✈️"}

# 核心数据区的标签列宽（按显示宽度算，中文占 2 列）。
# 取最长标签「最可能比分」的宽度 10 再加 2 列间隔：若只取 10，5 字标签会
# 与数值贴死，看上去像没分隔。
CORE_LABEL_WIDTH = 12


class PredictionView:
    @staticmethod
    def format_stored_card(row: dict, tz) -> str:
        """存档预测卡：直接展示库里已落盘的结论，不重新计算。

        为什么单独一个渲染入口：赛程快照是后来才加的列，更早的预测只有结论、
        没有快照；比赛开赛或跨天后今日赛程里也查不到，于是旧卡片一点就报
        「该场比赛已不在今日赛程中」。可结论明明就在库里。
        用户点旧卡片的目的是**回看已算过的结论**，不是重算一遍，
        所以这里绕过模型与赛程，直接用落盘数字渲染。

        与 format_prediction_card 的关系：那个吃完整 Prediction 对象、
        依赖积分榜与近期战绩；这个只吃一行落盘数据，字段少一个就少显示一项，
        绝不补占位数字。
        """
        home = esc(team_mobile(row.get("home") or "?"))
        away = esc(team_mobile(row.get("away") or "?"))
        hp, dp, ap = row["home_prob"], row["draw_prob"], row["away_prob"]

        lines = [f"🏟 <b>{home} vs {away}</b>", SEP, BLANK]

        # 开赛时间：存的是 UTC ISO，转成用户时区；解析失败就只显示原值，不编造
        when = row.get("kickoff") or ""
        shown = when
        try:
            dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            shown = dt.astimezone(tz).strftime("%m-%d %H:%M")
        except Exception:
            pass
        if row.get("league"):
            lines.append(f"🏆 {esc(row['league'])}")
        if shown:
            lines.append(f"🕐 {esc(shown)}")
        lines.append(BLANK)

        # 三合一概率：数值 + 条形，与预测主卡同一套字形
        lines.append("📊 <b>模型预测（存档）</b>")
        for label, prob in (("主胜", hp), ("平局", dp), ("客胜", ap)):
            icon = OUTCOME_ICON.get(label, "")
            lines.append(f"{icon} {label} {prob * 100:.0f}%  {hbar(prob)}")
        lines.append(BLANK)

        top_label, top_prob = max(
            (("主胜", hp), ("平局", dp), ("客胜", ap)), key=lambda kv: kv[1])
        lines.append(f"🎯 <b>最可能结果：{top_label}（{top_prob * 100:.0f}%）</b>")
        if row.get("best_score"):
            lines.append(f"⚽ 最可能比分：{esc(row['best_score'])}")
        lines.append(BLANK)

        # 已结算的比赛顺带给出实际比分与命中情况——这本来就是用户最想回看的
        ah, aw = row.get("actual_home"), row.get("actual_away")
        if ah is not None and aw is not None:
            lines.append(f"✅ <b>实际比分：{esc(ah)}-{esc(aw)}</b>")
            if row.get("result"):
                lines.append(f"📌 预测结果：{esc(row['result'])}")
            lines.append(BLANK)

        lines.append("🗂 本条为历史存档，赛程已过期，无法重新计算或深度分析。")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    @staticmethod
    def format_prediction(p, tz) -> str:
        a = p.analysis
        probs = (("主胜", a["win_prob"]), ("平局", a["draw_prob"]), ("客胜", a["loss_prob"]))

        title = f"🏆 <b>{esc(p.league)}</b>" + (f" · {esc(p.round_label)}" if p.round_label else "")
        when = f"🕐 <code>{CommonView.fmt_time(p.kickoff, tz)}</code> ({CommonView.tz_label(tz, p.kickoff)})"
        # 场馆独占一行：与「时间 + 时区」挤在一行实测 38 列，手机上必折行；
        # 分成两行各约 24 列，且不再需要截断场馆名（信息完整保留）。
        venue_line = f"🏟 <code>{esc(p.venue)}</code>" if p.venue else None

        top_label, top_prob = max(probs, key=lambda kv: kv[1])

        lines = [
            title,
            when,
            *([venue_line] if venue_line else []),
            BLANK,
            SEP,
            BLANK,
            # 对阵块：居中一个 ⚔️，两行队名，比挤在一行更容易扫读
            f"🏠 <b>{esc(team_mobile(p.home))}</b>",
            "　　　⚔️",
            f"✈️ <b>{esc(team_mobile(p.away))}</b>",
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
        # ---- 亚洲盘 + 比分推荐（项目主推方向）----
        # 放在胜平负之后、赔率之前：它是「推荐」，比参考性的赔率更要紧。
        ah = getattr(p, "asian", None)
        lines += [BLANK, SEP, BLANK, "🎯 <b>亚洲盘推荐</b>", SEP]
        if ah:
            line = ah["line"]
            # 盘口线按主队视角展示：负=主队让球
            line_txt = f"{line:+.2f}".rstrip("0").rstrip(".")
            h_side, a_side = ah["home"], ah["away"]
            # 推荐哪一边：按模型赢盘概率与赔率隐含概率的差（edge）选
            pick = "home" if h_side["edge"] >= a_side["edge"] else "away"
            pick_side = h_side if pick == "home" else a_side
            pick_name = esc(team_mobile(p.home)) if pick == "home" else esc(team_mobile(p.away))
            lines.append(
                kv_line("🏷", "盘口", f"主队 {line_txt} · {ah['n']}家", CORE_LABEL_WIDTH))
            lines.append(
                kv_line("📌", "推荐",
                        f"<b>{pick_name} {pick_side['odds']:.2f}</b>",
                        CORE_LABEL_WIDTH))
            lines.append(
                kv_line("📊", "赢盘概率",
                        f"{pick_side['prob']:.1%} · 偏差 {pick_side['edge']:+.1%}",
                        CORE_LABEL_WIDTH))
            # 走水提示：整数盘/平手盘存在退本金的情形，必须说清
            if abs(line) < 1e-9 or abs(line % 1.0) < 1e-9:
                lines.append("<i>　打平走水</i>")
        else:
            lines.append(kv_line("🏷", "盘口", "未开盘亚盘", CORE_LABEL_WIDTH))
            lines.append("<i>　不予推荐</i>")

        # 比分推荐：直接取泊松矩阵 top1，与「最可能比分」同源，不自相矛盾
        top_scores = (p.analysis.get("top_scores") or [])[:3]
        if top_scores:
            lines += [BLANK, SEP, BLANK, "🎯 <b>比分推荐</b>", SEP]
            lines.append(kv_line("🥇", "首选", esc(top_scores[0][0]), CORE_LABEL_WIDTH))
            if len(top_scores) > 1:
                alt = " · ".join(f"{sc}" for sc, _ in top_scores[1:])
                lines.append(kv_line("🥈", "备选", esc(alt), CORE_LABEL_WIDTH))

        if p.best and p.odds:
            key, o = p.best
            # 不打 🚀 Value Bet 标记：1110 场实测按 edge 阈值下注的 ROI 恒为负
            # （>0% -11.94%、>5% -16.26%、>10% -17.85%、>15% -20.61%），
            # 标记只会暗示「有便宜可捡」，与实测结论相反。
            # 赔率与偏差也进核心数据的标签列：与主卡不同，这里两者都是
            # 同一节内的补充指标，列宽对齐后才不会比上面的行突出一格。
            lines.append(
                kv_line("💰", "赔率",
                        f"{p.odds['home']:.2f} | {p.odds['draw']:.2f} | {p.odds['away']:.2f}"
                        f" ·{p.odds['n']}家",
                        CORE_LABEL_WIDTH)
            )
            # 价值偏差的值含 <b> 与 🚀，不能整行进 <code>（不支持嵌套），
            # 因此标签沿用等宽块、值留在外层，两侧标签同宽故起点一致
            lines.append(
                f"🔎 <code>{pad_cjk('模型差异', CORE_LABEL_WIDTH)}</code>"
                f"<b>{OUTCOME_LABEL[key]}</b> <code>{o['edge']:+.2%}</code>"
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
        # 措辞不再暗示「值得下注」：edge 是模型与市场的差异，
        # 实测该差异越大模型反而越不准（见 docs/MARKET_FUSION_EVAL.md）。
        if o["edge"] >= VALUE_HIGH:
            return f"{OUTCOME_LABEL[key]}（模型高于市场 {o['edge']:.0%}）"
        if o["edge"] >= VALUE_LOW:
            return f"{OUTCOME_LABEL[key]}（模型略高于市场）"
        return "与市场基本一致，观望"

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
            reason = (p.inputs or {}).get("odds_error")
            if reason:
                # 订阅了会员却一直显示「暂无赔率」时，用户无法判断是套餐不含、
                # 未开盘还是限流。这里把真实原因显示出来（/diag 可复现探测）。
                # 截断：风险提示每行有手机宽度上限，原因过长会折行。
                lines.append(f"未取到赔率：{str(reason)[:16]}")
            else:
                lines.append("暂无赔率数据，未做价值偏差对比。")
        # 伤停、赛程密度确实不参与概率计算（/analysis 可查看伤停名单）。
        # 原措辞「未计入伤停」会被读成「没拿到伤停数据」，与订阅能力无关，故改口径。
        lines.append("伤停等临场因素不参与概率计算。")
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
        home, away = esc(team_mobile(p.home)), esc(team_mobile(p.away))
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

        # 一行紧凑概率：三行进度条适合细看，但转发、截图、快速扫一眼时
        # 需要「48% — 28% — 24%」这种能整段引用的形式。挂在结论行尾部，
        # 不额外占行——正文已经很长，再加一行会让手机上要多滑一屏。
        compact_probs = " — ".join(f"{prob:.0%}" for _, prob in probs)

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
            f"🎯 预测结果　<b>{verdict}</b>　<code>{compact_probs}</code>",
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

        # 市场对照：主卡的概率是融合口径（线上含 75% 市场信息），不给市场
        # 基准就无法判断这个数字里有多少是模型自己的判断——差值才是模型
        # 真正贡献的部分。只放对照块，各家公司报价留在 format_odds_detail：
        # 主卡已有 40+ 行，再塞 6 行会让手机上多滑一屏。
        compare = cls.market_compare_block(p)
        if compare:
            lines += ["", SEP, "", "<b>💰 市场对照</b>", "", *compare]

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

    @classmethod
    def market_compare_block(cls, p, note: str | None = None) -> list[str]:
        """模型 vs 市场（去水后）对照块，主卡与赔率详情页共用。

        为什么抽成共用实现：
            主卡与详情页各写一套差值算法时，一旦出现「主卡 +5.1pp、
            详情页 +2.3pp」这种不一致，两边单看都对，只有互相比才发现，
            而用户只会认为这个系统不可信。

        为什么必须去水：
            1/赔率含抽水，三项和 > 1。直接拿它当市场概率会把市场算得比
            实际更自信，模型与市场的差距因此被系统性放大。

        为什么差值用 pp：
            写成 +5.1% 会被读成「概率 5.1%」，而它实际是「比市场高
            5.1 个百分点」。

        返回空列表表示不可比（无赔率或报价无效），调用方自行决定如何提示，
        本函数不替调用方编造文案。
        """
        if not p.odds:
            return []
        a = p.analysis
        probs = {"home": a["win_prob"], "draw": a["draw_prob"], "away": a["loss_prob"]}
        market = implied_probabilities(p.odds)
        gap = market_gap(probs, p.odds)
        # gap 为空或 market 为空都表示报价无效（如含 0 或负数），此时不渲染。
        if not gap or not market:
            return []
        label_w, num_w = 12, 8
        header = pad_cjk("", label_w) + "".join(
            align_cjk(OUTCOME_LABEL[k], num_w, "right") for k in OUTCOMES
        )
        model_row = pad_cjk("模型", label_w) + "".join(
            align_cjk(f"{probs[k]:.1%}", num_w, "right") for k in OUTCOMES
        )
        market_row = pad_cjk("市场(去水)", label_w) + "".join(
            align_cjk(f"{market[k]:.1%}", num_w, "right") for k in OUTCOMES
        )
        gap_row = pad_cjk("差值", label_w) + "".join(
            align_cjk(f"{gap[k] * 100:+.1f}pp", num_w, "right") for k in OUTCOMES
        )
        biggest = max(gap, key=lambda k: abs(gap[k]))
        block = [
            f"<pre>{esc(chr(10).join([header, model_row, market_row, gap_row]))}</pre>",
            f"最大分歧 <b>{OUTCOME_LABEL[biggest]}</b> "
            f"<code>{gap[biggest] * 100:+.1f}pp</code>",
        ]
        if note:
            block.append(note)
        return block

    @staticmethod
    def format_odds_detail(p, tz) -> str:
        title = "💰 <b>赔率对比</b>"
        if not p.odds:
            return f"{title}\n{CommonView.matchup(p, tz)}\n{SEP}\n暂无赔率数据（该场比赛可能尚未开盘）。"
        a = p.analysis
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
        # 对照块与单场预测卡共用同一实现（market_compare_block），
        # 保证两处显示的差值永远一致。
        compare_rows = PredictionView.market_compare_block(
            p,
            note="ℹ️ 上表为<b>对比口径</b>（已去抽水）；Value Bet 判据仍用"
                 "<b>含抽水赔率</b>算期望收益，两者不可互换。",
        )
        if not compare_rows:
            compare_rows.append("暂无可比数据（赔率缺失或含无效报价）。")
        blocks = [
            [title, CommonView.matchup(p, tz), BLANK],
            section("🏦", "各家公司赔率", f"<pre>{esc(chr(10).join(table))}</pre>"),
            section("📐", "模型 vs 市场 · 去水后", *compare_rows),
            section(
                "🧾", "抽水与样本",
                f"庄家抽水约 <code>{overround(o):.1%}</code>（共 {o['n']} 家公司）",
            ),
        ]
        lines = section_join(blocks).split("\n")
        lines += [SEP, BLANK, DISCLAIMER]
        return "\n".join(lines)
