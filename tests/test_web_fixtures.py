"""Web 看板：当日赛程端点 + 中文队名/联赛名。

覆盖三件事：
1. 本地库按日连表查出「比赛 + 预测」，未预测的比赛必须是 null 而非 0；
2. 分区代码两种写法（字母码 PL / 数字码 128）都能翻成中文联赛名；
3. /fixtures 按联赛分组，组数与总数自洽。

api.py 依赖可选包 fastapi；未安装时整体跳过。
"""
import pytest

fastapi = pytest.importorskip("fastapi", reason="未安装 requirements-web.txt 的可选依赖")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
import repository  # noqa: E402
from formatkit import team_short_name  # noqa: E402

DAY = "2026-10-04"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把 API 指向临时库，并绑定中文队名转换（lifespan 在测试里不跑）。"""
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "web.db"))
    monkeypatch.setattr(api, "_TEAM_CN", team_short_name)
    return TestClient(api.app)


def _seed_match(conn, mid, comp, home, away, *, utc_date=None, status="NS"):
    conn.execute(
        "INSERT OR REPLACE INTO matches "
        "(id, competition_code, season, utc_date, status, home_team_id, home_team_name,"
        " away_team_id, away_team_name, source, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mid, comp, 2026, utc_date or f"{DAY}T19:00:00Z", status,
         f"h{mid}", home, f"a{mid}", away, "test", "2026-10-04T00:00:00Z"),
    )


def _seed_prediction(conn, fixture_id, home, away, result="主胜"):
    conn.execute(
        "INSERT OR REPLACE INTO predictions "
        "(fixture_id, season, league, home, away, kickoff, model_version, source,"
        " level_key, result, home_prob, draw_prob, away_prob, best_score, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(fixture_id), 2026, "PL", home, away, f"{DAY}T19:00:00Z", "test",
         "test", "high", result, 0.62, 0.20, 0.18, "2-1", "2026-10-04T00:00:00Z"),
    )


# ── 一、连表查询 ────────────────────────────────────────────────────────────

def test_matches_with_predictions_joins_prediction(tmp_path):
    """已生成预测的比赛必须带上预测字段。"""
    repo = repository.PredictionRepository(str(tmp_path / "t1.db"))
    conn = repo._connect()
    _seed_match(conn, 1001, "PL", "Manchester City", "Liverpool")
    _seed_prediction(conn, 1001, "Manchester City", "Liverpool")
    conn.commit()

    rows = repo.matches_with_predictions(DAY)
    assert len(rows) == 1
    pred = rows[0]["prediction"]
    assert pred is not None, "有预测却没连上，join 键写错了"
    assert pred["result"] == "主胜"
    assert pred["home_prob"] == pytest.approx(0.62)


def test_matches_with_predictions_null_when_absent(tmp_path):
    """没算过的比赛必须是 null——「没算」和「算出来是 0」不能混为一谈。"""
    repo = repository.PredictionRepository(str(tmp_path / "t2.db"))
    conn = repo._connect()
    _seed_match(conn, 1002, "PL", "Arsenal", "Chelsea")
    conn.commit()

    rows = repo.matches_with_predictions(DAY)
    assert len(rows) == 1
    assert rows[0]["prediction"] is None


def test_matches_with_predictions_filters_by_day(tmp_path):
    """只返回指定日期的比赛，不能把前后几天的都带出来。"""
    repo = repository.PredictionRepository(str(tmp_path / "t3.db"))
    conn = repo._connect()
    _seed_match(conn, 2001, "PL", "A", "B", utc_date="2026-10-03T19:00:00Z")
    _seed_match(conn, 2002, "PL", "C", "D", utc_date=f"{DAY}T19:00:00Z")
    _seed_match(conn, 2003, "PL", "E", "F", utc_date="2026-10-05T19:00:00Z")
    conn.commit()

    rows = repo.matches_with_predictions(DAY)
    assert [r["fixture_id"] for r in rows] == ["2002"]


# ── 二、联赛码 → 中文名 ─────────────────────────────────────────────────────

def test_league_name_from_letter_code():
    """已收录联赛存字母码（英超 → PL）。"""
    assert api._league_name("PL") == "英格兰超级联赛"


def test_league_name_from_numeric_code():
    """未收录联赛 fallback 成数字串（阿甲 → "128"），也必须翻得出来。"""
    assert api._league_name("128") == "阿根廷甲级联赛"


def test_league_name_unknown_falls_back_to_raw():
    """翻不出来就原样返回，绝不编造联赛名。"""
    assert api._league_name("ZZZ") == "ZZZ"


def test_league_name_empty_is_classified():
    assert api._league_name("") == "未分类"


# ── 三、/fixtures 端点 ──────────────────────────────────────────────────────

def test_fixtures_groups_by_league(client, monkeypatch, tmp_path):
    """多联赛必须分组，且每组 count 与实际条数一致。"""
    db = tmp_path / "web.db"
    repo = repository.PredictionRepository(str(db))
    conn = repo._connect()
    _seed_match(conn, 3001, "PL", "Manchester City", "Liverpool")
    _seed_match(conn, 3002, "PL", "Arsenal", "Chelsea")
    _seed_match(conn, 3003, "128", "Racing Club", "Huracan")
    conn.commit()

    body = client.get(f"/fixtures?date={DAY}").json()
    assert body["status"] == "ok"
    assert body["total"] == 3
    names = {g["competition_cn"] for g in body["leagues"]}
    assert "英格兰超级联赛" in names
    assert "阿根廷甲级联赛" in names
    for group in body["leagues"]:
        assert group["count"] == len(group["matches"])


def test_fixtures_carries_chinese_team_names(client, tmp_path):
    """看板队名必须与 Telegram 一致，不能一边中文一边英文。"""
    db = tmp_path / "web.db"
    repo = repository.PredictionRepository(str(db))
    conn = repo._connect()
    _seed_match(conn, 4001, "PL", "Manchester City", "Liverpool")
    conn.commit()

    body = client.get(f"/fixtures?date={DAY}").json()
    match = body["leagues"][0]["matches"][0]
    assert match["home_cn"] == "曼城"
    assert match["away_cn"] == "利物浦"


def test_fixtures_unknown_team_keeps_original(client, tmp_path):
    """未收录的队显示英文原名，绝不按发音或子串编造中文名。

    队名必须真是「未收录」的：像 Estudiantes de Rio Cuarto 看着陌生，
    实际已收录（→里奥夸尔托），拿它当反例会得到一个假失败。
    """
    db = tmp_path / "web.db"
    repo = repository.PredictionRepository(str(db))
    conn = repo._connect()
    _seed_match(conn, 5001, "PL", "Nonexistent Town Rovers", "Aldosivi")
    conn.commit()

    body = client.get(f"/fixtures?date={DAY}").json()
    match = body["leagues"][0]["matches"][0]
    assert match["home_cn"] == "Nonexistent Town Rovers"
    assert match["away_cn"] == "阿尔多西维", "同一行里已收录的队仍应翻译"


def test_fixtures_empty_day_is_empty_status(client, tmp_path):
    """当天确实没比赛时，status 必须是 empty 而不是报错。"""
    body = client.get("/fixtures?date=2026-01-01").json()
    assert body["status"] == "empty"
    assert body["total"] == 0
    assert body["leagues"] == []


def test_audit_adds_chinese_names(client, tmp_path):
    """历史审计同样要中文化，且原名保留（前端已有渲染不能失效）。"""
    db = tmp_path / "web.db"
    repo = repository.PredictionRepository(str(db))
    conn = repo._connect()
    conn.execute(
        "INSERT OR REPLACE INTO predictions "
        "(fixture_id, season, league, home, away, kickoff, model_version, source,"
        " level_key, result, home_prob, draw_prob, away_prob, best_score,"
        " created_at, actual_home, actual_away, settled_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("9001", 2026, "PL", "Manchester City", "Liverpool", f"{DAY}T19:00:00Z",
         "test", "test", "high", "主胜", 0.6, 0.2, 0.2, "2-1",
         "2026-10-04T00:00:00Z", 2, 1, "2026-10-04T22:00:00Z"),
    )
    conn.commit()

    body = client.get("/audit?limit=10").json()
    item = body["items"][0]
    assert item["home_cn"] == "曼城"
    assert item["home"] == "Manchester City", "原名必须保留，否则旧前端会失效"
