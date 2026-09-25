"""P1-5：预测落盘与命中率统计 / Prediction persistence & hit-rate."""
from datetime import datetime, timezone, timedelta

from api_client import flatten_standings
from analyzer import MatchAnalyzer
from config import load_settings
from repository import PredictionRepository
from service import PredictionService
from tests.sample_data import fixture, standings_response

ENV = {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "ADMIN_ID": "1"}


def run(coro):
    import asyncio
    return asyncio.run(coro)


def now_dt(hours=2):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class API:
    season_in_use = 2026
    source_label = "API-Football"

    def __init__(self, rows):
        self.rows = rows

    async def get_fixtures(self, *a, **k):
        # 开赛时间设为过去，使记录进入「待结算」集合
        return [fixture(1001, 1, "Alpha FC", 2, "Beta & Sons", now_dt(-1))]

    async def get_standings(self, *a, **k):
        return self.rows

    async def get_odds(self, *a, **k):
        return []

    async def get_h2h(self, *a, **k):
        return []

    async def get_team_form(self, *a, **k):
        return []


def svc(tmp_path):
    s = load_settings({**ENV, "DB_PATH": str(tmp_path / "p.db")})
    service = PredictionService(s, API(flatten_standings(standings_response())), MatchAnalyzer())
    return service, s


# ---- 落盘 -------------------------------------------------------------------
def test_prediction_is_persisted_to_disk(tmp_path):
    """预测必须落盘到机器人存储，而不是只留在内存。"""
    service, s = svc(tmp_path)
    p = run(service.predict_fixture(1001, None))
    assert service.repo.persistent is True
    rows = service.repo.recent()
    assert len(rows) == 1
    assert rows[0]["fixture_id"] == str(p.fixture_id)
    assert (tmp_path / "p.db").exists()


def test_saving_twice_updates_same_record(tmp_path):
    """同一场比赛重复预测应更新，不产生重复记录。"""
    service, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    run(service.predict_fixture(1001, None))
    assert len(service.repo.recent()) == 1


def test_saved_record_keeps_model_version_and_probs(tmp_path):
    service, _ = svc(tmp_path)
    p = run(service.predict_fixture(1001, None))
    row = service.repo.recent()[0]
    assert row["model_version"] == p.model_version
    assert abs(row["home_prob"] + row["draw_prob"] + row["away_prob"] - 1.0) < 1e-6
    assert row["level_key"] in ("high", "medium", "low")


def test_falls_back_to_memory_when_dir_unwritable():
    """目录不可写时回退内存，机器人不能因此崩溃。"""
    repo = PredictionRepository("/proc/definitely/not/writable/p.db")
    assert repo.persistent is False
    assert repo.stats()["total"] == 0  # 统计仍可调用，不抛异常


# ---- 赛果回写与统计 ----------------------------------------------------------
def test_settle_and_hit_rate(tmp_path):
    """回写真实比分后能算出命中率。"""
    service, _ = svc(tmp_path)
    p = run(service.predict_fixture(1001, None))
    predicted = p.level["result"]
    # 构造与预测一致 / 不一致两种赛果
    home, away = (2, 1) if predicted == "主胜" else (1, 2)
    assert service.settle_result(1001, home, away) is True
    st = service.stats()
    assert st["total"] == 1
    assert st["hit"] == (1 if predicted == "主胜" else 0)


def test_stats_before_settling(tmp_path):
    service, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    st = service.stats()
    assert st["total"] == 0
    assert st["pending"] == 1
    assert st["rate"] is None  # 没有已结算数据时不编造命中率


def test_draw_is_counted(tmp_path):
    service, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    service.settle_result(1001, 1, 1)
    st = service.stats()
    assert st["total"] == 1


def test_pending_lists_unsettled(tmp_path):
    service, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    assert len(service.repo.pending()) == 1
    service.settle_result(1001, 3, 0)
    assert len(service.repo.pending()) == 0


def test_sync_results_writes_finished_matches(tmp_path):
    """定时任务：已完场的比赛自动回写比分。"""
    service, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    fx = fixture(1001, 1, "Alpha FC", 2, "Beta & Sons", now_dt(-3), status="FT")
    fx["goals"] = {"home": 2, "away": 0}
    done = run(service.sync_results([fx]))
    assert done == 1
    assert service.stats()["total"] == 1


def test_sync_results_ignores_unfinished(tmp_path):
    service, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    fx = fixture(1001, 1, "Alpha FC", 2, "Beta & Sons", now_dt(-3), status="NS")
    fx["goals"] = {"home": None, "away": None}
    assert run(service.sync_results([fx])) == 0


def test_sync_results_tolerates_fetch_failure(tmp_path):
    """取不到赛程时结算失败，但必须静默，不能抛异常。"""
    service, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))

    async def boom(*a, **k):
        raise RuntimeError("接口挂了")

    service.get_today_fixtures = boom
    assert run(service.sync_results()) == 0
