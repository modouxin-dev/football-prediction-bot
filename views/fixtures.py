"""赛程视图 / Fixtures view

本文件内的文案与生成它的逻辑同在一处，改文案只动这一个文件。
"""

from __future__ import annotations

from datetime import datetime

from analyzer import OUTCOMES, calculate_prediction_level, overround
from service import MODEL_VERSION, parse_kickoff
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
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

class FixturesView:
        @staticmethod
        def format_fixtures_page(
            items: list[dict], tz, page: int = 0, per_page: int = 5, day_label: str = "",
            multi_day: bool = False,
        empty_range: tuple[str, str] | None = None,
        empty_source_ok: bool = True,
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
                f"📆 <code>{esc(day_label)}</code> · 🌍 <code>{esc(tz.zone)}</code>",
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
                current = None
                for offset, fx in enumerate(chunk):
                    idx = page * per_page + offset + 1
                    info = fx.get("fixture") or {}
                    league = (fx.get("league") or {}).get("name") or "未知联赛"
                    if league != current:  # 按联赛分组，只在切换联赛时打印标题
                        current = league
                        lines.append(f"🏆 <b>{esc(league)}</b>")
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
                        when = local.strftime("%m-%d %H:%M") if multi_day else local.strftime("%H:%M")
                    else:
                        when = "--:--"
                    short = (info.get("status") or {}).get("short") or ""
                    status = STATUS_TEXT.get(short, short or "未知")
                    goals = fx.get("goals") or {}
                    gh, ga = goals.get("home"), goals.get("away")
                    # 比分两侧都要空格：只有左侧有空格会渲染成「2-1狼队」，
                    # 比分与客队名直接粘连。⚔️ 分支前后都有空格，所以只有
                    # 已完场的比赛会暴露这个 bug —— 未开赛时看不出来。
                    score = f" <b>{esc(gh)}-{esc(ga)}</b> " if gh is not None and ga is not None else " ⚔️ "
                    # 序号用等宽 2 位补位：个位数与两位数在比例字体下会错开半格
                    # 每场独立成块：序号+时间一行、对阵一行、状态一行，块间空行。
                    # 挤在一行时队名长的比赛会被折行，序号和状态就对不上了。
                    lines.append(f"<code>{idx:>2}</code> ⏰ <code>{when}</code>")
                    lines.append(f"　　🏠 {esc(home)}{score}{esc(away)}")
                    lines.append(
                        f"　　 {status_emoji(short)} <code>{esc(status)}</code>"
                        f" · <code>#{esc(info.get('id'))}</code>"
                    )
                    lines.append(BLANK)
                    rows.append(
                        [
                            InlineKeyboardButton(f"⚽ 预测 {idx}", callback_data=f"fx:{info.get('id')}"),
                            InlineKeyboardButton(f"📊 分析 {idx}", callback_data=f"fa:{info.get('id')}"),
                        ]
                    )
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
