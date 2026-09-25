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
    assert "08:00" in text  # 推送时间
    for item in ("今日赛程", "比赛预测", "深度分析", "联赛排名", "数据图表"):
        assert item in text, item


def test_brand_name_lives_in_banner_not_text():
    """品牌主视觉由头图承担，文案里不再重复大标题（避免图片+文字重复冗长）。"""
    try:
        import chart
    except ImportError:
        pytest.skip("matplotlib 未安装（可选依赖）")
    import inspect

    src = inspect.getsource(chart.brand_banner)
    assert "FOOTBALL QUANT" in src  # 品牌名在头图默认参数中
    assert "足球量化预测机器人" not in BotUI.format_welcome(SETTINGS)


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


# ---- 品牌头图 -------------------------------------------------------------------
def test_brand_banner_generates_png():
    """头图必须能生成 PNG；matplotlib 缺失时返回 None 而不是抛异常。"""
    try:
        import chart
    except ImportError:
        pytest.skip("matplotlib 未安装（可选依赖）")

    data = chart.brand_banner()
    if chart.FONT_IN_USE or True:  # 无中文字体时仍应出图（英文部分正常）
        assert data, "品牌头图生成失败"
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG"


def test_brand_banner_accepts_custom_text():
    try:
        import chart
    except ImportError:
        pytest.skip("matplotlib 未安装（可选依赖）")
    assert chart.brand_banner(title="TEST", subtitle="sub", tagline="tag")


# ---- 欢迎文案：仪表盘风格结构 -----------------------------------------------------
def test_welcome_has_dashboard_panels():
    text = BotUI.format_welcome(SETTINGS)
    assert "引擎参数" in text
    assert "Poisson" in text
    assert "核心功能" in text
    assert "08:00" in text


def test_welcome_uses_short_lines_to_avoid_misalignment():
    """非等宽字体下长边框会错位，因此不使用长 ━━/┃ 边框作为正文主体。"""
    text = BotUI.format_welcome(SETTINGS)
    for line in text.split("\n"):
        assert line.count("┃") == 0  # 不使用竖向长边框
        assert line.count("━") <= 18  # 分隔线保持短（SEP 本身长度 18）
