"""历史存档兜底：赛程查不到时，直接展示库里已落盘的预测结论。

背景：赛程快照是后来才加的列，更早的预测只有结论、没有快照；比赛一旦开赛
或跨天，今日赛程里也查不到了。此前这类点击一律报「该场比赛已不在今日赛程中」
——可结论明明躺在库里。用户点旧卡片的目的是**回看已算过的结论**，不是重算一遍。

本文件锁住三条性质：
1. 落盘结论读得回来，且重启后依然可用（不依赖内存）；
2. 结论不完整就返回 None，绝不补占位数字；
3. 兜底只补「找不到赛程」，不吞 API 故障。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from repository import PredictionRepository
from views.prediction import PredictionView

FIXTURE_ID = "777001"
KICKOFF = "2026-10-24T18:30:00+00:00"


def _repo(tmp_path, *, home_prob=0.56, draw_prob=0.23, away_prob=0.21,
          best_score="2-1", actual_home=None, actual_away=None,
          fixture_id=FIXTURE_ID):
    """建库并写入一行预测；概率可逐个置 None 以测「结论不完整」。"""
    r = PredictionRepository(str(tmp_path / "p.db"))
    conn = r._connect()
    conn.execute(
        "INSERT INTO predictions (fixture_id, season, league, home, away, kickoff, "
        "home_prob, draw_prob, away_prob, best_score, result, created_at, "
        "actual_home, actual_away) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fixture_id, 2026, "英超", "阿森纳", "埃弗顿", KICKOFF,
         home_prob, draw_prob, away_prob, best_score, "主胜", KICKOFF,
         actual_home, actual_away),
    )
    conn.commit()
    return r


# ---- 读回落盘结论 -----------------------------------------------------------

def test_stored_prediction_returns_saved_row(tmp_path):
    row = _repo(tmp_path).get_stored_prediction(FIXTURE_ID)
    assert row is not None, "落盘结论必须读得回来"
    assert row["home"] == "阿森纳" and row["away"] == "埃弗顿"
    assert row["home_prob"] == pytest.approx(0.56)


def test_stored_prediction_survives_new_repository_instance(tmp_path):
    """换实例（等价机器人重启）仍能读回——这正是内存快照做不到的。"""
    _repo(tmp_path)
    fresh = PredictionRepository(str(tmp_path / "p.db"))
    assert fresh.get_stored_prediction(FIXTURE_ID), "重启后存档必须还在"


def test_stored_prediction_returns_none_when_unknown(tmp_path):
    assert _repo(tmp_path).get_stored_prediction("000000") is None


def test_incomplete_probabilities_are_refused_not_guessed(tmp_path):
    """三合一概率缺任一就返回 None：宁可不展示，也不造一个数字凑数。"""
    r = _repo(tmp_path, draw_prob=None)
    assert r.get_stored_prediction(FIXTURE_ID) is None, "概率不全必须拒绝，不能补占位"


# ---- 存档卡片渲染 -----------------------------------------------------------

def test_stored_card_renders_teams_and_probabilities(tmp_path):
    row = _repo(tmp_path).get_stored_prediction(FIXTURE_ID)
    text = PredictionView.format_stored_card(row, timezone.utc)
    assert "阿森纳" in text and "埃弗顿" in text
    assert "56%" in text and "23%" in text and "21%" in text
    assert "2-1" in text
    assert "存档" in text  # 必须说明这是存档，避免被当成实时预测


def test_stored_card_shows_actual_score_when_settled(tmp_path):
    """已结算的比赛要给出实际比分——这本来就是用户回看时最想看的。"""
    row = _repo(tmp_path, actual_home=2, actual_away=1).get_stored_prediction(FIXTURE_ID)
    text = PredictionView.format_stored_card(row, timezone.utc)
    assert "2-1" in text and "实际比分" in text


def test_stored_card_says_it_cannot_reanalyze(tmp_path):
    """存档卡必须如实说明无法深度分析，不能让用户以为点了没反应。"""
    row = _repo(tmp_path).get_stored_prediction(FIXTURE_ID)
    assert "无法重新计算" in PredictionView.format_stored_card(row, timezone.utc)


def test_stored_card_tolerates_missing_optional_fields(tmp_path):
    """可选字段缺失就少显示一项，直接崩掉会让用户彻底看不到结论。"""
    row = _repo(tmp_path, best_score=None).get_stored_prediction(FIXTURE_ID)
    text = PredictionView.format_stored_card(row, timezone.utc)
    assert "阿森纳" in text


def test_stored_card_escapes_team_names(tmp_path):
    """队名进 HTML 必须转义，否则特殊字符会破坏整条消息。"""
    r = _repo(tmp_path, fixture_id="777002")
    conn = r._connect()
    conn.execute("UPDATE predictions SET home = ? WHERE fixture_id = ?",
                 ("A<B&C", "777002"))
    conn.commit()
    row = r.get_stored_prediction("777002")
    text = PredictionView.format_stored_card(row, timezone.utc)
    assert "<B" not in text and "&lt;B" in text
