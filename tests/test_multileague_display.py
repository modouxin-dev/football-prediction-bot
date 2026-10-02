"""多联赛展示层测试：菜单 / 运行状态要列出全部已启用联赛，不能只显示主联赛。

核心保证（防误导）：
    开了三个联赛，界面只显示英超，用户会以为另外两个没生效。
"""
import asyncio

from bot_handler import BotUI
from config import load_settings
from formatkit import league_label, leagues_label

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}


def settings_of(league_ids=None, league_id=39):
    env = dict(ENV)
    env["LEAGUE_ID"] = str(league_id)
    if league_ids is not None:
        env["LEAGUE_IDS"] = ",".join(str(i) for i in league_ids)
    return load_settings(env)


# ---- leagues_label -----------------------------------------------------------
def test_leagues_label_single_matches_league_label():
    """单联赛时输出与旧版完全一致。"""
    assert leagues_label((39,)) == league_label(39)


def test_leagues_label_joins_all():
    text = leagues_label((39, 140, 78))
    assert "39" in text and "140" in text and "78" in text
    assert text.count("/") == 2


def test_leagues_label_dedupes():
    assert leagues_label((39, 140, 39)) == leagues_label((39, 140))


def test_leagues_label_empty_is_explicit():
    assert leagues_label(()) == "未配置"


def test_leagues_label_ignores_bad_value():
    assert leagues_label((39, "abc", None)) == league_label(39)


# ---- 菜单 ---------------------------------------------------------------------
def test_menu_shows_all_enabled_leagues():
    text = BotUI.format_menu(settings_of([39, 140]))
    assert "39" in text and "140" in text


def test_menu_single_league_unchanged():
    text = BotUI.format_menu(settings_of(None))
    assert "39" in text and "140" not in text


# ---- 运行状态 -----------------------------------------------------------------
def test_status_line_lists_all_leagues():
    from types import SimpleNamespace

    from tests.test_bot import command_update, make_app

    from commands import admin as admin_cmds

    app, _, _ = make_app(settings=settings_of([39, 140]), warm=False)
    update, msg = command_update()
    asyncio.run(admin_cmds.status_cmd(update, SimpleNamespace(application=app)))
    text = msg.replies[-1]
    assert "39" in text and "140" in text
