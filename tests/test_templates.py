"""欢迎卡片 / 主菜单 / 使用帮助 的模板测试：结构、HTML 合法性与移动端可读性。"""
import re

import pytest

from bot_handler import BotUI, league_label
from config import load_settings

SETTINGS = load_settings(
    {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555",
     "SEASON": "2026", "ADMIN_ID": "555"}
)

TEMPLATES = {
    "welcome": lambda: BotUI.format_welcome(SETTINGS),
    "menu": lambda: BotUI.format_menu(SETTINGS),
    "help": lambda: BotUI.format_help(),
    "coming": lambda: BotUI.format_coming("web"),
}


# ---- 内容完整性 ---------------------------------------------------------------
def test_welcome_shows_brand_and_push_time():
    text = BotUI.format_welcome(SETTINGS)
    assert "足球量化预测机器人" in text
    assert "08:00" in text  # 推送时间
    for item in ("今日赛程", "比赛预测", "深度分析", "联赛排名", "数据图表"):
        assert item in text, item


def test_menu_shows_league_season_timezone():
    text = BotUI.format_menu(SETTINGS)
    assert "39" in text and "2026" in text and "Asia/Shanghai" in text


def test_help_lists_features_and_commands():
    text = BotUI.format_help()
    assert "使用帮助" in text
    for cmd in ("/start", "/menu", "/help", "/test", "/status"):
        assert cmd in text, cmd
    for feature in ("今日赛程", "比赛预测", "深度分析", "联赛排名", "刷新数据", "网页端"):
        assert feature in text, feature


def test_coming_page_keeps_label_and_guidance():
    text = BotUI.format_coming("fixtures")
    assert "今日赛程" in text
    assert "/help" in text  # 给出下一步指引，而不是死胡同


# ---- HTML 合法性（Telegram 严格解析，标签不配对会整体发送失败）----------------------
@pytest.mark.parametrize("name", list(TEMPLATES))
def test_html_tags_are_balanced(name):
    text = TEMPLATES[name]()
    for tag in ("b", "i", "code", "pre"):
        assert text.count(f"<{tag}>") == text.count(f"</{tag}>"), f"{name}: <{tag}> 不配对"


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_no_unescaped_raw_angle_brackets(name):
    """除合法标签外，不得出现裸露的 < 或 >（如球队名未转义）。"""
    text = TEMPLATES[name]()
    stripped = re.sub(r"</?(b|i|u|s|code|pre|a|tg-spoiler)[^>]*>", "", text)
    assert "<" not in stripped and ">" not in stripped


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_no_markdown_table_syntax(name):
    """移动端表格兼容性差：不得出现表头分隔行或以 | 开头的行。"""
    text = TEMPLATES[name]()
    assert "---" not in text
    assert not [ln for ln in text.split("\n") if ln.strip().startswith("|")]


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_line_count_reasonable_for_mobile(name):
    """单屏可读性：模板不宜过长（<= 30 行）。"""
    text = TEMPLATES[name]()
    assert text.count("\n") + 1 <= 30


# ---- 联赛名展示 -----------------------------------------------------------------
def test_league_label_known_and_unknown():
    assert "英格兰超级联赛" in league_label(39)
    assert "39" in league_label(39)
    assert league_label(9999) == "联赛 9999"  # 未知 ID 不猜测
