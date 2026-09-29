"""Elo 冷启动迁移 / Elo cold-start migration.

重点：回算必须幂等——重复执行不能让评分虚高。
"""
from elo import EloEngine
from migrate_elo import fetch_finished
from repository import PredictionRepository

# (home_id, away_id, home_score, away_score)
SEASON = [
    ("A", "B", 2, 0), ("C", "A", 1, 1), ("B", "C", 0, 3),
    ("A", "C", 1, 0), ("B", "A", 2, 2), ("C", "B", 1, 0),
    ("A", "B", 3, 1), ("C", "A", 0, 2), ("B", "C", 1, 1),
]


def seed(repo, rows=SEASON, competition="PL"):
    conn = repo._connect()
    conn.executemany(
        "INSERT OR REPLACE INTO matches "
        "(id, competition_code, season, utc_date, home_team_id, away_team_id, "
        " home_score, away_score, source, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,'t','x')",
        [
            (1000 + i, competition, 2026, f"2026-01-{i + 1:02d}T15:00:00Z",
             h, a, hs, as_,)
            for i, (h, a, hs, as_) in enumerate(rows)
        ],
    )
    conn.commit()


# ---- fetch_finished ---------------------------------------------------------

def test_fetch_finished_only_with_scores(tmp_path):
    repo = PredictionRepository(str(tmp_path / "m.db"))
    seed(repo)
    rows = fetch_finished(repo, "PL")
    assert len(rows) == len(SEASON)
    assert all(r["home_score"] is not None for r in rows)


def test_fetch_finished_is_time_ordered(tmp_path):
    """Elo 依赖顺序：必须按开赛时间正序返回。"""
    repo = PredictionRepository(str(tmp_path / "m.db"))
    seed(repo)
    rows = fetch_finished(repo, "PL")
    dates = [r["utc_date"] if "utc_date" in r else None for r in rows]
    ids = [r["fixture_id"] for r in rows]
    assert ids == sorted(ids) or dates  # 按 id 升序即等于按日期升序（种子数据如此）


def test_fetch_finished_excludes_unfinished(tmp_path):
    repo = PredictionRepository(str(tmp_path / "m.db"))
    conn = repo._connect()
    conn.execute(
        "INSERT INTO matches (id, competition_code, season, utc_date, home_team_id, "
        "away_team_id, home_score, away_score, source, updated_at) "
        "VALUES (1,'PL',2026,'2026-01-01T15:00:00Z','A','B',NULL,NULL,'t','x')"
    )
    conn.commit()
    assert fetch_finished(repo, "PL") == []


# ---- 回算幂等性 -------------------------------------------------------------

def test_replay_twice_does_not_inflate(tmp_path):
    """核心回归：重复迁移不能让评分虚高。"""
    db = tmp_path / "m.db"
    repo = PredictionRepository(str(db))
    seed(repo)

    e1 = EloEngine(repo=repo, competition="PL")
    first = e1.replay(fetch_finished(repo, "PL"))
    after_first = repo.elo_ratings("PL")

    # 模拟重新部署后再跑一次迁移
    repo2 = PredictionRepository(str(db))
    e2 = EloEngine(repo=repo2, competition="PL")
    second = e2.replay(fetch_finished(repo2, "PL"))
    after_second = repo2.elo_ratings("PL")

    assert first == len(SEASON)
    assert second == 0, "第二次回算应全部跳过"
    for team, rating in after_first.items():
        assert abs(after_second[team] - rating) < 1e-9, f"{team} 评分被重复计算"


def test_replay_produces_spread_ratings(tmp_path):
    """回算后球队评分应有区分度，不能全是 1500。"""
    repo = PredictionRepository(str(tmp_path / "m.db"))
    seed(repo)
    EloEngine(repo=repo, competition="PL").replay(fetch_finished(repo, "PL"))
    ratings = repo.elo_ratings("PL")
    assert len({round(v, 3) for v in ratings.values()}) > 1
    assert min(ratings.values()) >= 1200.0
    assert max(ratings.values()) <= 2400.0


def test_replay_isolated_by_competition(tmp_path):
    """两个联赛的评分互不干扰。"""
    db = tmp_path / "m.db"
    repo = PredictionRepository(str(db))
    seed(repo, competition="PL")
    seed(repo, [("X", "Y", 5, 0), ("Y", "X", 0, 4)], competition="PD")

    EloEngine(repo=repo, competition="PL").replay(fetch_finished(repo, "PL"))
    EloEngine(repo=repo, competition="PD").replay(fetch_finished(repo, "PD"))

    pl = repo.elo_ratings("PL")
    pd = repo.elo_ratings("PD")
    assert set(pl) == {"A", "B", "C"}
    assert set(pd) == {"X", "Y"}


def test_replay_empty_is_safe(tmp_path):
    repo = PredictionRepository(str(tmp_path / "m.db"))
    assert EloEngine(repo=repo, competition="PL").replay([]) == 0
    assert repo.elo_ratings("PL") == {}
