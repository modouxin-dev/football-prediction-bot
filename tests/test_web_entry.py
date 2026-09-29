"""网页端入口 / Web entry (Stage 5).

/web 必须给出**能打开的真实地址**；未部署时宁可明说，
也不给一条点开是 404 的链接。
"""
import pytest

from bot_handler import BotUI, web_entry_text


def test_no_url_says_not_deployed():
    """未配置 WEB_URL 时，必须说明未部署并给出启用步骤。"""
    text = web_entry_text("")
    assert "未部署" in text
    assert "WEB_URL" in text


def test_no_url_does_not_contain_broken_link():
    """绝不能出现 http 开头的假链接。"""
    assert "http" not in web_entry_text("")


def test_with_url_returns_clickable_link():
    text = web_entry_text("https://bot.example.com")
    assert "https://bot.example.com" in text
    assert "未部署" not in text


def test_url_is_html_escaped():
    """URL 来自环境变量，必须转义后才能放进 HTML 消息。"""
    text = web_entry_text("https://x.com/?a=1&b=2")
    assert "&amp;" in text


def test_botui_method_matches_module_function():
    assert BotUI.web_text("https://a.b") == web_entry_text("https://a.b")


def test_botui_default_fallback_exists():
    """保留类属性兜底，兼容既有引用。"""
    assert isinstance(BotUI.WEB_ENTRY_TEXT, str) and BotUI.WEB_ENTRY_TEXT


# ---- 配置解析 ---------------------------------------------------------------

def test_web_url_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("WEB_URL", raising=False)
    from config import load_settings

    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("RAPID_API_KEY", "k")
    monkeypatch.setenv("CHAT_ID", "1")
    assert load_settings().web_url == ""


def test_web_url_trailing_slash_is_stripped(monkeypatch):
    from config import load_settings

    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("RAPID_API_KEY", "k")
    monkeypatch.setenv("CHAT_ID", "1")
    monkeypatch.setenv("WEB_URL", "https://bot.example.com/")
    assert load_settings().web_url == "https://bot.example.com"
