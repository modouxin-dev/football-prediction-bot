"""移动端行宽守门 / Mobile line-width guard.

为什么要有这个文件：此前所有视图的文案都是按「桌面浏览器里看着还行」写的，
实测在手机上大面积折行——最严重的是详情页横排对阵
「🏠 布莱顿 (Brighton & Hove Albion FC) 🆚 狼队 (Wolverhampton…) ✈️」宽 77 列，
折成三行后横排布局完全失去意义。

守门口径（实测得出，不靠估计）：
- 常见 360dp 手机屏，Telegram 消息气泡去掉内边距后约能放 18 个中文字符
  = **36 显示宽度**。超过就会折行。
- `<code>` / `<pre>` 等宽块**不受此限**：Telegram 对整块缩字号而不是折行，
  强行截断反而会破坏表格对齐。所以守门只作用于**不含等宽块的行**。
"""
from __future__ import annotations

import html as _html
import re

import pytest

from api_client import flatten_standings
from bot_handler import BotUI
from config import load_settings
from formatkit import (
    MOBILE_MAX_COLS,
    display_width,
    fit_cols,
    team_mobile,
    team_name,
    team_short_name,
)
from service import MODEL_VERSION, PredictionService
from tests.sample_data import fixture, odds_response, standings_response

ENV = {
    "TELEGRAM_TOKEN": "123456:TEST-TOKEN",
    "RAPID_API_KEY": "k",
    "CHAT_ID": "555",
    "SEASON": "2026",
    "ADMIN_ID": "555",
}
SETTINGS = load_settings(ENV)
TZ = SETTINGS.timezone

# 故意选最长的队名，测最坏情况而不是平均情况
LONG_PAIRS = [
    ("Brighton & Hove Albion FC", "Wolverhampton Wanderers FC"),
    ("Borussia Mönchengladbach", "Nottingham Forest FC"),
    ("Manchester United FC", "AFC Bournemouth"),
]

_TAG = re.compile(r"<[^>]+>")
# 等宽块：<code>...</code> 或 <pre>...</pre>
_MONO = re.compile(r"<(code|pre)>.*?</\1>", re.S)


def _plain(text: str) -> str:
    return _html.unescape(_TAG.sub("", text))


def _prose_lines(text: str) -> list[str]:
    """只剩「会折行的普通文本行」：先挖掉等宽块，再剥离标签。"""
    stripped = _MONO.sub("", text)
    return [ln for ln in _plain(stripped).split("\n") if ln.strip()]


def _longest_prose(text: str) -> int:
    lines = _prose_lines(text)
    return max((display_width(ln) for ln in lines), default=0)


# ---- 工具函数 ----------------------------------------------------------------

def test_display_width_counts_cjk_as_two():
    assert display_width("阿森纳") == 6
    assert display_width("Arsenal") == 7


def test_fit_cols_keeps_short_text_intact():
    assert fit_cols("阿森纳") == "阿森纳"
    assert fit_cols("Arsenal FC") == "Arsenal FC"


def test_fit_cols_truncates_with_ellipsis():
    out = fit_cols("Wolverhampton Wanderers FC", 10)
    assert out.endswith("…")
    assert display_width(out) <= 10


def test_fit_cols_never_pads():
    """fit_cols 只截断不补空格——补位属于 align_cjk 的职责。"""
    assert fit_cols("阿森纳", 20) == "阿森纳"


def test_team_mobile_prefers_chinese_short_name():
    assert team_mobile("Brighton & Hove Albion FC") == "布莱顿"
    assert team_mobile("Wolverhampton Wanderers FC") == "狼队"


def test_team_mobile_never_invents_chinese():
    """未收录的队宁可显示（截断的）英文原名，也不编造中文名。"""
    out = team_mobile("Ipswich Town FC")
    assert "Ipswich" in out
    assert display_width(out) <= 16


def test_team_mobile_differs_from_bilingual():
    """移动端队名 ≠ 双语队名：后者宽 37 列，手机上必折行。"""
    raw = "Brighton & Hove Albion FC"
    assert display_width(team_mobile(raw)) < display_width(team_name(raw))
    assert team_mobile(raw) == team_short_name(raw)


# ---- 视图守门 ----------------------------------------------------------------

def _mk_fixtures():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)
    out = []
    for i, (h, a) in enumerate(LONG_PAIRS):
        out.append(fixture(200 + i, i * 2 + 1, h, i * 2 + 2, a,
                           now + timedelta(hours=i + 1), status="NS"))
    return out


class _API:
    def __init__(self, items):
        self.items = items
        self.standings = flatten_standings(standings_response())
        self.odds = odds_response([("Bet365", 1.32, 5.0, 9.0),
                                   ("Pinnacle", 1.30, 5.2, 8.8)])

    async def get_fixtures(self, league_id, season, date_from, date_to):
        return self.items

    async def get_standings(self, league_id, season):
        return self.standings

    async def get_odds(self, fixture_id, fresh=False):
        return self.odds

    async def get_h2h(self, *a, **k):
        return []

    async def get_injuries(self, *a, **k):
        return []


@pytest.fixture(scope="module")
def pred():
    import asyncio

    items = _mk_fixtures()
    svc = PredictionService(SETTINGS, _API(items))
    return asyncio.run(svc.predict_fixture(200, items))


def test_matchup_line_uses_short_names(pred):
    """横排对阵若用双语全称实测 77 列，手机上折三行。"""
    text = _plain(BotUI.matchup(pred, TZ))
    assert "Brighton" not in text
    assert "Wolverhampton" not in text
    assert _longest_prose(BotUI.matchup(pred, TZ)) <= MOBILE_MAX_COLS


def test_prediction_card_lines_fit_mobile(pred):
    text = BotUI.format_prediction_card(pred, TZ, MODEL_VERSION)
    longest = _longest_prose(text)
    assert longest <= MOBILE_MAX_COLS, f"最长普通文本行 {longest} 列，手机上会折行"


def test_prediction_lines_fit_mobile(pred):
    text = BotUI.format_prediction(pred, TZ)
    longest = _longest_prose(text)
    assert longest <= MOBILE_MAX_COLS, f"最长普通文本行 {longest} 列，手机上会折行"


def test_deep_analysis_lines_fit_mobile(pred):
    text = BotUI.format_deep_analysis(pred, TZ)
    longest = _longest_prose(text)
    assert longest <= MOBILE_MAX_COLS, f"最长普通文本行 {longest} 列，手机上会折行"


def test_h2h_lines_fit_mobile(pred):
    text = BotUI.format_h2h(pred, [], TZ)
    assert _longest_prose(text) <= MOBILE_MAX_COLS


def test_welcome_and_menu_fit_mobile():
    assert _longest_prose(BotUI.format_welcome(SETTINGS)) <= MOBILE_MAX_COLS
    assert _longest_prose(BotUI.format_menu(SETTINGS)) <= MOBILE_MAX_COLS


def test_help_lines_fit_mobile():
    assert _longest_prose(BotUI.format_help()) <= MOBILE_MAX_COLS


def test_standings_lines_fit_mobile():
    rows = flatten_standings(standings_response())
    text = BotUI.format_standings_page(rows, TZ, 20, "英格兰超级联赛")
    assert _longest_prose(text) <= MOBILE_MAX_COLS


def test_disclaimer_keeps_compliance_core():
    """缩短后必须保留「不构成投注建议」这句合规核心。"""
    from templates import DISCLAIMER

    assert "不构成投注建议" in DISCLAIMER
    assert display_width(_plain(DISCLAIMER)) <= MOBILE_MAX_COLS
