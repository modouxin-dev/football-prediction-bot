"""菜单与帮助 / Menu & help

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
    VALUE_HIGH,
    VALUE_LOW,
)
from formatkit import (
    BLANK,
    _factors,
    _form_line,
    _is_fallback,
    _is_fallback_source,
    _num,
    _row_line,
    _row_summary,
    bar,
    display_width,
    esc,
    kv_line,
    league_label,
    leagues_label,
    pad_cjk,
    section,
    section_join,
    team_name,
    web_entry_text,
)

class MenuView:
        @staticmethod
        def format_welcome(settings) -> str:
            """/start 欢迎文案：科技仪表盘风格（配套品牌头图，头图已含品牌名）。
    
            注意：Telegram 消息区不是等宽字体，因此这里只用「短边框 + 参数面板」
            这类容错较高的符号；主视觉由品牌头图承担，避免长边框错位。
            """
            return (
                f"👋 <b>欢迎使用 {BRAND_CN}</b>\n"
                f"<code>{BRAND_EN}</code> · AI 赛事情报\n"
                "\n"
                "用泊松分布拆解每一场比赛，\n"
                "让预测可量化、可追溯。\n"
                "\n"
                f"◆ <b>引擎参数</b>\n"
                f"{THIN_SEP}\n"
                f"▍模型　<code>Poisson Distribution</code>\n"
                f"▍数据　赛程 · 积分榜 · 赔率\n"
                f"▍输出　概率 · 信心评级 · 图表\n"
                "\n"
                f"◆ <b>核心功能</b>\n"
                f"{THIN_SEP}\n"
                f"│ 📅 今日赛程\n"
                f"│ ⚽ 比赛预测\n"
                f"│ 📊 深度分析\n"
                f"│ 🏆 联赛排名\n"
                f"└ 📈 数据图表\n"
                "\n"
                f"{SEP}\n"
                f"⏰ 每日 <code>{settings.push_time:%H:%M}</code>（{esc(CommonView.tz_label(settings.timezone))}）自动推送\n"
                "💡 点击下方菜单，或发送 /menu 开始"
            )
    
        @staticmethod
        def format_menu(settings) -> str:
            return (
                "⚽ <b>Football Insight</b>\n"
                "AI 赛事情报 · 数据驱动洞察\n"
                f"{SEP}\n"
                "\n"
                "⚙️ <b>运行环境</b>\n"
                f"{THIN_SEP}\n"
                f"│ 联赛　<code>{esc(leagues_label(getattr(settings, 'league_ids', None) or (settings.league_id,)))}</code>\n"
                f"│ 赛季　<code>{settings.season}</code>\n"
                f"└ 时区　<code>{esc(settings.timezone.zone)}</code>\n"
                "\n"
                f"{SEP}\n"
                "\n"
                "📌 <b>请选择功能</b>"
            )
    
        @staticmethod
        def format_help() -> str:
            # 功能与命令各成一节。命令名放进等宽块并按最长命令补位：
            # 「/start」与「/standings」长度不同，不补位说明文字会参差。
            features = [
                ("📅", "今日赛程", "当日比赛，支持翻页"),
                ("⚽", "比赛预测", "胜平负、比分与信心"),
                ("📊", "深度分析", "状态、主客场、交锋"),
                ("🏆", "联赛排名", "积分榜与攻防数据"),
                ("🔄", "刷新数据", "清空缓存，重新拉取"),
                ("🌐", "网页端", "浏览器查询入口"),
            ]
            # 整行进 <code>：功能名 4 字为主、说明长短不一，只有等宽块能对齐
            feature_rows = [kv_line(icon, name, desc, 10) for icon, name, desc in features]
            commands = [
                ("/start", "欢迎与推送时间"),
                ("/menu", "功能菜单"),
                ("/help", "本指南"),
                ("/fixtures", "今日赛程"),
                ("/predict", "比赛预测"),
                ("/standings", "联赛排名"),
                ("/refresh", "刷新数据"),
                ("/web", "网页端"),
                ("/test", "立即推送（管理员）"),
                ("/status", "状态诊断（管理员）"),
            ]
            cmd_w = max(len(c) for c, _ in commands) + 1
            command_rows = [f"<code>{pad_cjk(cmd, cmd_w)}</code>{desc}" for cmd, desc in commands]

            blocks = [
                ["ℹ️ <b>使用帮助</b>", BLANK],
                section("🧭", "功能说明", *feature_rows),
                section("⌨️", "命令列表 / Commands", *command_rows),
            ]
            lines = section_join(blocks).split("\n")
            lines += [SEP, BLANK, DISCLAIMER]
            return "\n".join(lines)
    
        @staticmethod
        def format_coming(key: str) -> str:
            label = dict(MENU_ITEMS).get(key, key)
            return (
                f"🚧 <b>{esc(label)}</b>\n"
                f"{SEP}\n"
                "\n"
                "该功能正在开发中，将在后续阶段上线。\n"
                "\n"
                f"{SEP}\n"
                "📌 可先使用其他功能，或发送 /help 查看完整说明。"
            )
    
        @staticmethod
        def error_hint(exc) -> str:
            """把 APIError 翻译成人话，区分 Key / 套餐 / 限流 / 赛季 / 网络等原因。"""
            text = str(exc).lower()
            if "do not have access to this season" in text or "free plan" in text:
                return "🔎 原因：当前订阅套餐不支持该赛季（Free 套餐通常只开放 2022–2024），需升级套餐或改用可访问的赛季。"
            if "rate limit" in text or "429" in text or "too many" in text:
                return "🔎 原因：请求次数已超限，请稍后再试或升级套餐。"
            if "403" in text or "not subscribed" in text or "invalid" in text:
                return "🔎 原因：API Key 无效或未订阅该接口，请检查 Key 与订阅状态。"
            if "401" in text or "unauthor" in text:
                return "🔎 原因：API Key 鉴权失败，请检查 Key 是否正确。"
            if "timeout" in text or "timed out" in text or "network" in text or "connect" in text:
                return "🔎 原因：网络异常或数据源超时，请稍后重试。"
            return "🔎 如持续出现，请检查 API Key、套餐权限与网络连接。"
