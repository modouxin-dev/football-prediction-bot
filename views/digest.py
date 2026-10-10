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

from analyzer import calculate_prediction_level
from formatkit import (
    BLANK,
    esc,
    fit_cols,
    fmt_time,
    league_label,
    team_mobile,
)
from templates import (
    BRAND_EN,
    BULLET,
    DISCLAIMER,
    LEAGUE_FLAGS,
    LEAGUE_NAMES,
    LEAGUE_ORDER,
    OUTCOME_LABEL,
    SEP,
    THIN_SEP,
)

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
    grouped: dict[int, list] = {}
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


def _verdict_line(p) -> str:
    """一行结论：「▸ 主胜 52% · 🟢 高信心」。

    不再加 🚀：实测 edge 越大 ROI 越差，🚀 会把「模型与市场分歧大」
    误示成「值得关注的机会」。
    """
    a = p.analysis or {}
    probs = {
        "home_win": float(a.get("win_prob", 0) or 0),
        "draw": float(a.get("draw_prob", 0) or 0),
        "away_win": float(a.get("loss_prob", 0) or 0),
    }
    level = calculate_prediction_level(
        {"home_win": probs["home_win"], "draw": probs["draw"], "away_win": probs["away_win"]}
    )
    # level["result"] 形如 "主胜"/"平局"/"客胜"，直接展示；拿不到就取概率最高项
    name = level.get("result") or max(
        (("home", probs["home_win"]), ("draw", probs["draw"]), ("away", probs["away_win"])),
        key=lambda kv: kv[1],
    )[0]
    if name not in ("主胜", "平局", "客胜"):
        name = OUTCOME_LABEL.get(name, name)
    pct = max(probs.values())
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
    return (
        f"{BULLET} {esc(name)} {pct:.0%} · "
        f"{level.get('emoji', '⚪')} {esc(level.get('name', '—'))}信心{flag}"
    )


class DigestView:
    @staticmethod
    def format_daily_digest(predictions, settings, tz, *, note: str | None = None) -> str:
        """按联赛分组的单条汇总。

        空列表时返回「今日无比赛」而不是空白消息——空白会让用户以为机器人
        坏了，而真实原因（国际比赛日 / 套餐不支持该赛季）必须说出来。
        """
        if not predictions:
            lines = [
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

        lines: list[str] = [
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
