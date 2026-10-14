"""看板联赛过滤 / Dashboard league filter.

看板与自动预测必须看同一批比赛：预测只跑配置的联赛，看板若把本地库里
所有比赛都列出来（含历史同步残留的未配置联赛），那些比赛永远不会有预测，
每条都恒显示「尚未生成预测」——看起来像预测坏了，实际是不在预测范围内。
"""
import pytest

from repository import PredictionRepository

# 配置内的联赛（英超/西甲）与未配置联赛（哥伦比亚，历史同步残留）
CONFIGURED = (("PL", 1), ("PD", 2))
UNCONFIGURED = (("COL", 3),)

DAY = "2026-10-15"


def _seed(db_path, rows):
    repo = PredictionRepository(str(db_path))
    conn = repo._connect()
    for code, mid in rows:
        conn.execute(
            "INSERT OR REPLACE INTO matches "
            "(id, competition_code, season, utc_date, status, matchday, "
            " home_team_id, home_team_name, away_team_id, away_team_name, "
            " home_score, away_score, source, raw_json, updated_at) "
            "VALUES (?,?,2026,?,'NS',1,?,?,'a','Away',NULL,NULL,'test','{}','now')",
            (mid, code, f"{DAY}T19:00:00Z", f"h{mid}", f"Home{mid}"),
        )
    conn.commit()
    return repo


@pytest.fixture()
def repo(tmp_path):
    return _seed(tmp_path / "f.db", CONFIGURED + UNCONFIGURED)


def test_unfiltered_returns_every_league(repo):
    """不传 competitions 时必须返回全部——既有调用方行为不得改变。"""
    rows = repo.matches_with_predictions(DAY)
    assert {r["competition"] for r in rows} == {"PL", "PD", "COL"}


def test_filter_keeps_only_configured_leagues(repo):
    """传入配置联赛后，未配置联赛不得再出现在看板上。"""
    rows = repo.matches_with_predictions(DAY, competitions=("PL", "PD"))
    assert {r["competition"] for r in rows} == {"PL", "PD"}
    assert len(rows) == 2


def test_filter_single_league(repo):
    rows = repo.matches_with_predictions(DAY, competitions=("PD",))
    assert [r["competition"] for r in rows] == ["PD"]


def test_filter_matching_nothing_yields_empty_not_all(repo):
    """过滤条件一个都不匹配时必须是空，绝不能退化成「不过滤」返回全部。

    退化成全部会让过滤形同虚设：配置读错时看板照旧列出未配置联赛，
    而排查时只会看到「过滤没生效」，看不出是配置错了。
    """
    rows = repo.matches_with_predictions(DAY, competitions=("XX",))
    assert rows == []


def test_empty_tuple_is_treated_as_no_filter(repo):
    """空元组等同于不过滤——避免调用方传了空配置却把看板清空。"""
    rows = repo.matches_with_predictions(DAY, competitions=())
    assert len(rows) == 3


def test_limit_still_applies_with_filter(tmp_path):
    _seed(tmp_path / "g.db", [("PL", i) for i in range(1, 8)])
    repo = PredictionRepository(str(tmp_path / "g.db"))
    rows = repo.matches_with_predictions(DAY, limit=3, competitions=("PL",))
    assert len(rows) == 3


# ---- api.py: _active_competitions ----------------------------------------
fastapi = pytest.importorskip("fastapi", reason="未安装 requirements-web.txt 的可选依赖")

import api  # noqa: E402


def test_active_competitions_matches_configured_leagues(monkeypatch):
    """看板过滤用的分区代码必须来自配置的 LEAGUE_IDS，不能写死。"""
    monkeypatch.setenv("LEAGUE_IDS", "39,140,78")
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:test")
    monkeypatch.setenv("API_FOOTBALL_KEY", "k")
    codes = api._active_competitions()
    assert codes == ("PL", "PD", "BL1")


def test_active_competitions_returns_none_on_failure(monkeypatch):
    """读取失败时必须返回 None（不过滤），而不是抛错让看板 500。

    宁可多显示几场无关比赛，也不能因为配置异常让整个看板打不开。
    """
    monkeypatch.setenv("LEAGUE_IDS", "not-a-number")
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    assert api._active_competitions() is None


def test_fixtures_endpoint_excludes_unconfigured_league(monkeypatch, tmp_path):
    """端到端：/fixtures 不得返回未配置联赛的比赛。"""
    from fastapi.testclient import TestClient

    db = tmp_path / "api.db"
    _seed(db, CONFIGURED + UNCONFIGURED)
    monkeypatch.setattr(api, "_DB_PATH", str(db))
    monkeypatch.setenv("LEAGUE_IDS", "39,140")
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:test")
    monkeypatch.setenv("API_FOOTBALL_KEY", "k")

    with TestClient(api.app) as c:
        body = c.get(f"/fixtures?date={DAY}").json()

    comps = {g["competition"] for g in body["leagues"]}
    assert "COL" not in comps, f"未配置联赛仍出现在看板：{comps}"
    assert comps == {"PL", "PD"}
