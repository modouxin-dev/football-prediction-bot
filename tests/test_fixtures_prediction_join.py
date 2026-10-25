"""赛程与预测的连表必须兼容两种 ID 写法。

背景（实测，非推测）：看板「今日赛程」里每场都显示「尚未生成预测」，
而自动预测任务状态是 ok。

根因是两个数据源的 ID 写法不同：
  - 主源 API-Football：纯数字，如 1575523
  - 备用源 football-data.org：带 "fd-" 前缀，如 fd-555051

matches.id 存的是 _stable_id() 归一化后的**整数**（前缀被剥掉），
而 predictions.fixture_id 存的是**原始字符串**（前缀还在）。
JOIN 条件用 `p.fixture_id = CAST(m.id AS TEXT)` 时，
"fd-555051" 永远不等于 "555051" —— 预测明明存在，却显示为未生成。

这类缺陷不会报错：LEFT JOIN 匹配不上只是安静地返回 NULL，
上层按「没算过」渲染，看起来像预测功能坏了。
"""
from __future__ import annotations

import sqlite3

import pytest

from repository import PredictionRepository


@pytest.fixture()
def repo(tmp_path):
    r = PredictionRepository(str(tmp_path / "t.db"))
    r._connect()
    return r


def _seed_match(conn, mid: int, comp: str = "BSA"):
    conn.execute(
        """INSERT INTO matches
           (id, competition_code, season, utc_date, status, matchday,
            home_team_id, home_team_name, away_team_id, away_team_name,
            home_score, away_score, source, raw_json, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, comp, 2026, "2026-10-26T22:30:00Z", "NS", 32,
         "1", "A队", "2", "B队", None, None, "football-data.org", "{}", "now"),
    )


def _seed_pred(conn, fixture_id: str):
    conn.execute(
        """INSERT INTO predictions
           (fixture_id, home, away, kickoff, home_prob, draw_prob, away_prob,
            result, level_key)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (fixture_id, "A队", "B队", "2026-10-26T22:30:00Z",
         0.5, 0.25, 0.25, "主胜", "low"),
    )


def test_prediction_with_fd_prefix_is_joined(repo):
    """备用源同步的比赛 + 带 fd- 前缀的预测，必须能连上。

    这是线上真实形态：matches.id=555051，predictions.fixture_id='fd-555051'。
    """
    conn = repo._connect()
    _seed_match(conn, 555051)
    _seed_pred(conn, "fd-555051")
    conn.commit()

    rows = repo.matches_with_predictions("2026-10-26")
    assert rows, "未返回任何比赛"
    assert rows[0]["prediction"] is not None, (
        "带 fd- 前缀的预测没被连上，看板会显示「尚未生成预测」")
    assert rows[0]["prediction"]["result"] == "主胜"


def test_prediction_with_plain_id_still_joined(repo):
    """主源的纯数字 ID 必须仍然能连上（不能为了修前缀把原路径弄坏）。"""
    conn = repo._connect()
    _seed_match(conn, 1575523)
    _seed_pred(conn, "1575523")
    conn.commit()

    rows = repo.matches_with_predictions("2026-10-26")
    assert rows and rows[0]["prediction"] is not None


def test_match_without_prediction_is_null(repo):
    """确实没算过的比赛，prediction 仍应是 None——不能凭空造一张卡片。"""
    conn = repo._connect()
    _seed_match(conn, 999999)
    conn.commit()

    rows = repo.matches_with_predictions("2026-10-26")
    assert rows and rows[0]["prediction"] is None
