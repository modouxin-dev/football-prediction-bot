"""每日汇总视图的单测 / Daily digest view tests.

汇总视图是「每天一条推送」的核心，也是唯一一个把几十场比赛压成一条消息
的地方。这里的用例重点守三件事：

1. **分组与排序**：多联赛必须按联赛分开、组内按开赛时间升序
2. **移动端宽度**：每行不超过守门线，否则手机上折行后左右错位
3. **空列表兜底**：无比赛时给出可读说明，而不是空白消息

全部用假数据构造，不碰数据库、不发网络请求。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from templates import DISCLAIMER, LEAGUE_NAMES
from views.digest import DigestView, _league_id_of, _league_sort_key

TZ = ZoneInfo("Asia/Shanghai")


class _Strength:
    """TeamStrength 的最小替身：视图只用到 games_home / games_away。"""

    def __init__(self, games: int = 20):
        self.games_home = games
        self.games_away = games


def _mk(fixture_id: int, league_id: int, home: str, away: str,
        minute: int, probs=(0.55, 0.25, 0.20)) -> dict:
    """构造一个够视图用的假预测（用 dict 属性对象，避免依赖真实模型层）。"""
    class _P:
        pass

    p = _P()
    p.fixture_id = fixture_id
    p.home = home
    p.away = away
    p.kickoff = datetime(2026, 10, 5, 19, minute, tzinfo=timezone.utc)
    p.analysis = {
        "win_prob": probs[0],
        "draw_prob": probs[1],
        "loss_prob": probs[2],
    }
    p.best = None
    p.fixture = {"league": {"id": league_id}}
    return p


def _digest(preds, note: str | None = None) -> str:
    return DigestView.format_daily_digest(preds, None, TZ, note=note)


def test_empty_digest_is_readable_not_blank():
    """无比赛时必须给出可读说明，空白消息会让用户以为机器人坏了。"""
    text = _digest([], note="国际比赛日")
    assert "暂无比赛" in text
    assert "国际比赛日" in text
    assert DISCLAIMER in text


def test_single_league_grouping_and_header():
    preds = [_mk(1, 39, "Arsenal FC", "Liverpool FC", 30)]
    text = _digest(preds)
    assert "今日预测汇总" in text
    assert LEAGUE_NAMES[39] in text  # 联赛名
    assert "1" in text and "场" in text


def test_multi_league_grouped_and_counted():
    """五大联赛混合时：按联赛分组，每组成立独立小标题。"""
    preds = [
        _mk(1, 39, "Arsenal FC", "Liverpool FC", 30),
        _mk(2, 140, "Real Madrid CF", "FC Barcelona", 0),
        _mk(3, 78, "FC Bayern München", "Borussia Dortmund", 15),
    ]
    text = _digest(preds)
    assert LEAGUE_NAMES[39] in text
    assert LEAGUE_NAMES[140] in text
    assert LEAGUE_NAMES[78] in text
    assert "共 <b>3</b> 场" in text
    assert "3</b> 个联赛" in text


def test_league_order_puts_premier_league_first():
    """汇总页联赛顺序：英超(39) 必须排在巴甲(71) 之前。"""
    preds = [
        _mk(1, 71, "Team A", "Team B", 10),
        _mk(2, 39, "Arsenal FC", "Liverpool FC", 20),
    ]
    text = _digest(preds)
    assert text.index(LEAGUE_NAMES[39]) < text.index(LEAGUE_NAMES[71])


def test_matches_sorted_by_kickoff_within_league():
    """同一联赛内按开赛时间升序：早场排在前。"""
    late = _mk(1, 39, "Arsenal FC", "Liverpool FC", 45)
    early = _mk(2, 39, "Chelsea FC", "Everton FC", 0)
    text = _digest([late, early])
    # 汇总页用移动端中文短名，断言必须用转换后的名字
    assert text.index("切尔西") < text.index("阿森纳")


def test_every_line_within_mobile_width():
    """守门线：剥离标签后每行不得超过 36 列，否则手机折行错位。"""
    preds = [
        _mk(1, 39, "Brighton & Hove Albion FC", "Wolverhampton Wanderers FC", 30),
        _mk(2, 135, "FC Internazionale Milano", "Borussia Mönchengladbach", 15),
    ]
    text = _digest(preds)
    import re
    for raw in text.split("\n"):
        line = re.sub(r"<[^>]+>", "", raw)  # 去 HTML 标签
        # 分隔线本身是装饰，不参与宽度守门
        if set(line.strip()) <= set("━─"):
            continue
        assert len(line) <= 36, f"超宽({len(line)}): {line!r}"


def test_unknown_league_is_kept_not_dropped():
    """未登记联赛不能让比赛凭空消失——宁可分组难看也不能丢数据。"""
    preds = [_mk(1, 99999, "Team A", "Team B", 0)]
    text = _digest(preds)
    assert "Team A" in text


def test_league_id_of_missing_league_defaults_zero():
    class _P:
        fixture = {}
    assert _league_id_of(_P()) == 0


def test_league_sort_key_unregistered_goes_last():
    """未登记联赛排最后；两个未登记的之间按 ID 升序。"""
    assert _league_sort_key(39) < _league_sort_key(99999)
    assert _league_sort_key(88888) < _league_sort_key(99999)


def test_warning_flag_shown_when_edge_high():
    """分歧大时打 ⚠️：实测 edge 越大 ROI 越差，这里提示的是「模型不可信」而非机会。"""

    class _P:
        pass

    p = _mk(1, 39, "Arsenal FC", "Liverpool FC", 30)
    p.best = ("home", {"edge": 0.12})
    text = DigestView.format_daily_digest([p], None, TZ)
    assert "⚠️" in text and "🚀" not in text


def test_single_page_has_no_page_number():
    """只有一页时不加页码——否则每次推送都挂着「（1/1）」很奇怪。"""
    preds = [_mk(1, 39, "Arsenal FC", "Liverpool FC", 30)]
    pages = DigestView.format_daily_digest_pages(preds, None, TZ)
    assert len(pages) == 1
    assert "1/1" not in pages[0]


def test_many_matches_split_into_multiple_pages():
    """场次多到超限必须分页，且每页都不超过硬上限。"""
    preds = [
        _mk(i, lid, f"Home Team {i}", f"Away Team {i}", i % 60)
        for i, lid in enumerate([39, 140, 78, 135, 61] * 12)  # 60 场
    ]
    pages = DigestView.format_daily_digest_pages(preds, None, TZ)
    assert len(pages) > 1, "60 场应当触发分页"
    for p in pages:
        assert len(p) <= 4096, f"单页超长：{len(p)}"


def test_pagination_loses_no_match():
    """分页最容易犯的错是丢数据：总数必须对得上。"""
    preds = [
        _mk(i, lid, f"Home{i}", f"Away{i}", i % 60)
        for i, lid in enumerate([39, 140, 78, 135, 61] * 12)
    ]
    pages = DigestView.format_daily_digest_pages(preds, None, TZ)
    joined = "\n".join(pages)
    for i in range(60):
        # 队名被移动端短名处理过，用数字部分核对是否出现
        assert f"Home{i}" in joined or f"Home{str(i)[:8]}" in joined


def test_each_page_is_independently_readable():
    """每页都要自带页眉和免责声明——用户可能只翻到第 2 页。"""
    preds = [
        _mk(i, lid, f"Home{i}", f"Away{i}", i % 60)
        for i, lid in enumerate([39, 140, 78, 135, 61] * 12)
    ]
    pages = DigestView.format_daily_digest_pages(preds, None, TZ)
    for p in pages:
        assert "今日预测汇总" in p
        assert DISCLAIMER in p


def test_no_value_flag_when_edge_low():
    p = _mk(1, 39, "Arsenal FC", "Liverpool FC", 30)
    p.best = ("home", {"edge": 0.01})
    text = DigestView.format_daily_digest([p], None, TZ)
    assert "🚀" not in text
