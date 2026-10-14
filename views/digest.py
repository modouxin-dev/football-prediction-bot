"""每日预测汇总 / Daily digest

为什么单独做一个汇总视图，而不是把现有「逐场推送」改一改：

    用户的核心诉求是「所有联赛接入 + 每天一条清晰的汇总」。五大联赛加上
    欧战，周末一天能有 30+ 场，逐场推送意味着 30 条消息——在 Telegram 里
    这会连续触发 30 次通知，把聊天列表整个刷掉，也会把此前的内容顶没了
    （调研同类体育推送产品时，这被明确列为「垂直蔓延」的典型反面模式）。

    汇总把 30 条压成 1 条：按联赛分组、组内按开赛时间排序，扫一眼就知道
    今天有什么、哪场值得看。要细节再点进单场。

排版取舍（移动端优先）：

    - **一场两行**：第一行「时间 + 对阵」，第二行「结论 + 信心」。实测一行
      塞不下对阵和概率（36 列守门线），硬塞必然折行，折行后左右两列错位，
      反而比两行更难读。
    - **队名用移动端短名**：汇总里挂双语全称（"曼城 (Manchester City)"）
      会让每行宽度翻倍。短名在这里够用，全称留给单场详情。
    - **联赛用国旗 emoji 分组**：一屏十几条时，图标比文字标题定位更快。
      每条只挂一个图标，不堆砌。
    - **不用等宽块做横排表格**：Markdown/等宽表格在手机上普遍过宽不可读，
      这里统一用「图标 + 粗体 + 分隔符」的竖排结构。
"""

from __future__ import annotations

from typing import Any

from analyzer import calculate_prediction_level
from formatkit import BLANK, esc, fit_cols, fmt_time, league_label, team_mobile
from templates import (BRAND_EN, BULLET, DISCLAIMER, LEAGUE_FLAGS,
                       LEAGUE_NAMES, LEAGUE_ORDER, LEAGUE_SHORT,
                       OUTCOME_LABEL, SEP, THIN_SEP)

# 单场「对阵行」的守门宽度：时间(5) + 空格(1) + 两队名(各 12) + " vs "(5) ≈ 35。
# 与 formatkit.MOBILE_MAX_COLS=36 同源，这里略放宽是因为已经用了短名。
HEADLINE_MAX_COLS = 34

# Telegram 单条消息硬上限 4096 字符（含 HTML 标签）。这里留出页眉、页脚和
# 一点余量，避免刚好踩线导致整条推送 400 失败。
TELEGRAM_MAX_CHARS = 4096
PAGE_SOFT_LIMIT = 3600


def _league_id_of(p) -> int:
    """取比赛所属联赛 ID。

    优先读原始数据里的 league.id（多联赛下唯一可靠的分组依据）；
    缺失时退化成 0，汇总里归入「其它」而不是丢弃这场比赛——宁可分组
    难看，也不能让用户的比赛凭空消失。
    """
    try:
        return int((p.fixture.get("league") or {}).get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _league_sort_key(league_id: int) -> tuple[int, int]:
    """联赛先后：登记过的按 LEAGUE_ORDER，未登记的排最后并按 ID 升序。"""
    try:
        return (LEAGUE_ORDER.index(league_id), 0)
    except ValueError:
        return (len(LEAGUE_ORDER), league_id)


def _group_blocks(predictions, tz) -> list[tuple[int, list[str]]]:
    """按联赛分组并排序，返回 [(联赛ID, 该组的展示行), ...]。

    分组与排序的唯一实现：单页汇总与分页汇总都走这里，避免两处逻辑漂移。
    """
    grouped: dict[int, list[Any]] = {}
    for p in predictions:
        grouped.setdefault(_league_id_of(p), []).append(p)

    ordered_ids = sorted(grouped, key=_league_sort_key)
    blocks: list[tuple[int, list[str]]] = []
    for lid in ordered_ids:
        matches = sorted(grouped[lid], key=lambda p: p.kickoff)
        flag = LEAGUE_FLAGS.get(lid, "⚽")
        name = LEAGUE_NAMES.get(lid) or (league_label(lid) if lid else "其它")
        lines = [
            f"{flag} <b>{esc(name)}</b> <code>{len(matches)} 场</code>",
            THIN_SEP,
        ]
        for p in matches:
            lines.append(_headline(p, tz))
            lines.append(_verdict_line(p))
        blocks.append((lid, lines))
    return blocks


def _headline(p, tz) -> str:
    """一行对阵：「🕐 19:30 曼城 vs 利物浦」。"""
    time_text = fmt_time(p.kickoff, tz, "%H:%M")
    pair = f"{team_mobile(p.home)} vs {team_mobile(p.away)}"
    return f"🕐 {time_text} <b>{esc(fit_cols(pair, HEADLINE_MAX_COLS))}</b>"


def _probs_of(p) -> tuple[float, float, float]:
    """取 (主胜, 平局, 客胜) 概率，缺失按 0 处理。

    单独抽出来是因为完整列表与焦点战卡片都要读这三个数，两处各写一遍
    字段名（win_prob / draw_prob / loss_prob）迟早会写错一个。
    """
    a = p.analysis or {}
    return (
        float(a.get("win_prob", 0) or 0),
        float(a.get("draw_prob", 0) or 0),
        float(a.get("loss_prob", 0) or 0),
    )


def _outcome_meta(p) -> tuple[str, str, str, float]:
    """返回 (结论名, 信心 emoji, 信心等级单字, 最高概率)。"""
    home, draw, away = _probs_of(p)
    level = calculate_prediction_level(
        {"home_win": home, "draw": draw, "away_win": away}
    )
    # level["result"] 形如 "主胜"/"平局"/"客胜"，直接展示；拿不到就取概率最高项
    name = level.get("result") or max(
        (("home", home), ("draw", draw), ("away", away)),
        key=lambda kv: kv[1],
    )[0]
    if name not in ("主胜", "平局", "客胜"):
        name = OUTCOME_LABEL.get(name, name)
    return name, level.get("emoji", "⚪"), level.get("name", "—"), max(home, draw, away)


def _verdict_line(p) -> str:
    """一行结论：「▸ 主胜 52% · 🟢 高信心」。

    不再加 🚀：实测 edge 越大 ROI 越差，🚀 会把「模型与市场分歧大」
    误示成「值得关注的机会」。
    """
    name, emoji, level_name, pct = _outcome_meta(p)
    flag = ""
    best_edge = None
    if getattr(p, "best", None):
        try:
            best_edge = float(p.best[1].get("edge", 0))
        except (TypeError, ValueError, IndexError, AttributeError):
            best_edge = None
    if best_edge is not None and best_edge >= 0.07:
        flag = " ⚠️"  # 分歧大 = 模型更不可信，不是机会
    # LEVEL_META 里的 name 是单字（高/中/低），单独挂在句尾语义不完整，
    # 这里补成「高信心」——单字「中」容易被误读成「中场」之类。
    return f"{BULLET} {esc(name)} {pct:.0%} · {emoji} {esc(level_name)}信心{flag}"


# ---- 焦点战（推送首屏）----------------------------------------------------
# 焦点战场次上限。按钮区每场独占一行，3 场是「看得见的重点」与「不把推送
# 撑成长列表」的平衡点；再多就失去了「总览一眼扫完」的意义。
FOCUS_LIMIT = 3

FOCUS_ICON = "🔥"
OTHERS_ICON = "📌"

# 按钮文案显示宽度上限：Telegram 按钮在手机上一行约 20 个半角列，超过会
# 被截断成「…」。中文按 2 列算，display_width / fit_cols 已处理。
BUTTON_MAX_COLS = 20


def _focus_key(p) -> tuple[tuple[int, int], Any]:
    """焦点战排序键：先联赛优先级，再开赛时间。

    选「焦点战」必须用确定性口径，不能依赖热度/强度打分——那些指标在缺
    数据时会退化成随机顺序，同一天两次推送可能选出不同的三场，用户会以为
    推送在乱跳。联赛优先级（LEAGUE_ORDER）+ 开赛时间完全确定、可复现。
    """
    return (_league_sort_key(_league_id_of(p)), p.kickoff)


def _pick_focus(predictions, limit: int = FOCUS_LIMIT) -> list[Any]:
    return sorted(predictions, key=_focus_key)[:limit]


def _button_label(p) -> str:
    """按钮文案：「⚽ 曼城 vs 阿森纳」，两侧各限 7 列后整体限 20 列。"""
    pair = f"{team_mobile(p.home, 7)} vs {team_mobile(p.away, 7)}"
    return f"⚽ {fit_cols(pair, BUTTON_MAX_COLS)}"


def _league_title(lid: int) -> str:
    """联赛小标题，未登记的回退到数据源名，绝不丢弃。"""
    flag = LEAGUE_FLAGS.get(lid, "⚽")
    name = (LEAGUE_SHORT.get(lid) or LEAGUE_NAMES.get(lid)
            or (league_label(lid) if lid else "其它"))
    return f"{flag} <b>{esc(name)}</b>"


def _focus_lines(focus, tz) -> list[str]:
    """焦点战区块：联赛小标题 + 每场三行（对阵 / 时间与结论 / 概率三连）。"""
    lines: list[str] = []
    cur = None
    for p in focus:
        lid = _league_id_of(p)
        if lid != cur:  # 只在切换联赛时打标题，避免同一联赛重复打印
            cur = lid
            lines.append(_league_title(lid))
        home, draw, away = _probs_of(p)
        name, emoji, _, pct = _outcome_meta(p)
        pair = f"{team_mobile(p.home)} vs {team_mobile(p.away)}"
        lines.append(f"⚽ <b>{esc(fit_cols(pair, HEADLINE_MAX_COLS))}</b>")
        lines.append(
            f"🕐 {fmt_time(p.kickoff, tz, '%H:%M')} | {emoji} {esc(name)} {pct:.0%}"
        )
        # 概率三连进 <code>：等宽下三列对齐，正文比例字体里百分号会参差
        lines.append(f"📊 <code>{home:.0%} — {draw:.0%} — {away:.0%}</code>")
        lines.append(BLANK)
    return lines


def _others_summary(rest) -> str:
    """其他场次一行概览：「🇪🇸西甲 3 场 · 🇩🇪德甲 2 场」。

    只给联赛 + 场数，不逐场列出：逐场列等于把完整列表搬进推送，那
    「剩余 N 场」和「点按钮看完整赛程」就自相矛盾了。
    """
    grouped: dict[int, int] = {}
    for p in rest:
        lid = _league_id_of(p)
        grouped[lid] = grouped.get(lid, 0) + 1
    parts = []
    for lid in sorted(grouped, key=_league_sort_key):
        flag = LEAGUE_FLAGS.get(lid, "⚽")
        name = (LEAGUE_SHORT.get(lid) or LEAGUE_NAMES.get(lid)
                or (league_label(lid) if lid else "其它"))
        parts.append(f"{flag}{esc(name)} {grouped[lid]} 场")
    return " · ".join(parts)


class DigestView:
    @staticmethod
    def focus_entries(predictions) -> list[tuple[int, str]]:
        """焦点战按钮数据：[(fixture_id, 按钮文案), ...]。

        拿不到 fixture_id 的场次直接跳过而不是塞一个假 id——按钮指向不存在的
        比赛比少一个按钮更难排查。
        """
        entries: list[tuple[int, str]] = []
        for p in _pick_focus(predictions or ()):
            fid = getattr(p, "fixture_id", None)
            if fid is None:
                continue
            try:
                entries.append((int(fid), _button_label(p)))
            except (TypeError, ValueError):
                continue
        return entries

    @staticmethod
    def format_daily_overview(predictions, settings, tz, *, note: str | None = None) -> str:
        """精简总览：焦点战 3 场 + 其余折叠成一行。

        与 format_daily_digest 的分工：

        - 本函数是**推送首屏**，恒定短小（因此不需要分页），让人一眼看到
          今天最值得看的几场；
        - format_daily_digest(_pages) 是**完整列表**，由「📋 完整赛程」
          按钮进入。

        为什么拆开：全部逐场平铺时，30 场的推送要滑很久才能看完，而大多
        数人只想扫一眼重点。把列表收进按钮，推送才有「总览」感，按钮数量
        也从「每场两个」降到「每焦一场一个 + 底部一行」。
        """
        if not predictions:
            return DigestView.format_daily_digest([], settings, tz, note=note)

        focus = _pick_focus(predictions)
        # 用 id() 而不是 `p not in focus`：Prediction 未定义 __eq__，列表
        # 成员判断会退化成逐项 ==，既慢又可能在将来加了 __eq__ 后误判。
        focus_ids = {id(p) for p in focus}
        rest = [p for p in predictions if id(p) not in focus_ids]

        total = len(predictions)
        day_label = fmt_time(min(p.kickoff for p in predictions), tz, "%m月%d日")
        lines = [
            "⚽ <b>今日预测汇总</b>",
            f"<code>{BRAND_EN} · DAILY</code>",
            SEP,
            BLANK,
            f"📅 {day_label} · 共 <b>{total}</b> 场 · <b>{len({_league_id_of(p) for p in predictions})}</b> 个联赛",
        ]
        if note:
            lines += [BLANK, f"<i>{esc(note)}</i>"]
        lines.append(BLANK)
        lines.append(f"{FOCUS_ICON} <b>焦点战</b>")
        lines += _focus_lines(focus, tz)
        if rest:
            lines.append(f"{OTHERS_ICON} <b>其他场次</b>")
            lines.append(_others_summary(rest))
            lines.append(f"📋 剩余 <b>{len(rest)}</b> 场，点下方按钮查看完整赛程")
            lines.append(BLANK)
        lines += [SEP, DISCLAIMER]
        return "\n".join(lines)

    @staticmethod
    def format_daily_digest(predictions, settings, tz, *, note: str | None = None) -> str:
        """按联赛分组的单条汇总。

        空列表时返回「今日无比赛」而不是空白消息——空白会让用户以为机器人
        坏了，而真实原因（国际比赛日 / 套餐不支持该赛季）必须说出来。
        """
        if not predictions:
            lines: list[str] = [
                "⚽ <b>今日预测汇总</b>",
                f"<code>{BRAND_EN} · DAILY</code>",
                SEP,
                BLANK,
                "📭 当前时间范围内暂无比赛。",
            ]
            if note:
                lines += [BLANK, f"<i>{esc(note)}</i>"]
            lines += [BLANK, SEP, DISCLAIMER]
            return "\n".join(lines)

        blocks = _group_blocks(predictions, tz)
        ordered_ids = [lid for lid, _ in blocks]
        total = len(predictions)
        day_label = fmt_time(min(p.kickoff for p in predictions), tz, "%m月%d日")

        lines = [
            "⚽ <b>今日预测汇总</b>",
            f"<code>{BRAND_EN} · DAILY</code>",
            SEP,
            BLANK,
            f"📅 {day_label} · 共 <b>{total}</b> 场 · <b>{len(ordered_ids)}</b> 个联赛",
        ]
        if note:
            lines += [BLANK, f"<i>{esc(note)}</i>"]
        lines.append(BLANK)

        # ---- 逐联赛输出 -----------------------------------------------------
        for _, block in blocks:
            lines += block
            lines.append(BLANK)

        lines += [SEP, DISCLAIMER]
        return "\n".join(lines)

    @staticmethod
    def format_daily_digest_pages(predictions, settings, tz, *, note: str | None = None,
                                  limit: int = PAGE_SOFT_LIMIT) -> list[str]:
        """分页版汇总：返回可直接发送的字符串列表。

        Telegram 单条消息上限 4096 字符。五大联赛周末一天 30+ 场，一场两行
        很容易撑爆——超限时 send 会直接 400 失败，而这是无人值守的定时推送，
        失败了没人知道。所以这里按**联赛块**切页（绝不从一场中间切断），
        每页自带页眉页脚，独立可读。

        只有一页时不加页码，与老格式完全一致。
        """
        if not predictions:
            return [DigestView.format_daily_digest([], settings, tz, note=note)]

        blocks = _group_blocks(predictions, tz)
        total = len(predictions)
        day_label = fmt_time(min(p.kickoff for p in predictions), tz, "%m月%d日")
        footer = [SEP, DISCLAIMER]

        # 先切块：按联赛累积，加入下一块会超限就翻页
        chunks: list[list[str]] = []
        cur: list[str] = []
        for _, lines in blocks:
            block = lines + [BLANK]
            # 预留页眉页脚的量，否则最后一页拼完才发现超限
            if cur and len("\n".join(cur + block + footer)) > limit:
                chunks.append(cur)
                cur = []
            cur.extend(block)
        if cur:
            chunks.append(cur)
        if not chunks:  # 极端情况：单个联赛就超限，也至少发一页
            chunks = [[]]

        count = len(chunks)

        def head(page_no: int) -> list[str]:
            suffix = f"（{page_no}/{count}）" if count > 1 else ""
            lines = [
                f"⚽ <b>今日预测汇总{suffix}</b>",
                f"<code>{BRAND_EN} · DAILY</code>",
                SEP,
                BLANK,
            ]
            if page_no == 1:
                lines.append(
                    f"📅 {day_label} · 共 <b>{total}</b> 场 · <b>{len(blocks)}</b> 个联赛"
                )
                if note:
                    lines += [BLANK, f"<i>{esc(note)}</i>"]
            lines.append(BLANK)
            return lines

        return ["\n".join(head(i) + body + footer) for i, body in enumerate(chunks, 1)]

    @staticmethod
    def format_digest_empty(note: str | None = None) -> str:
        """无比赛时的兜底文案（供调度器单独调用）。"""
        return DigestView.format_daily_digest([], None, None, note=note)
