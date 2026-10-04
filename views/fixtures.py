"""赛程视图 / Fixtures view

本文件内的文案与生成它的逻辑同在一处，改文案只动这一个文件。"""

from __future__ import annotations

from datetime import datetime, timedelta

# 日期导航条显示的天数：4 天是手机上不换行的上限，再多只能滚动。
DAY_NAV_SPAN = 4

from analyzer import OUTCOMES, calculate_prediction_level, overround
from service import MODEL_VERSION, parse_kickoff
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from templates import (
    BRAND_CN,
    LEAGUE_FLAGS,
    LEAGUE_NAMES,
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
    align_cjk,
    display_width,
    BLANK,
    bar,
    esc,
    hbar,
    league_label,
    status_emoji,
    team_name,
    team_short_name,
    web_entry_text,
)

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# ---- 赛程表格列宽（显示列，中文 = 2 列）----------------------------------
# 一行 = 序号(2) + 空格 + 时间(5) + 空格 + 队名(side) + " vs " + 队名(side)
#      + 空格 + 进度。手机上 <pre> 等宽块太宽就会横向滚动，一滚动
# 「日期时间 / 对阵 / 进度」三列就散了，对齐也就白做了 —— 所以队名列宽
# 按本页实际内容自适应：短名页面只占 32 列，遇到 5 字中文队名才撑到 40。
SIDE_MIN = 6     # 队名列下限：3 个中文字，再窄就没法看了
SIDE_MAX = 10    # 队名列上限：5 个中文字。超出的（长英文名）截断，
                 # 否则一行会被单个队名撑爆，整页都跟着横向滚动


def _side_col(names) -> int:
    """按本页最长队名决定列宽，钳在 [SIDE_MIN, SIDE_MAX]。"""
    widest = max((display_width(n) for n in names), default=0)
    return max(SIDE_MIN, min(SIDE_MAX, widest))


def _relative_day(day, tz) -> str:
    """相对日期标签：今天/明天/后天，再往后回退到星期。

    按钮上放不下「今天 10-04」（10 列），所以把相对标签挪到标题行——
    标题没有宽度限制，信息一点不丢。
    """
    offset = (day - datetime.now(tz).date()).days
    if offset == 0:
        return "今天"
    if offset == 1:
        return "明天"
    if offset == 2:
        return "后天"
    return WEEKDAY_CN[day.weekday()]


def _day_nav_row(tz, active_day):
    """生成横排日期导航：今天起连续 4 天，active_day 打勾标记。

    按钮上只放 MM-DD（6 列）。此前写「今天 10-04」共 10 列，而手机上
    4 个按钮平分一行、每格仅约 8 列，文案被截成「今天 10-…」——恰恰
    是要看的日期被挤没了。改成纯日期后完整可见，相对标签移到标题行。
    """
    base = datetime.now(tz).date()
    row = []
    for offset in range(DAY_NAV_SPAN):
        day = base + timedelta(days=offset)
        label = day.strftime("%m-%d")
        if active_day == day:
            label = f"✅{label}"
        row.append(InlineKeyboardButton(label, callback_data=f"fxd:{offset}"))
    return row


def _progress_text(short: str, goals: dict | None) -> str:
    """进度列文本：已完场给比分（最有用），进行中给状态，未开始给「未开始」。

    列宽只有 6 列，所以「中场休息」「时间待定」这类四字状态要缩短，
    否则会被截成「中场休…」，反而看不懂。
    """
    if short in ("FT", "AET", "PEN"):
        gh = (goals or {}).get("home")
        ga = (goals or {}).get("away")
        return f"{gh}-{ga}" if gh is not None and ga is not None else "已完场"
    if short in ("1H", "2H", "ET", "BT", "P", "LIVE"):
        return "进行中"
    if short == "HT":
        return "中场"
    if short == "TBD":
        return "待定"
    return STATUS_TEXT.get(short) or "未知"


def _match_cell(home: str, away: str, side: int) -> str:
    """对阵列「主队 vs 客队」：两侧各自补到固定列宽，中间的 vs 永远在同一列。

    为什么不整串拼接后统一补位：那样 vs 的位置会随队名长度左右漂移，
    扫视时两场比赛的对阵就对不齐。固定两侧列宽后，视觉上是一条竖线。
    """
    return f"{align_cjk(home, side)} vs {align_cjk(away, side)}"


def _table_row(idx: int, when: str, home: str, away: str, progress: str, side: int) -> str:
    """一行赛程。整行进 <pre> 才有等宽：正文是比例字体，补多少空格都对不齐。

    进度列不补位：它是最后一列，补的空格在行尾既看不见也会被渲染器 trim，
    但会白白占掉宽度、把队名挤压得更窄。
    """
    cell = _match_cell(home, away, side)
    # 先按显示宽度补齐、再转义：esc 只改变源码字符数（& -> &amp;），
    # 不改变渲染后的列宽，反过来做会让含 & 的队名（Brighton & Hove）少一列。
    return esc(f"{idx:>2} {when} {cell} {progress}")


class FixturesView:
        @staticmethod
        def format_fixtures_page(
            items: list[dict], tz, page: int = 0, per_page: int = 5, day_label: str = "",
            multi_day: bool = False,
        empty_range: tuple[str, str] | None = None,
        empty_source_ok: bool = True,
        active_day=None,
        ) -> tuple[str, InlineKeyboardMarkup, int, int]:
            """按联赛分组渲染一页赛程。返回 (文本, 键盘, 实际页码, 总页数)。
    
            multi_day=True 表示这批赛程跨越多天（今日无比赛时扩展到未来），
            此时标题改为「近期赛程」，且每场比赛显示日期，避免用户误以为是今天的比赛。
            """
            total_pages = max(1, -(-len(items) // per_page))
            page = min(max(page, 0), total_pages - 1)
            chunk = items[page * per_page : (page + 1) * per_page]
    
            title = "📅 <b>近期赛程</b>" if multi_day else "📅 <b>今日赛程</b>"
            # 相对标签（今天/明天）放标题：按钮上放不下，标题没有宽度限制。
            rel_day = active_day if active_day is not None else datetime.now(tz).date()
            lines = [
                title,
                f"📆 <code>{esc(day_label)}</code> · {_relative_day(rel_day, tz)}"
                f" · 🌍 <code>{esc(tz.zone)}</code>",
                SEP,
            ]
            rows: list[list[InlineKeyboardButton]] = []
            if not chunk:
                # 空状态要说清三件事：没比赛、查了哪段时间、数据源是否正常。
                # 绝不能把「窗口内没比赛」说成「数据源无数据」。
                lines.extend([
                    "🔭 <b>NO FIXTURE IN THIS WINDOW</b>",
                    "📅 当前时间范围内暂无比赛",
                    SEP,
                ])
                if empty_source_ok:
                    lines.append("✅ 数据源正常，已查询：")
                else:
                    lines.append("⚠️ 数据源返回异常，已查询：")
                span_text = (
                    f"{empty_range[0]} 至 {empty_range[1]}" if empty_range
                    else esc(day_label)
                )
                lines.append(f"<code>{esc(span_text)}</code>")
                lines += ["", "你可以尝试："]
            else:
                # 列宽按本页最长队名自适应：短名页面不浪费宽度，遇到
                # 5 字中文队名（里奥夸尔托）才撑到 SIDE_MAX，避免一刀切
                # 按最宽值把每行都拉长——那样短名页面会空出一大片空白。
                def _names():
                    for f in chunk:
                        t = f.get("teams") or {}
                        yield team_short_name((t.get("home") or {}).get("name"))
                        yield team_short_name((t.get("away") or {}).get("name"))

                side = _side_col(list(_names()))

                buf: list[str] = []

                def flush_table():
                    """把累积的数据行收成一个 <pre> 块。

                    整块进 <pre> 才有等宽：Telegram 正文是比例字体，补多少
                    空格都对不齐，三列会各自漂移。
                    """
                    if buf:
                        lines.append("<pre>" + "\n".join(buf) + "</pre>")
                        buf.clear()

                current = None  # None 与任何联赛 id 都不同，首场必打印标题
                prev_day = None
                for offset, fx in enumerate(chunk):
                    idx = page * per_page + offset + 1
                    info = fx.get("fixture") or {}
                    try:
                        lid = int((fx.get("league") or {}).get("id"))
                    except (TypeError, ValueError):
                        lid = 0
                    if lid != current:  # 按联赛分组，只在切换联赛时打印标题
                        flush_table()
                        prev_day = None  # 新联赛块内重新按日期分组
                        current = lid
                        # 标题用中文联赛名而不是数据源的英文名（API 返回的是
                        # "Liga Profesional Argentina" 之类，中文用户扫视时
                        # 定位不了）。未登记的联赛回退数据源原名，绝不丢弃。
                        flag = LEAGUE_FLAGS.get(lid, "⚽")
                        name = LEAGUE_NAMES.get(lid) or (
                            (fx.get("league") or {}).get("name") or f"联赛 {lid}"
                        )
                        # 场数按「本页内该联赛」统计，翻页时不会误导
                        count = sum(
                            1 for f in chunk
                            if (int((f.get("league") or {}).get("id") or 0)
                                if str((f.get("league") or {}).get("id") or "").lstrip("-").isdigit()
                                else 0) == lid
                        )
                        lines.append(f"{flag} <b>{esc(name)}</b> <code>{count} 场</code>")
                        lines.append(THIN_SEP)
                    teams = fx.get("teams") or {}
                    # 列表是扫视场景，只放中文短名：双语全名实测中位数 25 列、
                    # 最长 41 列（门兴格拉德巴赫 (Borussia Mönchengladbach)），
                    # 在手机上必然折行，折行后序号与队名就对不上了。
                    # 全名仍在单场预测卡里完整呈现，此处不重复占位。
                    home = team_short_name((teams.get("home") or {}).get("name"))
                    away = team_short_name((teams.get("away") or {}).get("name"))
                    kickoff = parse_kickoff(info.get("date"))
                    if kickoff:
                        local = kickoff.astimezone(tz)
                        when = local.strftime("%H:%M")
                        day = local.date()
                    else:
                        when = "--:--"
                        day = None
                    # 跨天时先打一行日期小标题，行内就只留时间。若把
                    # 「10-07 21:45」硬塞进 5 列的时间列，对阵列会被压到
                    # 14 列，两侧队名各只剩 5 列——中文字被砍一半。
                    if multi_day and day is not None and day != prev_day:
                        prev_day = day
                        if buf:
                            buf.append("")
                        buf.append(f"{day.strftime('%m-%d')} {WEEKDAY_CN[day.weekday()]}")
                    short = (info.get("status") or {}).get("short") or ""
                    goals = fx.get("goals") or {}
                    # 日期时间 · 主队 vs 客队 · 进度，三列固定列宽对齐
                    buf.append(_table_row(idx, when, home, away, _progress_text(short, goals), side))
                    rows.append(
                        [
                            InlineKeyboardButton(f"⚽ 预测 {idx}", callback_data=f"fx:{info.get('id')}"),
                            InlineKeyboardButton(f"📊 分析 {idx}", callback_data=f"fa:{info.get('id')}"),
                        ]
                    )
                flush_table()
            # 日期导航条：主流赛程站的标配。没有它，用户想看明天只能
            # 手打 /date 命令——而「明天有什么球」恰恰是最常见的需求。
            # 横排 4 天，当前所在日期打勾，一眼能看出自己停在哪一天。
            rows.append(_day_nav_row(tz, active_day))

            if not chunk:
                # 空状态直接给可继续操作的按钮，避免用户以为系统坏了
                rows.append([
                    InlineKeyboardButton("⏭ 查询下一场", callback_data="fxm:next"),
                    InlineKeyboardButton("📅 未来 7 天", callback_data="fxm:upcoming"),
                    InlineKeyboardButton("📆 指定日期", callback_data="fxm:date"),
                ])
            if total_pages > 1:
                rows.append(
                    [
                        InlineKeyboardButton("⬅️ 上一页", callback_data=f"fxp:{max(0, page - 1)}"),
                        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"),
                        InlineKeyboardButton("下一页 ➡️", callback_data=f"fxp:{min(total_pages - 1, page + 1)}"),
                    ]
                )
            if chunk:  # 空态已单独给出「下一场/未来 7 天」，不重复
                rows.append([
                    InlineKeyboardButton("📊 赛程图表", callback_data="chart:schedule:all"),
                    InlineKeyboardButton("⏭ 下一场", callback_data="fxm:next"),
                ])
            rows.append([InlineKeyboardButton("↩️ 返回主菜单", callback_data="menu:home")])
            return "\n".join(lines), InlineKeyboardMarkup(rows), page, total_pages
