"""点联赛分组进入该联赛：只看一个联赛，序号与预测按钮跟着重排。

场景：五大联赛一起列时，不同联赛的比赛按开赛时间交替出现，同一个联赛
标题被反复打印，等于没分组。想只看英超得自己在几十行里挑。
"""
from __future__ import annotations

import html as _html
import re

import pytz

from formatkit import display_width
from views.fixtures import FixturesView, _league_of, _league_rows

TZ = pytz.timezone("Asia/Shanghai")


def _fx(lid: int, lname: str, home: str, away: str, i: int, date: str,
        short: str = "NS", gh: int | None = None, ga: int | None = None) -> dict:
    return {
        "fixture": {"id": i, "date": date, "status": {"short": short}},
        "league": {"id": lid, "name": lname},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": gh, "away": ga},
    }


def _items() -> list[dict]:
    return [
        _fx(39, "Premier League", "Man City", "Liverpool", 101, "2026-10-04T19:00:00+00:00"),
        _fx(128, "Liga Profesional Argentina", "Huracan", "Aldosivi", 102,
            "2026-10-04T20:00:00+00:00"),
        _fx(39, "Premier League", "Arsenal", "Chelsea", 103, "2026-10-04T21:00:00+00:00"),
        _fx(71, "Serie A Brasil", "Palmeiras", "Flamengo", 104, "2026-10-04T22:00:00+00:00"),
    ]


def _plain(markup: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", markup))


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def test_filter_shows_only_that_league():
    """筛选后只出现该联赛的比赛——混着列出来等于没筛。"""
    text, _, _, _ = FixturesView.format_fixtures_page(
        _items(), TZ, per_page=10, day_label="2026-10-04",
        all_items=_items(), league_filter=39)
    plain = _plain(text)
    assert "利物浦" in plain and "切尔西" in plain
    assert "飓风队" not in plain and "帕尔梅拉斯" not in plain


def test_filter_renumbers_matches_from_one():
    """序号必须从 1 重新开始。

    不重排的话，筛完剩下 2 场却显示成 1 和 3，用户点「预测 3」会落到
    另一个联赛的比赛上——按钮和数据错位是最难自查的一类 bug。
    """
    text, _, _, _ = FixturesView.format_fixtures_page(
        _items(), TZ, per_page=10, day_label="2026-10-04",
        all_items=_items(), league_filter=39)
    nums = [int(m) for m in re.findall(r"^\s*(\d+)\s", _plain(text), re.M)]
    assert nums == [1, 2], f"序号未重排: {nums}"


def test_filter_predict_buttons_point_at_that_league():
    """预测/分析按钮必须指向筛选后那一场的 fixture id。"""
    items = _items()
    _, markup, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04",
        all_items=items, league_filter=39)
    ids = [b.callback_data for b in _buttons(markup) if b.callback_data.startswith("fx:")]
    assert ids == ["fx:101", "fx:103"], f"按钮指向了别的联赛: {ids}"


def test_league_buttons_use_short_names_that_fit():
    """联赛按钮用短名：全称「英格兰超级联赛」14 列，4 个按钮平分一行时
    每格约 8 列，会被截成「英格兰超…」——国别恰恰是唯一有用的信息。"""
    rows = _league_rows(_items(), None)
    labels = [b.text for row in rows for b in row]
    assert "🏴󠁧󠁢󠁥󠁮󠁧󠁿英超" in labels or any("英超" in t for t in labels)
    for t in labels:
        assert display_width(t) <= 8, f"按钮过宽会被截断: {t!r}"


def test_league_button_marks_active_and_offers_all():
    """选中的联赛要打勾，并给出「全部联赛」出口——否则进去就出不来了。"""
    rows = _league_rows(_items(), 39)
    labels = [b.text for row in rows for b in row]
    assert sum(1 for t in labels if t.startswith("✅")) == 1
    assert any("全部联赛" in t for t in labels)


def test_league_button_callback_carries_league_id():
    """回调带联赛 ID，服务端按 ID 过滤——带名称会随数据源写法变化。"""
    rows = _league_rows(_items(), None)
    data = [b.callback_data for row in rows for b in row]
    assert data[:3] == ["fxl:39", "fxl:128", "fxl:71"]


def test_filtered_empty_states_which_league_and_offers_upcoming():
    """该联赛今天没球要说清楚，并给出去看它近期赛程的出口。

    只显示「暂无比赛」会被当成机器人坏了——用户不知道是无球还是坏了。
    """
    items = _items()
    text, markup, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04",
        all_items=items, league_filter=140)  # 西甲：本批数据里没有
    plain = _plain(text)
    assert "西甲" in plain and "该联赛本时段无比赛" in plain
    data = [b.callback_data for b in _buttons(markup)]
    assert "fxm:upcoming" in data


def test_no_filter_shows_every_league():
    """不筛选时全部联赛都在——默认视图不能偷偷变窄。"""
    items = _items()
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04",
        all_items=items, league_filter=None)
    plain = _plain(text)
    for team in ("利物浦", "切尔西", "飓风队", "帕尔梅拉斯"):
        assert team in plain


def test_league_of_tolerates_missing_or_dirty_ids():
    """联赛 ID 可能是字符串或缺失，解析失败要归 0 而不是抛异常。"""
    assert _league_of({"league": {"id": "39"}}) == 39
    assert _league_of({"league": {}}) == 0
    assert _league_of({}) == 0


def _run(coro):
    """在独立事件循环里跑一个协程。

    不能用 get_event_loop()：全量跑时前面的测试可能已关闭默认循环，
    单独跑能过、一起跑就挂——这类用例本身就是假绿灯。
    """
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Query:
    def __init__(self, data):
        self.data = data
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True


class _Update:
    def __init__(self, data):
        self.callback_query = _Query(data)


class _Context:
    def __init__(self):
        self.user_data = {"fx_page": 3}
        self.application = None


def test_callback_sets_per_user_filter_and_resets_page(monkeypatch):
    """筛选写进 user_data（按用户隔离），并回到第一页。

    页码不重置的话，上个联赛停在第 3 页而新联赛只有 1 页，会直接落到
    空页上——看着像「点了联赛就没反应」，实际是页码越界。
    """
    import main

    seen = {}

    async def fake_show(update, context, page=0):
        seen["page"] = page
        seen["league"] = context.user_data.get("fx_league")

    monkeypatch.setattr(main, "show_fixtures", fake_show)
    ctx = _Context()
    _run(main.on_fixtures_league(_Update("fxl:39"), ctx))
    assert seen["league"] == 39 and seen["page"] == 0
    assert ctx.user_data["fx_page"] == 0


def test_callback_all_clears_filter(monkeypatch):
    """「全部联赛」必须真的清掉筛选，否则用户永远回不到总览。"""
    import main

    async def fake_show(update, context, page=0):
        return None

    monkeypatch.setattr(main, "show_fixtures", fake_show)
    ctx = _Context()
    ctx.user_data["fx_league"] = 39
    _run(main.on_fixtures_league(_Update("fxl:all"), ctx))
    assert "fx_league" not in ctx.user_data


def test_league_callback_pattern_is_registered():
    """回调必须有处理器接管，否则按钮点了没反应（最常见的死按钮）。"""
    src = open("main.py", encoding="utf-8").read()
    assert 'pattern=r"^fxl:(all|-?\\d+)$"' in src
    assert "on_fixtures_league" in src


def test_each_league_title_prints_exactly_once():
    """同一联赛的标题只能出现一次。

    数据源按开赛时间返回全局序列，不聚合的话五个联赛的比赛交替出现、
    同一个标题被反复打印——分组标题就成了摆设。
    """
    text, _, _, _ = FixturesView.format_fixtures_page(
        _items(), TZ, per_page=10, day_label="2026-10-04", all_items=_items())
    plain = _plain(text)
    assert plain.count("英格兰超级联赛") == 1, plain
    assert plain.count("阿根廷甲级联赛") == 1, plain


def test_matches_of_one_league_are_contiguous():
    """同一联赛的比赛必须挨在一起，中间不能插入别的联赛。"""
    text, _, _, _ = FixturesView.format_fixtures_page(
        _items(), TZ, per_page=10, day_label="2026-10-04", all_items=_items())
    lids = [39 if "英格兰超级联赛" in ln else
            128 if "阿根廷甲级联赛" in ln else
            71 if "巴西甲级联赛" in ln else None
            for ln in _plain(text).split("\n")]
    blocks = [x for x in lids if x is not None]
    assert len(blocks) == len(set(blocks)), f"同一联赛被拆成了多块: {blocks}"


def test_leagues_follow_configured_order():
    """联赛先后按 LEAGUE_ORDER：五大联赛在前，其余随后。"""
    text, _, _, _ = FixturesView.format_fixtures_page(
        _items(), TZ, per_page=10, day_label="2026-10-04", all_items=_items())
    plain = _plain(text)
    order = [(plain.index(n), n) for n in
             ("英格兰超级联赛", "阿根廷甲级联赛", "巴西甲级联赛")]
    names = [n for _, n in sorted(order)]
    from templates import LEAGUE_ORDER
    assert [LEAGUE_ORDER.index(i) for i in (39, 71, 128)] == sorted(
        LEAGUE_ORDER.index(i) for i in (39, 71, 128))
    assert names == ["英格兰超级联赛", "巴西甲级联赛", "阿根廷甲级联赛"], names
