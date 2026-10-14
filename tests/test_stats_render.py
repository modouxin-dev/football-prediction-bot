"""/stats 渲染：按模板分「整体表现 / 分类准确率 / 联赛表现」。

为什么单独测渲染而不是只测数据层：
    数据层全绿但渲染把字段漏掉（或拼错键名导致整块消失）的情况，
    在只测 stats() 时完全测不出来。渲染测试盯的是「用户看到什么」。

    另一类真实风险是 Telegram 4096 字符硬限：联赛越多越长，超限后
    整条推送 400 失败，用户在机器人里什么都收不到——这是静默故障。
"""

from __future__ import annotations

import re

import pytest

from commands.admin import MIN_LEAGUE_SAMPLE, render_stats

TAG_RE = re.compile(r"</?(b|i|u|code|s)>")


def _st(**kw) -> dict:
    """构造 stats() 的返回值，未指定的字段给合法空值。"""
    base = {
        "total": 0, "hit": 0, "rate": None, "streak": 0,
        "by_level": {}, "by_result": {}, "by_league": {},
        "pending": 0, "persistent": True, "scored": 0,
        "brier": None, "logloss": None,
    }
    base.update(kw)
    return base


def _tag_balanced(html: str) -> bool:
    """HTML 标签成对：开标签数与闭标签数按标签名分别相等。"""
    from collections import Counter
    c = Counter()
    for m in TAG_RE.finditer(html):
        name = m.group(1)
        c[name] += 1 if m.group(0)[1] != "/" else -1
    return all(v == 0 for v in c.values())


# ---- 契约 1：整体表现 -----------------------------------------------------

def test_overall_shows_settled_and_rate():
    html = render_stats(_st(total=127, hit=69, rate=69 / 127, pending=5))
    assert "整体表现" in html
    assert "127" in html
    assert "54.3%" in html  # 69/127 = 54.33%


def test_empty_db_shows_waiting_not_crash():
    """空库：给「等待首场结算」而不是除零或 KeyError。"""
    html = render_stats(_st())
    assert "等待首场结算" in html
    assert _tag_balanced(html)


# ---- 契约 2：分类准确率（按预测方向）--------------------------------------

def test_direction_breakdown_lists_three_rows():
    html = render_stats(_st(
        total=127, hit=69, rate=0.543,
        by_result={
            "主胜": {"total": 74, "hit": 43, "rate": 43 / 74},
            "平局": {"total": 24, "hit": 10, "rate": 10 / 24},
            "客胜": {"total": 29, "hit": 16, "rate": 16 / 29},
        },
    ))
    assert "分类准确率" in html
    for label in ("主胜", "平局", "客胜"):
        assert label in html
    assert "58.1%" in html  # 43/74
    assert "41.7%" in html  # 10/24
    assert "55.2%" in html  # 16/29


def test_direction_order_is_home_draw_away():
    """顺序固定为 主胜→平局→客胜，与概率三连的阅读顺序一致。"""
    html = render_stats(_st(total=3, hit=3, rate=1.0, by_result={
        "客胜": {"total": 1, "hit": 1, "rate": 1.0},
        "主胜": {"total": 1, "hit": 1, "rate": 1.0},
        "平局": {"total": 1, "hit": 1, "rate": 1.0},
    }))
    pos = [html.index(x) for x in ("主胜", "平局", "客胜")]
    assert pos[0] < pos[1] < pos[2]


# ---- 契约 3：联赛表现 -----------------------------------------------------

def test_league_breakdown_uses_chinese_short_name():
    """代码/英文名都要落到中文短名，不能把 'Premier League' 原样丢给用户。"""
    html = render_stats(_st(total=2, hit=1, rate=0.5, by_league={
        "Premier League": {"total": 35, "hit": 20, "rate": 20 / 35},
        "PL": {"total": 21, "hit": 11, "rate": 11 / 21},
        "39": {"total": 13, "hit": 7, "rate": 7 / 13},
    }))
    assert "联赛表现" in html
    assert "英超" in html
    assert "Premier League" not in html  # 不应把英文原名直接端给用户


def test_unknown_league_name_kept_not_dropped():
    """未登记的联赛：原样显示，绝不能让这批比赛从统计里消失。"""
    html = render_stats(_st(total=1, hit=1, rate=1.0, by_league={
        "某某邀请赛": {"total": 5, "hit": 3, "rate": 0.6},
    }))
    assert "某某邀请赛" in html
    assert "5" in html


def test_small_sample_leagues_get_caveat():
    """存在样本 < MIN_LEAGUE_SAMPLE 的联赛时给出提示。"""
    html = render_stats(_st(total=30, hit=15, rate=0.5, by_league={
        "PL": {"total": 25, "hit": 13, "rate": 0.52},
        "PD": {"total": 5, "hit": 2, "rate": 0.4},
    }))
    assert "仅供参考" in html
    assert str(MIN_LEAGUE_SAMPLE) in html


def test_all_large_sample_has_no_caveat():
    """所有联赛样本都够时不加提示——提示本身也是噪音。"""
    html = render_stats(_st(total=50, hit=25, rate=0.5, by_league={
        "PL": {"total": 30, "hit": 15, "rate": 0.5},
        "PD": {"total": 20, "hit": 10, "rate": 0.5},
    }))
    assert "仅供参考" not in html


def test_league_sorted_by_count_desc():
    """按场次降序：样本多的排前面，用户先看可信度高的。"""
    html = render_stats(_st(total=40, hit=20, rate=0.5, by_league={
        "PD": {"total": 10, "hit": 5, "rate": 0.5},
        "PL": {"total": 30, "hit": 15, "rate": 0.5},
    }))
    assert html.index("英超") < html.index("西甲")


# ---- 契约 4：硬约束 -------------------------------------------------------

def test_html_tags_balanced_with_full_data():
    html = render_stats(_st(
        total=200, hit=110, rate=0.55, pending=8, scored=200,
        brier=0.21, logloss=1.02,
        by_result={"主胜": {"total": 100, "hit": 60, "rate": 0.6},
                   "平局": {"total": 40, "hit": 15, "rate": 0.375},
                   "客胜": {"total": 60, "hit": 35, "rate": 0.583}},
        by_league={"PL": {"total": 80, "hit": 48, "rate": 0.6},
                   "PD": {"total": 60, "hit": 32, "rate": 0.533},
                   "SA": {"total": 40, "hit": 20, "rate": 0.5},
                   "BL1": {"total": 20, "hit": 10, "rate": 0.5}},
        by_level={"high": {"total": 50, "hit": 35, "rate": 0.7, "scored": 50},
                  "medium": {"total": 90, "hit": 45, "rate": 0.5, "scored": 90},
                  "low": {"total": 60, "hit": 30, "rate": 0.5, "scored": 60}},
    ))
    assert _tag_balanced(html)


def test_stays_within_telegram_limit_with_many_leagues():
    """20 个联赛时仍不超 4096 硬限——超了整条推送会 400 失败。"""
    by_league = {f"L{i}": {"total": 30, "hit": 15, "rate": 0.5} for i in range(20)}
    html = render_stats(_st(
        total=600, hit=300, rate=0.5,
        by_result={"主胜": {"total": 300, "hit": 150, "rate": 0.5},
                   "平局": {"total": 150, "hit": 75, "rate": 0.5},
                   "客胜": {"total": 150, "hit": 75, "rate": 0.5}},
        by_league=by_league,
    ))
    assert len(html) <= 4096


def test_none_rate_renders_dash_not_crash():
    """rate 为 None（无已结算）时渲染成 —，不是抛 TypeError。"""
    html = render_stats(_st(total=0, hit=0, rate=None, pending=3))
    assert "—" in html
