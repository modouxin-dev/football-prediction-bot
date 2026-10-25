"""实时板块只展示「本模型推送过」的比赛。

`fixtures?live=all` 返回的是全网进行中的比赛，绝大多数与本项目无关
（未配置联赛，或已配置但没生成预测的场次）。展示无关比赛会让人
误以为模型预测了它们——那比少显示几场严重得多。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import api
from repository import PredictionRepository


def _mk(tmp_path):
    db = tmp_path / "t.db"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE predictions(fixture_id TEXT PRIMARY KEY, league TEXT,"
                 " home TEXT, away TEXT, kickoff TEXT, home_prob REAL, draw_prob REAL,"
                 " away_prob REAL, best_score TEXT, result TEXT, actual_home INTEGER,"
                 " actual_away INTEGER)")
    conn.commit()
    return db, conn


def test_predicted_fixture_ids_filters_unrelated(tmp_path):
    """只有做过预测的比赛 ID 会被判定为「本模型推送过」。"""
    db, conn = _mk(tmp_path)
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                 ("111", "140", "H", "A", now.isoformat(), .5, .25, .25, "1-0", "home"))
    conn.commit()

    repo = PredictionRepository(str(db))
    got = repo.predicted_fixture_ids(["111", "222", "333"])
    assert got == {"111"}


def test_predicted_fixture_ids_handles_empty(tmp_path):
    """空输入不拼 SQL —— `IN ()` 是非法语句。"""
    db, _ = _mk(tmp_path)
    repo = PredictionRepository(str(db))
    assert repo.predicted_fixture_ids([]) == set()
    assert repo.predicted_fixture_ids(None) == set()


def test_predicted_fixture_ids_normalizes_types(tmp_path):
    """整数 ID 与字符串 ID 视为同一个（数据源两种都下发）。"""
    db, conn = _mk(tmp_path)
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                 ("9001", "140", "H", "A", now.isoformat(), .5, .25, .25, "1-0", "home"))
    conn.commit()
    repo = PredictionRepository(str(db))
    assert repo.predicted_fixture_ids([9001]) == {"9001"}


def test_live_pick_respects_status():
    """只挑真正进行中的状态，未开赛/已完赛不进实时板块。"""
    def fx(short):
        return {"fixture": {"id": 1, "status": {"short": short}, "date": "2026-01-01"}}
    raw = [fx("NS"), fx("1H"), fx("FT"), fx("2H"), fx("HT")]
    out = api._pick_live(raw)
    shorts = [((f.get("fixture") or {}).get("status") or {}).get("short") for f in out]
    assert shorts == ["1H", "HT", "2H"] or set(shorts) == {"1H", "HT", "2H"}
    assert "NS" not in shorts and "FT" not in shorts


def test_not_predicted_status_is_never_cached():
    """「非本模型推送」的结果不能进缓存。

    它随时可能变（我们推送的比赛开始踢了），缓存会让它迟迟不出现。
    缓存白名单只允许 status=ok，这里断言该常量确实排除了 not_predicted。
    """
    src = open("api.py", encoding="utf-8").read()
    assert 'should_cache=lambda v: v.get("status") == "ok"' in src
    assert '"not_predicted"' in src
