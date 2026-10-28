"""自动预测的「重算冷却」与自动预测日志的参数错位修复。

这两件事都来自实测，不是设计推演：

1. 日志参数错位（scheduler.auto_predict_job）
   `joblog.ok(...)` 曾被写进 `log.info()` 的实参里，而格式串只有 3 个
   占位符。logging 在格式化时抛错、把错误打到 stderr，那条「落盘了多少场」
   的真实信息永远不出现在日志里，任务状态也没被记成成功。运维在后台
   看不到自动预测到底跑没跑。

2. 重算冷却（service.auto_predict_store）
   自动预测 6 小时一轮，但容器每次重启（first=120 秒）都会跑一次，
   而进程内存里的缓存在重启后清空 —— 新一轮会把上一轮刚算完的几十场
   原样重算一遍：输入没变、概率几乎没变，额度却照扣。实测两轮调用的
   请求序列完全一致。

本文件锁住「省下来」和「没省过头」两端：冷却内必须省，冷却过了必须照算。
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

import scheduler
from config import load_settings
from repository import PredictionRepository
from service import PredictionService
from tests.sample_data import FakeAPI, fixture

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def repo(tmp_path):
    return PredictionRepository(str(tmp_path / "cooldown.db"))


def _fx(fid: int, hours: float) -> dict:
    """hours 小时后开赛：放在窗口内但足够远，避开"临近开赛"的特殊处理。"""
    return fixture(fid, 1, "主队1", 2, "客队2",
                   datetime.now(timezone.utc) + timedelta(hours=hours))


def _service(repo, api, **env_extra):
    settings = load_settings({**ENV, **env_extra})
    svc = PredictionService(settings, api)
    svc.repo = repo
    return svc


# ── 一、冷却内必须省下来 ────────────────────────────────────


def test_second_run_inside_cooldown_sends_no_request(repo):
    """刚算过就再跑一轮（容器重启的典型场景）→ 一场都不该重算。"""
    fx = _fx(101, 30)
    repo.save_matches("PL", [fx], source="api-football")
    api = FakeAPI({2026: [fx]})
    svc = _service(repo, api)

    first = run(svc.auto_predict_store((39,), days=2))
    n_first = len(api.odds_calls)

    second = run(svc.auto_predict_store((39,), days=2))
    n_second = len(api.odds_calls) - n_first

    assert first["saved"] == 1, "第一轮必须真的算出并落盘"
    assert n_first >= 1, "第一轮必然要发请求取盘口"
    assert second["saved"] == 0
    assert n_second == 0, f"冷却内第二轮不应再发请求，实际发了 {n_second} 次"
    assert second["skipped_recent"] == 1, "省下的场次必须可观测"


def test_cooldown_is_observed_per_fixture(repo):
    """只有算过的那场被跳过，没算过的照算 —— 冷却不能一刀切。"""
    done, fresh = _fx(101, 30), _fx(102, 31)
    repo.save_matches("PL", [done], source="api-football")
    api = FakeAPI({2026: [done]})
    svc = _service(repo, api)
    run(svc.auto_predict_store((39,), days=2))
    n_before = len(api.odds_calls)

    # 新增一场没算过的比赛
    repo.save_matches("PL", [fresh], source="api-football")
    api.fixtures_by_season = {2026: [done, fresh]}
    stats = run(svc.auto_predict_store((39,), days=2))

    assert len(api.odds_calls) - n_before >= 1, "新比赛不该被冷却挡住"
    assert stats["saved"] == 1
    assert stats["skipped_recent"] == 1


# ── 二、冷却过了必须照算（防误伤，比省额度更重要）────────────


def test_recalculates_again_after_cooldown_expires(repo):
    """冷却期过后必须重算 —— 否则预测会静默停止更新。"""
    fx = _fx(101, 30)
    repo.save_matches("PL", [fx], source="api-football")
    api = FakeAPI({2026: [fx]})
    svc = _service(repo, api)
    run(svc.auto_predict_store((39,), days=2))
    n_first = len(api.odds_calls)

    # 把"上次重算时间"推回 10 小时前：远超 3 小时冷却
    stale = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    conn = repo._connect()
    conn.execute("UPDATE predictions SET updated_at=?", (stale,))
    conn.commit()
    conn.close()

    stats = run(svc.auto_predict_store((39,), days=2))
    assert len(api.odds_calls) - n_first >= 1, "冷却已过却没重算，预测会停更"
    assert stats["saved"] == 1
    assert stats["skipped_recent"] == 0


def test_uncalculated_match_is_never_skipped(repo):
    """库里没有重算时间的比赛一律当"没算过"：宁可多算，不能漏算。"""
    fx = _fx(101, 30)
    repo.save_matches("PL", [fx], source="api-football")
    api = FakeAPI({2026: [fx]})
    svc = _service(repo, api)

    stats = run(svc.auto_predict_store((39,), days=2))
    assert stats["saved"] == 1
    assert stats["skipped_recent"] == 0


# ── 三、repository 侧的判据 ─────────────────────────────────


def test_prediction_updated_map_reports_last_recalc(repo):
    """落盘后必须能读回"上次重算时间"，否则冷却无从判断。"""
    fx = _fx(101, 30)
    repo.save_matches("PL", [fx], source="api-football")
    api = FakeAPI({2026: [fx]})
    svc = _service(repo, api)
    run(svc.auto_predict_store((39,), days=2))

    got = repo.prediction_updated_map(["101"])
    assert "101" in got, f"读不到重算时间：{got}"
    assert (datetime.now(timezone.utc) - got["101"]).total_seconds() < 600


def test_prediction_updated_map_empty_for_unknown(repo):
    """没算过的比赛不出现在返回里 —— 调用方据此判定为"该算"。"""
    assert repo.prediction_updated_map(["999"]) == {}
    assert repo.prediction_updated_map([]) == {}


def test_prediction_updated_map_tolerates_dirty_value(repo):
    """一条脏时间不能把比赛永久锁死在"刚算过"。"""
    fx = _fx(101, 30)
    repo.save_matches("PL", [fx], source="api-football")
    api = FakeAPI({2026: [fx]})
    svc = _service(repo, api)
    run(svc.auto_predict_store((39,), days=2))

    conn = repo._connect()
    conn.execute("UPDATE predictions SET updated_at=?", ("not-a-time",))
    conn.commit()
    conn.close()

    assert repo.prediction_updated_map(["101"]) == {}


# ── 四、自动预测的日志必须真的打得出来 ──────────────────────


class _FakeService:
    def __init__(self) -> None:
        self.settings = types.SimpleNamespace(league_id=39, league_ids=(39,),
                                              db_path=None)

    async def auto_predict_store(self, ids, days=2):
        return {"saved": 7, "errors": [], "per_league": {}, "window_days": days}


def test_auto_predict_job_log_is_not_broken(caplog):
    """日志必须能被格式化出来 —— 参数错位时 getMessage() 会直接抛错。

    断言"能取出消息"而不是"没打印 Logging error"：logging 只在真正
    格式化时才报错，不打出来就发现不了，而线上恰恰是看不到这条消息。
    """
    svc = _FakeService()
    app = types.SimpleNamespace(bot_data={"service": svc})
    with caplog.at_level("INFO", logger="bot"):
        run(scheduler.auto_predict_job(types.SimpleNamespace(application=app)))

    records = [r for r in caplog.records if r.name == "bot"]
    assert records, "自动预测跑完却没有任何日志，无从判断它是否成功"
    msgs = [r.getMessage() for r in records]  # 参数错位会在这里抛 TypeError
    assert any("自动预测落盘：7 场" in m for m in msgs), f"落盘信息没打出来：{msgs}"
