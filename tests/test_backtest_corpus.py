"""回测语料（库内赛果 + 内置历史 CSV）合并的测试。

重点验证三件事，缺一不可：
1. 内置历史真的并进来了（样本量级提升）；
2. 队 id 与库内统一（否则走前回测接不上历史，等于白合并）；
3. 同一场比赛不会被算两次（重复计数会让 Log Loss 失真）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backtest import run_backtest
from backtest_corpus import fixture_to_match, known_team_ids, load_corpus
from football_data_uk import available_local_seasons
from repository import PredictionRepository


def _seed(repo, *, fixture_id: int, date: str, home_id: str, home_name: str,
          away_id: str, away_name: str, hs: int, as_: int,
          competition: str = "PL", season: int = 2023) -> None:
    """直接插一行已完赛比赛（绕过 API，纯数据准备）。"""
    conn = repo._connect()
    conn.execute(
        "INSERT INTO matches (id, competition_code, season, utc_date, status, "
        "matchday, home_team_id, home_team_name, away_team_id, away_team_name, "
        "home_score, away_score, source, raw_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fixture_id, competition, season, date, "FINISHED", 1,
         home_id, home_name, away_id, away_name, hs, as_,
         "test", None, "2026-01-01T00:00:00"),
    )
    conn.commit()


def _find(matches, date_prefix: str, home_id: str, away_id: str):
    for m in matches:
        if (m.get("utc_date") or "").startswith(date_prefix) \
                and str(m["home_team_id"]) == str(home_id) \
                and str(m["away_team_id"]) == str(away_id):
            return m
    return None


# ---- 1. 内置历史并入 -------------------------------------------------------

def test_builtin_history_files_exist():
    """前置条件：三个赛季 CSV 必须真的在镜像里，否则后面全是空谈。"""
    seasons = available_local_seasons()
    assert seasons == [2023, 2024, 2025], f"内置赛季异常: {seasons}"


def test_history_merged_into_empty_db(tmp_path):
    """库内为空时，语料应全部来自内置历史。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, info = load_corpus(repo, "PL")

    assert info["db"] == 0
    assert info["history"] > 1000, f"内置历史太少: {info['history']}"
    assert info["total"] == info["history"]
    assert info["seasons"] == [2023, 2024, 2025]


def test_every_row_has_scores(tmp_path):
    """回测器会跳过无比分的行；若大量缺失，样本量就是虚的。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, _ = load_corpus(repo, "PL")

    assert matches, "语料为空"
    for m in matches:
        assert isinstance(m["home_score"], int)
        assert isinstance(m["away_score"], int)
        assert m["home_team_id"] and m["away_team_id"]


# ---- 2. 队 id 统一（合并的价值所在）---------------------------------------

def test_csv_team_id_reuses_db_id(tmp_path):
    """CSV 的 "Man City" 必须复用库内 "Manchester City FC" 的 id。

    否则同一支球队在语料里有两个 id，走前回测会当成两支不同的队，
    历史完全接不上——合并就白做了。
    """
    repo = PredictionRepository(str(tmp_path / "c.db"))
    # 先造一条库内记录，让反查表知道这两队的官方 id（日期错开，避免去重）
    _seed(repo, fixture_id=900001, date="2023-05-01T12:00:00Z",
          home_id="328", home_name="Burnley FC",
          away_id="65", away_name="Manchester City FC", hs=0, as_=1)

    known = known_team_ids(repo)
    assert known.get("Burnley FC") == "328"
    assert known.get("Manchester City FC") == "65"

    matches, _ = load_corpus(repo, "PL")
    row = _find(matches, "2023-08-11", "328", "65")
    assert row is not None, "内置 CSV 里 2023-08-11 的 Burnley vs Man City 未找到"
    assert row["home_team_id"] == "328"
    assert row["away_team_id"] == "65"


def test_unknown_team_falls_back_to_slug(tmp_path):
    """库里没有的球队 → 退回 slug 兜底，不能崩。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, _ = load_corpus(repo, "PL")
    # 不配反查表时也应能正常解析（全走 slug）
    assert any(str(m["home_team_id"]).startswith("fduk-") for m in matches)


# ---- 3. 去重 ---------------------------------------------------------------

def test_same_match_not_counted_twice(tmp_path):
    """库内与 CSV 覆盖同一场时，只保留库内那行。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    _seed(repo, fixture_id=900002, date="2023-08-11T20:00:00Z",
          home_id="328", home_name="Burnley FC",
          away_id="65", away_name="Manchester City FC", hs=0, as_=3)

    matches, info = load_corpus(repo, "PL")
    rows = [m for m in matches
            if (m.get("utc_date") or "").startswith("2023-08-11")
            and str(m["home_team_id"]) == "328"
            and str(m["away_team_id"]) == "65"]

    assert len(rows) == 1, f"同一场被算了 {len(rows)} 次"
    assert rows[0]["source"] == "db", "应保留库内那行（真实 API 数据更全）"


# ---- 4. 排序与开关 ---------------------------------------------------------

def test_sorted_by_time(tmp_path):
    """走前回测要求时间正序，乱序会让「用之前的历史」变成前视偏差。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, _ = load_corpus(repo, "PL")
    dates = [m.get("utc_date") or "" for m in matches]
    assert dates == sorted(dates), "语料未按时间正序"


def test_history_can_be_disabled(tmp_path):
    """include_history=False 应退化为旧的「只有库内」。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    _seed(repo, fixture_id=900003, date="2023-08-11T20:00:00Z",
          home_id="328", home_name="Burnley FC",
          away_id="65", away_name="Manchester City FC", hs=0, as_=3)

    matches, info = load_corpus(repo, "PL", include_history=False)
    assert info["total"] == 1
    assert info["history"] == 0


# ---- 5. 端到端：回测器真能吃这份语料 --------------------------------------

def test_backtester_runs_on_corpus(tmp_path):
    """合并出来的语料必须能被回测器直接消费，否则只是好看的数字。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, info = load_corpus(repo, "PL")

    result = run_backtest(matches)
    n = result["comparison"]["challenger"]["n"]

    assert n > 1000, f"评估样本太少: {n}"
    assert result["comparison"]["challenger"]["log_loss"] is not None


def test_fixture_to_match_skips_unplayed():
    """未赛（无比分）的行必须被跳过，不能混进样本。"""
    fx = {
        "id": "1", "utcDate": "2023-08-11T20:00:00Z", "competition": "PL",
        "homeTeam": {"id": "1", "name": "A"}, "awayTeam": {"id": "2", "name": "B"},
        "score": {"fullTime": {"home": None, "away": None}},
    }
    assert fixture_to_match(fx) is None
