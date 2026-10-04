"""日期导航条：点「明天」就该看明天的球，不用手打命令。

场景：用户点「今日赛程」，看到今天没比赛。以前只能退出去打
/date 2026-10-05，多数人根本不知道有这个命令，于是判定机器人坏了。
导航条把「明天/后天」变成一次点击。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from views.fixtures import DAY_NAV_SPAN, WEEKDAY_CN, _day_nav_row

TZ = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


def _row(active=None):
    return _day_nav_row(TZ, active)


def test_day_nav_has_four_days_starting_today():
    """导航条给出今天起连续 4 天，覆盖最常见的『今明两天』需求。"""
    row = _row()
    assert len(row) == DAY_NAV_SPAN == 4
    today = datetime.now(TZ).date()
    assert row[0].text.endswith(today.strftime("%m-%d"))
    assert row[1].text.endswith((today + timedelta(days=1)).strftime("%m-%d"))


def test_day_nav_button_text_fits_mobile_column():
    """按钮文字必须短到能在手机上完整显示。

    4 个按钮平分一行，每格只有约 8 列。此前写「今天 10-04」共 10 列，
    被截成「今天 10-…」——恰恰是最该看见的日期被挤没了。改成纯 MM-DD
    后连打勾也才 7 列，完整可见；「今天/明天」移到标题行，那里不限宽。
    """
    from formatkit import display_width

    row = _row(active=datetime.now(TZ).date())
    for b in row:
        assert display_width(b.text) <= 8, f"按钮过宽会被截断: {b.text!r}"


def test_day_nav_labels_show_real_dates():
    """按钮上必须是真实日期，用户才知道自己点了哪天。"""
    row = _row()
    today = datetime.now(TZ).date()
    assert today.strftime("%m-%d") in row[0].text
    assert (today + timedelta(days=1)).strftime("%m-%d") in row[1].text


def test_day_nav_marks_active_day_with_check():
    """当前所在日期要打勾——不打勾，点完明天就分不清在看哪天。"""
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    row = _row(active=tomorrow)
    marked = [b for b in row if b.text.startswith("✅")]
    assert len(marked) == 1, f"应恰好标记一天，实际 {len(marked)}"
    assert marked[0].text.endswith(tomorrow.strftime("%m-%d"))


def test_day_nav_callback_carries_day_offset():
    """回调带偏移天数，服务端自行换算日期——带日期字符串会随时区跑偏。"""
    row = _row()
    assert row[0].callback_data == "fxd:0"
    assert row[1].callback_data == "fxd:1"
    assert row[3].callback_data == f"fxd:{DAY_NAV_SPAN - 1}"


def test_relative_day_labels():
    """相对标签：今天/明天/后天，再往后回退到星期。

    按钮上放不下这两个字，但标题行没有宽度限制——用户仍然一眼知道
    「现在看的是不是今天」，这正是日期导航最需要的信息。
    """
    from views.fixtures import _relative_day

    today = datetime.now(TZ).date()
    assert _relative_day(today, TZ) == "今天"
    assert _relative_day(today + timedelta(days=1), TZ) == "明天"
    assert _relative_day(today + timedelta(days=2), TZ) == "后天"
    later = today + timedelta(days=5)
    assert _relative_day(later, TZ) == WEEKDAY_CN[later.weekday()]
