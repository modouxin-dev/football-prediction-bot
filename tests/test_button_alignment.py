"""按钮等宽 + 联赛按钮点了必须有比赛。

两个真实缺陷，先写测试复现为红再改，不靠推测：

1. 底部面板按钮宽度随文案长短变化。实测 MENU_ITEMS 九个标签的显示宽度
   是 11/15/11/11/11/11/11/9/11——「生成今日预测」比「网页端」宽出 6 列，
   resize_keyboard 按内容定宽，面板因此高矮不齐。
2. 切日期/切模式时旧的联赛筛选不清除。筛了英超再点「明天」，fx_league
   仍是 39，而明天没有英超——页面整片空白，看起来就是「联赛按钮点了没
   比赛」。既然按钮是从当前这批比赛里生成的，点了就不该是空的。
"""
from __future__ import annotations

import asyncio
import html as _html
import re

import pytz

from formatkit import display_width
from keyboards import Keyboards
from views.fixtures import FixturesView

TZ = pytz.timezone("Asia/Shanghai")


def _run(coro):
    """全量运行时已有测试会关掉默认事件循环，这里必须新建。"""
    return asyncio.run(coro)


def _fx(lid: int, lname: str, home: str, away: str, i: int, date: str) -> dict:
    return {
        "fixture": {"id": i, "date": date, "status": {"short": "NS"}},
        "league": {"id": lid, "name": lname},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": None, "away": None},
    }


def _pool() -> list[dict]:
    """五个联赛各一场：联赛筛选行会出现 5 个按钮。"""
    return [
        _fx(39, "Premier League", "Man City", "Liverpool", 101, "2026-10-04T19:00:00+00:00"),
        _fx(140, "La Liga", "Real Madrid", "Barcelona", 102, "2026-10-04T20:00:00+00:00"),
        _fx(78, "Bundesliga", "Bayern", "Dortmund", 103, "2026-10-04T21:00:00+00:00"),
        _fx(135, "Serie A", "Juventus", "Inter", 104, "2026-10-04T22:00:00+00:00"),
        _fx(61, "Ligue 1", "Paris SG", "Marseille", 105, "2026-10-04T23:00:00+00:00"),
    ]


def _plain(text: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", text))


# ---- 1. 底部面板等宽 ---------------------------------------------------------

def test_reply_menu_labels_have_uniform_width():
    """底部面板所有按钮显示宽度必须一致。

    resize_keyboard=True 按内容定宽：文案长的按钮就比同行的宽一截，
    九个按钮排下来高矮不齐，看着像没排过版。
    """
    kb = Keyboards.reply_menu_keyboard(expanded=True)
    labels = [b.text for row in kb.keyboard for b in row if b.text != "✖️ 收起面板"]
    widths = {display_width(x) for x in labels}
    assert len(labels) >= 6
    assert len(widths) == 1, f"按钮宽度不齐：{sorted(widths)}"


def test_padded_panel_button_still_resolves_to_menu_key():
    """补齐后按钮必须仍能反查菜单键。

    底部面板是靠文字匹配触发的（点按钮等于发送该文字）。补齐用的全角空格
    若没被 strip 掉，反查就会失配——按钮变成点了没反应，比不等宽严重得多。
    """
    from main import _menu_key_from_text

    kb = Keyboards.reply_menu_keyboard(expanded=True)
    for row in kb.keyboard:
        for b in row:
            if b.text == "✖️ 收起面板":
                continue
            assert _menu_key_from_text(b.text), f"补齐后失配：{b.text!r}"


def test_reply_menu_keeps_labels_intact():
    """补齐不能改掉原文案：按钮文字去掉填充后仍与菜单项一致。"""
    from templates import MENU_ITEMS

    kb = Keyboards.reply_menu_keyboard(expanded=True)
    labels = [b.text for row in kb.keyboard for b in row]
    for _, label in MENU_ITEMS:
        assert any(x.strip("\u3000 ") == label.strip("\u3000 ") for x in labels), label


# ---- 2. 联赛按钮不出现孤行 ---------------------------------------------------

def test_league_rows_are_balanced():
    """5 个联赛按钮必须均分（3+2），不能 4+1。

    末尾只剩一个按钮时，Telegram 把它拉满整行，变成一条又宽又空的长条，
    比正文还宽——正是「按钮比输出框宽」的观感来源。
    """
    text, markup, _, _ = FixturesView.format_fixtures_page(
        _pool(), TZ, per_page=10, day_label="2026-10-04", all_items=_pool())
    rows = [r for r in markup.inline_keyboard
            if all(b.callback_data.startswith("fxl:") for b in r)]
    assert rows, "没有找到联赛筛选行"
    sizes = [len(r) for r in rows]
    assert 1 not in sizes, f"出现独占一行的联赛按钮：{sizes}"
    assert max(sizes) - min(sizes) <= 1, f"联赛按钮分配不均：{sizes}"


# ---- 3. 切日期 / 切模式必须清掉旧的联赛筛选 ---------------------------------

class _Query:
    def __init__(self, data):
        self.data = data

    async def answer(self, *a, **k):
        return None


class _Update:
    def __init__(self, data):
        self.callback_query = _Query(data)


class _Cache:
    def __init__(self):
        self.cleared = False

    async def clear(self, key):
        self.cleared = True


class _Ctx:
    def __init__(self):
        self.user_data = {"fx_league": 39, "fx_page": 2}
        self.cache = _Cache()
        self.application = type("A", (), {})()
        self.application.bot_data = {
            "settings": type("S", (), {"timezone": TZ})(),
            "cache_manager": self.cache,
        }


def test_day_switch_drops_stale_league_filter(monkeypatch):
    """点「明天」必须清掉联赛筛选。

    不清的话：昨天筛英超，明天没英超，页面整片空白——用户只会理解为
    「这个联赛按钮点了没比赛」。
    """
    import main

    async def fake_show(update, context, page=0):
        return None

    monkeypatch.setattr(main, "show_fixtures", fake_show)
    ctx = _Ctx()
    _run(main.on_fixtures_day(_Update("fxd:1"), ctx))
    assert "fx_league" not in ctx.user_data


def test_mode_switch_drops_stale_league_filter(monkeypatch):
    """切「未来 7 天 / 下一场」同样要清筛选，理由同上。"""
    import main

    async def fake_show(update, context, page=0):
        return None

    monkeypatch.setattr(main, "show_fixtures", fake_show)
    ctx = _Ctx()
    _run(main.on_fixtures_mode(_Update("fxm:upcoming"), ctx))
    assert "fx_league" not in ctx.user_data


def test_league_switch_keeps_its_own_filter(monkeypatch):
    """反过来：主动点联赛时筛选必须保留，否则筛选功能形同虚设。"""
    import main

    seen = {}

    async def fake_show(update, context, page=0):
        seen["league"] = context.user_data.get("fx_league")

    monkeypatch.setattr(main, "show_fixtures", fake_show)
    ctx = _Ctx()
    _run(main.on_fixtures_league(_Update("fxl:140"), ctx))
    assert seen["league"] == 140


# ---- 4. 筛选失效时回退到全部，而不是给一个空页 -------------------------------

def test_stale_filter_falls_back_to_all_leagues():
    """筛选的联赛在这批比赛里不存在时，回退渲染全部联赛并说明原因。

    按钮是从当前这批比赛生成的，点了却空空如也比报错更让人困惑。
    """
    pool = [f for f in _pool() if f["league"]["id"] != 39]
    text, markup, _, _ = FixturesView.format_fixtures_page(
        pool, TZ, per_page=10, day_label="2026-10-04",
        all_items=pool, league_filter=39)
    plain = _plain(text)
    assert "皇马" in plain or "巴塞罗那" in plain, "回退后应当渲染出别的联赛"
    assert "英超" in plain, "应当说明是哪一个联赛本时段没比赛"
