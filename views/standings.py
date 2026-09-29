"""积分榜视图 / Standings view

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

class StandingsView:
        @staticmethod
        @staticmethod
        def format_standings_page(rows: list[dict], tz, limit: int = 20, league_name: str = "", updated=None) -> str:
            """联赛排名：前 3 名做成数据卡片突出重点，其余用紧凑单行，避免 20 行糊成一片。

            参数名用 league_name 而非 league_label：后者是 formatkit 导出的**函数**，
            同名会在本作用域内把它遮蔽掉（pyflakes W0404 场景），
            一旦函数体内想调用 league_label(...) 就会拿到字符串而崩溃。
            """
            lines = ["🏆 <b>联赛排名</b>"]
            if league_name:
                lines.append(f"<code>{esc(league_name)}</code>")
            if updated is not None:
                lines.append(
                    f"🕑 <code>UPDATED: {CommonView.fmt_time(updated, tz, '%m-%d %H:%M')}</code>"
                )
            lines.append(SEP)
            if not rows:
                lines.append(NO_DATA)
                lines += [SEP, DISCLAIMER]
                return "\n".join(lines)
    
            shown = rows[:limit]
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            for i, row in enumerate(shown, start=1):
                summary = _row_summary(row)
                name = esc(team_name((row.get("team") or {}).get("name")))
                rank = (summary or {}).get("rank") or i
                pts = (summary or {}).get("points")
                points = f"{pts}分" if pts is not None else "-"  # 无积分时不显示孤立的「分」字
                record = f"{summary['win']}-{summary['draw']}-{summary['lose']}" if summary else "-"
                avg_for = f"{summary['avg_for']:.2f}" if summary and summary["avg_for"] is not None else "-"
                avg_against = (
                    f"{summary['avg_against']:.2f}"
                    if summary and summary["avg_against"] is not None else "-"
                )
                if i <= 3:
                    # 前三：两行卡片，积分大字突出
                    lines.append(f"{medals[i]} <b>{name}</b>　<b>{points}</b>")
                    lines.append(f"└ <code>{record}</code> · 进 {avg_for} / 失 {avg_against}")
                else:
                    # 其余：紧凑单行，靠点线引导视线，避免堆成表格
                    lines.append(f"<code>{rank:>2}</code> {name} <b>{points}</b>　<code>{record}</code>")
            lines += [SEP, DISCLAIMER]
            return "\n".join(lines)
