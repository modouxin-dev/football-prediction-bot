"""赛程列表排版：短名 + 比分不粘连（P1-美化步骤 2）"""

from __future__ import annotations

import re

import pytest

from bot_handler import BotUI
from config import load_settings
from formatkit import team_name, team_short_name

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def _fx(i: int, home: str, away: str, date: str, short: str = "NS",
        gh: int | None = None, ga: int | None = None) -> dict:
    return {
        "fixture": {"id": 1000 + i, "date": date, "status": {"short": short}},
        "league": {"name": "Premier League", "id": 39},
        "teams": {"home": {"name": home, "id": 1}, "away": {"name": away, "id": 2}},
        "goals": {"home": gh, "away": ga},
    }


def _plain(html: str) -> str:
    """剥掉 HTML 标签，只看用户真正读到的文本。"""
    return re.sub(r"<[^>]+>", "", html).replace("&amp;", "&")


def test_list_uses_short_name_not_bilingual():
    """赛程列表只显示中文短名，不出现「中文 (English)」这种双语长串。

    双语名实测中位数 25 列、最长 41 列，在手机上必然折行，
    折行后序号与队名对不上，反而更难读。
    """
    items = [_fx(1, "Arsenal FC", "Chelsea FC", "2026-10-03T18:30:00+00:00")]
    text, _, _, _ = BotUI.format_fixtures_page(items, SETTINGS.timezone, 0, 5, "d")
    plain = _plain(text)
    assert "阿森纳" in plain and "切尔西" in plain
    assert "Arsenal FC" not in plain  # 列表里不应再出现英文全称


def test_score_has_space_on_both_sides():
    """比分两侧都要有空格：只补左侧会渲染成「2-1狼队」。

    「⚔️」分支前后都带空格，所以只有已完场的比赛会暴露这个 bug，
    未开赛时完全看不出来 —— 必须单独用已完场的数据测。
    """
    items = [_fx(1, "Brighton & Hove Albion FC", "Wolverhampton Wanderers FC",
                 "2026-10-03T14:00:00+00:00", "FT", 2, 1)]
    text, _, _, _ = BotUI.format_fixtures_page(items, SETTINGS.timezone, 0, 5, "d")
    plain = _plain(text)
    # 已完场的比分独占行尾的「进度」列，与客队名之间必须有空格分隔，
    # 否则会渲染成「狼队2-1」这种粘连。
    assert re.search(r"狼队\s+2-1\s*$", plain, re.M), plain


def test_unknown_team_keeps_original_name():
    """未收录的队显示英文原名，绝不编造中文名。"""
    items = [_fx(1, "Ipswich Town FC", "Arsenal FC", "2026-10-03T18:30:00+00:00")]
    text, _, _, _ = BotUI.format_fixtures_page(items, SETTINGS.timezone, 0, 5, "d")
    plain = _plain(text)
    # 未收录：保留英文原名（超长的会被截到队名列宽，但绝不编造中文名）
    assert "Ipswich" in plain
    assert "伊普斯" not in plain
    assert "阿森纳" in plain            # 已收录：中文名


def test_line_width_stays_readable():
    """整页最长行不得超过 40 字符，超过就会在手机上折行。

    改动前实测 74 字符（双语队名），改动后 33。
    """
    items = [
        _fx(1, "Manchester City FC", "Nottingham Forest FC", "2026-10-03T18:30:00+00:00"),
        _fx(2, "Brighton & Hove Albion FC", "Wolverhampton Wanderers FC",
            "2026-10-03T21:00:00+00:00", "FT", 2, 1),
        _fx(3, "Ipswich Town FC", "Tottenham Hotspur FC", "2026-10-04T14:00:00+00:00"),
    ]
    text, _, _, _ = BotUI.format_fixtures_page(items, SETTINGS.timezone, 0, 5, "d")
    longest = max(len(line) for line in _plain(text).split("\n") if line.strip())
    assert longest <= 40, f"最长行 {longest} 字符，会折行"


def test_team_short_name_differs_from_bilingual_false():
    """team_short_name 与 team_name(bilingual=False) 语义相反，必须锁住。

    bilingual=False 是「关闭双语、返回数据源原名」，实测会把 Arsenal FC
    原样输出成英文；team_short_name 才是「返回中文短名」。名字相近行为相反，
    一旦有人混用，列表会静默退回长英文名且没有报错。
    """
    assert team_short_name("Arsenal FC") == "阿森纳"
    assert team_name("Arsenal FC", bilingual=False) == "Arsenal FC"
    assert team_name("Arsenal FC") == "阿森纳 (Arsenal FC)"


def test_team_short_name_handles_empty_and_unknown():
    assert team_short_name(None) == "?"
    assert team_short_name("") == "?"
    assert team_short_name("  ") == "?"
    assert team_short_name("Some Unknown FC") == "Some Unknown FC"
