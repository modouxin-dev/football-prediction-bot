"""赛程端点：每场比赛附带的队伍近况（近期战绩 / 实力分 / 排名）。

看板只给「谁对谁」是不够的——用户想知道两队最近什么状态。这些字段由
repository.recent_form() 与 elo_ratings 现算，本文件锁死四件事：

1. 战绩与实力分确实返回，且顺序由旧到新（与 Telegram 侧展示一致）；
2. **无前视偏差**：区间内的比赛不会被算进任何一队的战绩；
3. 多天（days=3）能跨日返回，且每天带自己的日期供前端分段；
4. 缺数据返回 null 而不是 0 —— 否则「还没回填」看起来像一支 0 分的弱队。

api.py 依赖可选包 fastapi；未安装时整体跳过。
"""
import pytest

fastapi = pytest.importorskip("fastapi", reason="未安装 requirements-web.txt 的可选依赖")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
import repository  # noqa: E402
from formatkit import team_short_name  # noqa: E402

DAY = "2026-10-04"
HOME_ID = "h1"
AWAY_ID = "a1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把 API 指向临时库，并绑定中文队名转换（lifespan 在测试里不跑）。"""
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "web.db"))
    monkeypatch.setattr(api, "_TEAM_CN", team_short_name)
    return TestClient(api.app)


def _seed(conn, mid, day, home_id, away_id, hs=None, aws=None, status="NS", comp="PL"):
    conn.execute(
        "INSERT OR REPLACE INTO matches "
        "(id, competition_code, season, utc_date, status, matchday,"
        " home_team_id, home_team_name, away_team_id, away_team_name,"
        " home_score, away_score, source, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, comp, 2026, f"{day}T19:00:00Z", status, 3,
         home_id, f"Home{mid}", away_id, f"Away{mid}",
         hs, aws, "test", "2026-10-04T00:00:00Z"),
    )


def _rating(conn, team_id, rating, comp="PL"):
    conn.execute(
        "INSERT OR REPLACE INTO elo_ratings "
        "(team_id, competition, rating, matches, updated_at) VALUES (?,?,?,?,?)",
        (team_id, comp, float(rating), 10, "2026-10-04T00:00:00Z"),
    )


def _conn(client):
    """拿到与 API 同一个临时库的裸连接，用于造数据。"""
    return repository.PredictionRepository(api._DB_PATH)._connect()


# ── 一、队伍近况确实返回 ───────────────────────────────────────────────────

def test_fixtures_returns_form_rating_and_rank(client):
    """近期战绩、胜负平、进失球、实力分、排名都要出现在每支球队上。"""
    conn = _conn(client)
    # 三场已完赛的历史：2 胜 1 负，进 5 失 3
    _seed(conn, 1, "2026-09-20", HOME_ID, AWAY_ID, 2, 1, status="FT")
    _seed(conn, 2, "2026-09-25", AWAY_ID, HOME_ID, 2, 0, status="FT")
    _seed(conn, 3, "2026-09-28", HOME_ID, AWAY_ID, 1, 2, status="FT")
    # 未来一场：主队是 HOME_ID
    _seed(conn, 9, DAY, HOME_ID, AWAY_ID)
    _rating(conn, HOME_ID, 1543.27)
    _rating(conn, AWAY_ID, 1601.0)
    conn.commit()

    data = client.get("/fixtures", params={"date": DAY}).json()
    m = data["leagues"][0]["matches"][0]
    home, away = m["home_team"], m["away_team"]

    assert [x["r"] for x in home["form"]] == ["W", "L", "L"]   # 2-1 胜 / 0-2 负 / 1-2 负
    assert (home["w"], home["d"], home["l"]) == (1, 0, 2)
    assert (home["gf"], home["ga"]) == (3, 5)
    assert [x["r"] for x in away["form"]] == ["L", "W", "W"]
    assert home["rating"] == 1543.3          # 保留 1 位
    assert away["rating"] == 1601.0
    assert (home["rank"], away["rank"]) == (2, 1)   # 分高者排第 1
    assert home["rank_total"] == 2
    assert m["matchday"] == 3


# ── 二、无前视偏差 ─────────────────────────────────────────────────────────

def test_form_excludes_matches_on_or_after_the_day(client):
    """当天及之后的比赛不算进战绩，否则等于用未来结果预测未来。"""
    conn = _conn(client)
    _seed(conn, 1, "2026-09-20", HOME_ID, AWAY_ID, 3, 0, status="FT")
    _seed(conn, 2, DAY, HOME_ID, AWAY_ID, 1, 1, status="FT")        # 同一天
    _seed(conn, 3, "2026-10-09", HOME_ID, AWAY_ID, 5, 0, status="FT")  # 之后
    _seed(conn, 9, DAY, HOME_ID, AWAY_ID)
    conn.commit()

    data = client.get("/fixtures", params={"date": DAY}).json()
    m = data["leagues"][0]["matches"][0]
    assert [x["date"] for x in m["home_team"]["form"]] == ["2026-09-20"]


def test_form_cutoff_is_range_start_not_end(client):
    """多天区间以**起始日**为界，避免区间内已踢完的场次污染后几天的状态。"""
    conn = _conn(client)
    _seed(conn, 1, "2026-09-20", HOME_ID, AWAY_ID, 1, 0, status="FT")
    _seed(conn, 2, "2026-10-05", HOME_ID, AWAY_ID, 4, 0, status="FT")  # 落在 3 天区间内
    _seed(conn, 9, "2026-10-06", HOME_ID, AWAY_ID)
    conn.commit()

    data = client.get("/fixtures", params={"date": DAY, "days": 3}).json()
    m = data["leagues"][0]["matches"][0]
    assert [x["date"] for x in m["home_team"]["form"]] == ["2026-09-20"]


# ── 三、跨天 ───────────────────────────────────────────────────────────────

def test_days_spans_multiple_dates(client):
    """days=2 要返回两天，且每场带自己的日期供前端分段。"""
    conn = _conn(client)
    _seed(conn, 9, DAY, HOME_ID, AWAY_ID)
    _seed(conn, 10, "2026-10-05", HOME_ID, AWAY_ID)
    conn.commit()

    data = client.get("/fixtures", params={"date": DAY, "days": 2}).json()
    assert data["total"] == 2
    assert (data["date_from"], data["date_to"]) == (DAY, "2026-10-05")
    assert sorted(m["date"] for m in data["leagues"][0]["matches"]) == [DAY, "2026-10-05"]


# ── 四、缺数据是 null，不是 0 ──────────────────────────────────────────────

def test_missing_form_and_rating_are_null(client):
    """库里没战绩/没 Elo 时必须为 null —— 显示成 0 会被误读成极弱的队。"""
    conn = _conn(client)
    _seed(conn, 9, DAY, HOME_ID, AWAY_ID)
    conn.commit()

    data = client.get("/fixtures", params={"date": DAY}).json()
    home = data["leagues"][0]["matches"][0]["home_team"]
    assert home["form"] is None
    assert home["rating"] is None
    assert home["rank"] is None


def test_empty_db_tells_user_to_wait_for_backfill(client):
    """空库要说明「还没回填」，而不是笼统地说今天没比赛。"""
    data = client.get("/fixtures", params={"date": DAY}).json()
    assert data["status"] == "empty"
    assert "回填" in data["message"]


def test_bad_date_returns_400(client):
    resp = client.get("/fixtures", params={"date": "10/04/2026"})
    assert resp.status_code == 400
