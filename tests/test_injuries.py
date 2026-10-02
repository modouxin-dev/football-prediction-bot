"""伤停接入测试：分组整理 + 缺失降级 + 视图渲染（不联网）。"""
import asyncio
from datetime import datetime, timedelta, timezone

from service import injuries_stats
from views.analysis import _injuries_body


def run(coro):
    return asyncio.run(coro)


def _inj(team_id: int, name: str, reason: str = "") -> dict:
    player = {"id": 1, "name": name}
    if reason:
        player["reason"] = reason
    return {"player": player, "team": {"id": team_id, "name": f"队{team_id}"}}


def test_injuries_stats_groups_by_side():
    raw = [_inj(1, "甲", "受伤"), _inj(1, "乙"), _inj(2, "丙", "停赛"), _inj(9, "无关")]
    out = injuries_stats(raw, 1, 2)
    assert out["home"] == ["甲（受伤）", "乙"]
    assert out["away"] == ["丙（停赛）"]


def test_injuries_stats_empty_raw():
    assert injuries_stats([], 1, 2) == {"home": [], "away": []}
    assert injuries_stats(None, 1, 2) == {"home": [], "away": []}


def test_injuries_body_no_data_shows_reason_not_fake():
    body = _injuries_body({"home": "主", "away": "客", "injuries": {"home": [], "away": []}}, {})
    assert any("未提供" in x for x in body)


def test_injuries_body_surfaces_fetch_error():
    body = _injuries_body(
        {"home": "主", "away": "客", "injuries": {"home": [], "away": []}},
        {"injuries": "ReadTimeout"},
    )
    assert any("ReadTimeout" in x for x in body)


def test_injuries_body_lists_players_and_truncates():
    report = {
        "home": "阿森纳", "away": "利兹联",
        "injuries": {"home": [f"球员{i}" for i in range(10)], "away": ["客队1"]},
    }
    body = _injuries_body(report, {})
    assert any("阿森纳" in x and "10 人" in x for x in body)
    assert any("另有 2 人" in x for x in body)
    assert any("客队1" in x for x in body)


def test_injuries_body_one_side_empty():
    report = {"home": "主", "away": "客", "injuries": {"home": ["甲"], "away": []}}
    body = _injuries_body(report, {})
    assert any("甲" in x for x in body)
    assert any("客" in x and "暂无" in x for x in body)
