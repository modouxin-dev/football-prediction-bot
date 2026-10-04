# -*- coding: utf-8 -*-
"""中文队名映射与归一化匹配的回归测试。"""
from __future__ import annotations

import pytest

from formatkit import display_width, team_name, team_short_name, team_mobile
from templates import TEAM_NAMES


# 每个已配置联赛至少抽两支代表队，确保映射覆盖到全部 17 个联赛
LEAGUE_SAMPLES = {
    39: ["Manchester City FC", "Arsenal FC"],
    140: ["Real Madrid CF", "FC Barcelona"],
    78: ["FC Bayern München", "Borussia Dortmund"],
    135: ["Juventus FC", "AC Milan"],
    61: ["Paris Saint-Germain FC", "Olympique de Marseille"],
    88: ["AFC Ajax", "PSV Eindhoven"],
    94: ["SL Benfica", "FC Porto"],
    203: ["Galatasaray SK", "Fenerbahçe SK"],
    71: ["Flamengo", "SE Palmeiras"],
    128: ["River Plate", "Boca Juniors"],
    98: ["Vissel Kobe", "Kashima Antlers"],
    292: ["Ulsan Hyundai FC", "Jeonbuk Hyundai Motors FC"],
    307: ["Al-Hilal SFC", "Al-Nassr FC"],
    103: ["Bodø/Glimt", "Molde FK"],
    113: ["Malmö FF", "AIK"],
}


def test_every_configured_league_has_chinese_names():
    for league_id, teams in LEAGUE_SAMPLES.items():
        for raw in teams:
            got = team_short_name(raw)
            assert got != raw, f"联赛 {league_id} 的 {raw!r} 未映射中文名"
            assert any("\u4e00" <= ch <= "\u9fff" for ch in got), f"{raw} 未得到中文"


def test_argentine_teams_from_production_screenshot():
    """用户截图里实际出现的四个阿甲队名，必须全部有中文。"""
    for raw, cn in [
        ("Estudiantes de Rio Cuarto", "里奥夸尔托"),
        ("Racing Club", "竞赛队"),
        ("Huracan", "飓风队"),
        ("Aldosivi", "阿尔多西维"),
    ]:
        assert team_short_name(raw) == cn


def test_normalization_matches_written_variants():
    """同一支队的不同写法（词缀/重音/简写）必须归一到同一个中文名。"""
    groups = [
        (["Liverpool FC", "Liverpool"], "利物浦"),
        (["Fenerbahçe SK", "Fenerbahce"], "费内巴切"),
        (["1. FC Köln", "FC Koln"], "科隆"),
        (["Bodø/Glimt", "Bodo Glimt"], "博多格林特"),
        (["Al-Hilal SFC", "Al-Hilal", "Al Hilal"], "利雅得新月"),
        (["Paris Saint-Germain FC", "PSG"], "巴黎圣日耳曼"),
    ]
    for variants, cn in groups:
        got = {team_short_name(v) for v in variants}
        assert got == {cn}, f"{variants} 归一结果不一致：{got}"


def test_unknown_team_never_fabricated():
    """未收录的队名必须原样返回，绝不臆造中文。"""
    raw = "Zzz Nonexistent United"
    assert team_short_name(raw) == raw
    assert team_name(raw, bilingual=True) == raw
    assert team_short_name("") == "?"
    assert team_short_name(None) == "?"


def test_no_chinese_name_exceeds_mobile_width():
    """中文名超过 14 列会在手机上折行，导致序号与队名错位。"""
    too_wide = {
        v: display_width(v)
        for v in set(TEAM_NAMES.values())
        if display_width(v) > 14
    }
    assert not too_wide, f"超宽中文名需缩短：{too_wide}"


def test_bilingual_keeps_original():
    assert team_name("Liverpool FC", bilingual=True) == "利物浦 (Liverpool FC)"
    assert team_name("Liverpool FC", bilingual=False) == "Liverpool FC"


def test_mobile_truncation_only_for_unknown():
    """已收录的队不该被截断；未收录的长英文名才截断。"""
    assert team_mobile("Liverpool FC") == "利物浦"
    long_unknown = "Wolverhampton Wanderers Football Club"
    assert display_width(team_mobile(long_unknown)) <= 16


def _mk(lid, name, home, away, hour):
    from datetime import datetime, timedelta, timezone as _tz
    from views.fixtures import FixturesView
    return FixturesView  # noqa: 仅保持导入路径一致


def test_fixtures_group_title_is_chinese_with_flag():
    """赛程分组标题必须是「国旗 + 中文联赛名 + 场数」，不能是数据源英文名。"""
    import pytz
    from datetime import datetime, timedelta, timezone as _tzu
    from views.fixtures import FixturesView

    tz = pytz.timezone("Asia/Shanghai")

    def mk(lid, lname, h, a, hour):
        return {
            "fixture": {
                "id": str(hour),
                "date": (datetime.now(_tzu.utc) + timedelta(hours=hour)).isoformat(),
                "status": {"short": "NS"},
            },
            "league": {"id": lid, "name": lname},
            "teams": {"home": {"name": h}, "away": {"name": a}},
            "goals": {"home": None, "away": None},
        }

    items = [
        mk(128, "Liga Profesional Argentina", "Estudiantes de Rio Cuarto", "Racing Club", 1),
        mk(128, "Liga Profesional Argentina", "Huracan", "Aldosivi", 2),
        mk(39, "Premier League", "Manchester City FC", "Liverpool FC", 3),
    ]
    text, _, _, _ = FixturesView.format_fixtures_page(
        items, tz, per_page=10, day_label="2026-10-04"
    )
    assert "🇦🇷" in text and "阿根廷甲级联赛" in text, "联赛标题应为中文+国旗"
    assert "Liga Profesional Argentina" not in text, "不应再显示数据源英文名"
    assert "2 场" in text, "应显示该联赛场数"
    assert "里奥夸尔托" in text and "竞赛队" in text, "队名应为中文"
    # 分组标题只在切换联赛时打印一次
    assert text.count("🇦🇷") == 1


def test_unregistered_league_falls_back_to_source_name():
    """未登记联赛不能丢失，回退数据源原名而不是显示「未知联赛」。"""
    import pytz
    from datetime import datetime, timedelta, timezone as _tzu
    from views.fixtures import FixturesView

    tz = pytz.timezone("Asia/Shanghai")
    fx = {
        "fixture": {
            "id": "1",
            "date": (datetime.now(_tzu.utc) + timedelta(hours=1)).isoformat(),
            "status": {"short": "NS"},
        },
        "league": {"id": 999999, "name": "Some Obscure League"},
        "teams": {"home": {"name": "Team A"}, "away": {"name": "Team B"}},
        "goals": {"home": None, "away": None},
    }
    text, _, _, _ = FixturesView.format_fixtures_page(
        [fx], tz, per_page=10, day_label="2026-10-04"
    )
    assert "Some Obscure League" in text
    assert "未知联赛" not in text
