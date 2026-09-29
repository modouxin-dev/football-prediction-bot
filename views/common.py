"""公共展示原语 / Common presentation primitives

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
    bar,
    esc,
    fmt_time as _fmt_time,
    league_label,
    team_name,
    web_entry_text,
)

class CommonView:
        @staticmethod
        def web_text(web_url: str = "") -> str:
            """/web 入口文案：按是否配置了 WEB_URL 动态生成。"""
            return web_entry_text(web_url)
    
        @staticmethod
        def tz_label(tz, when: datetime | None = None) -> str:
            when = (when or datetime.now(tz)).astimezone(tz)
            hours = when.utcoffset().total_seconds() / 3600
            return f"UTC{hours:+g}"
    
        @staticmethod
        def fmt_time(dt: datetime, tz, pattern: str = "%m-%d %H:%M") -> str:
            return dt.astimezone(tz).strftime(pattern)
    
        @staticmethod
        def matchup(p, tz) -> str:
            """详情页顶部的一行比赛信息。"""
            return (
                f"🏠 <b>{esc(team_name(p.home))}</b> 🆚 <b>{esc(team_name(p.away))}</b> ✈️\n"
                f"🕐 <code>{CommonView.fmt_time(p.kickoff, tz)}</code> ({CommonView.tz_label(tz, p.kickoff)})"
            )
    
        @staticmethod
        # 概率条：用方块横向条，不用特殊符号堆叠
        @staticmethod
        def _hbar(prob: float, width: int = 10) -> str:
            filled = max(0, min(width, round(prob * width)))
            return "█" * filled + "░" * (width - filled)
