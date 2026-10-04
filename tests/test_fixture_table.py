"""赛程三列排版：日期时间 · 主队 vs 客队 · 进度

原来的写法是每场分三行（序号+时间 / 对阵 / 状态），手机上三行各自换行，
队名一长就折行，序号和状态对不上人。改成 <pre> 等宽表格后，三列各自
成列，扫视时是一条竖线。
"""
from __future__ import annotations

import html as _html
import re
import unicodedata

import pytz

from views.fixtures import FixturesView, WEEKDAY_CN

TZ = pytz.timezone("Asia/Shanghai")


def _fx(lid: int, lname: str, home: str, away: str, i: int, date: str,
        short: str = "NS", gh: int | None = None, ga: int | None = None) -> dict:
    return {
        "fixture": {"id": i, "date": date, "status": {"short": short}},
        "league": {"id": lid, "name": lname},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": gh, "away": ga},
    }


def _plain(markup: str) -> str:
    """剥掉标签并还原实体，得到用户真正看到的文本（列宽要按它算）。"""
    return _html.unescape(re.sub(r"<[^>]+>", "", markup))


def _width(text: str) -> int:
    """显示宽度：中文占 2 列。按字符数算会把中英混排的表格算错。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _data_lines(plain: str) -> list[str]:
    return [ln for ln in plain.split("\n") if " vs " in ln]


def test_vs_separator_sits_in_one_column():
    """vs 必须落在同一视觉列——这是「对齐」最容易坏的一处。

    队名长短不一，若整串拼接后再补位，vs 会随队名左右漂移，
    两场比赛的对阵就对不齐了。
    """
    items = [
        _fx(128, "Liga Profesional Argentina",
            "Estudiantes de Rio Cuarto", "Racing Club", 1, "2026-10-04T21:45:00+00:00"),
        _fx(128, "Liga Profesional Argentina",
            "Huracan", "Aldosivi", 2, "2026-10-04T22:45:00+00:00"),
    ]
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04")
    cols = {_width(ln[:ln.index(" vs ")]) for ln in _data_lines(_plain(text))}
    assert len(cols) == 1, f"vs 不在同一列: {cols}"


def test_progress_column_sits_in_one_column():
    """进度列同样要对齐，否则「未开始/2-1」会参差不齐。"""
    items = [
        _fx(39, "Premier League", "Arsenal FC", "Chelsea FC", 1,
            "2026-10-03T14:00:00+00:00", "FT", 2, 1),
        _fx(39, "Premier League", "Manchester City FC", "Liverpool FC", 2,
            "2026-10-04T14:00:00+00:00", "NS"),
    ]
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04")
    starts = set()
    for ln in _data_lines(_plain(text)):
        head, _, tail = ln.partition(" vs ")
        # 进度 = 客队名补位之后的那一列
        starts.add(_width(ln) - _width(tail.strip().split()[-1]))
    assert len(starts) == 1, f"进度列未对齐: {starts}"


def test_five_char_team_name_is_not_truncated():
    """5 字中文队名必须完整显示：截断成「里奥夸尔…」等于没翻译。

    队名列宽按本页最长队名自适应（上限 5 字），所以这里不会被砍。
    """
    items = [
        _fx(128, "Liga Profesional Argentina",
            "Estudiantes de Rio Cuarto", "Racing Club", 1, "2026-10-04T21:45:00+00:00"),
    ]
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04")
    plain = _plain(text)
    assert "里奥夸尔托" in plain
    assert "…" not in "".join(_data_lines(plain)), "本页不该有被截断的队名"


def test_finished_match_shows_score_in_progress_column():
    """已完场给比分而不是「已完场」三个字——比分才是想看的。"""
    items = [_fx(39, "Premier League", "Arsenal FC", "Chelsea FC", 1,
                 "2026-10-03T14:00:00+00:00", "FT", 2, 1)]
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04")
    line = _data_lines(_plain(text))[0]
    assert line.rstrip().endswith("2-1"), line


def test_multi_day_groups_by_date_and_keeps_row_time_short():
    """跨天时日期单独成行做小标题，行内只留 HH:MM。

    若把「10-07 21:45」塞进 5 列的时间列，对阵列会被压到剩 14 列，
    两侧队名各 5 列——中文字直接被砍一半。
    """
    items = [
        _fx(39, "Premier League", "Arsenal FC", "Chelsea FC", 1,
            "2026-10-04T14:00:00+00:00"),
        _fx(39, "Premier League", "Manchester City FC", "Liverpool FC", 2,
            "2026-10-06T14:00:00+00:00"),
    ]
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04~10-06", multi_day=True)
    plain = _plain(text)
    assert re.search(r"^10-04 周", plain, re.M), plain
    assert re.search(r"^10-06 周", plain, re.M), plain
    for ln in _data_lines(plain):
        assert re.match(r"\s*\d+\s+\d{2}:\d{2}\s", ln), f"行内时间应只有 HH:MM: {ln!r}"


def test_row_width_fits_mobile_screen():
    """整行不得超过 40 显示列，超过手机上就要横向滚动，一滚三列就散了。"""
    items = [
        _fx(128, "Liga Profesional Argentina",
            "Estudiantes de Rio Cuarto", "Racing Club", 1, "2026-10-04T21:45:00+00:00"),
        _fx(39, "Premier League", "Brighton & Hove Albion FC",
            "Wolverhampton Wanderers FC", 2, "2026-10-03T14:00:00+00:00", "FT", 2, 1),
    ]
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, TZ, per_page=10, day_label="2026-10-04")
    widest = max(_width(ln) for ln in _data_lines(_plain(text)))
    assert widest <= 40, f"最宽行 {widest} 列，手机上会横向滚动"


def test_row_escapes_html_and_keeps_column_width():
    """队名里的 & < > 必须转义，否则 <pre> 整块会被判为非法 HTML。

    转义必须在补齐列宽之后做：esc 只增加源码字符数（& -> &amp;），
    不改变渲染后的列宽。反过来做会让这一行少一列、整页一起错位。
    """
    from views.fixtures import _table_row

    row = _table_row(1, "22:00", "A & B", "C <D>", "未开始", 8)
    assert "&amp;" in row and "&lt;" in row and "&gt;" in row, row
    # 2(序号) + 1 + 5(时间) + 1 + 8+4+8(对阵) + 1 + 6(进度)
    assert _width(_plain(row)) == 36, f"转义后列宽变了: {_width(_plain(row))}"
