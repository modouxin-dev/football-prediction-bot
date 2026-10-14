"""赛前刷新 / Pre-match refresh.

为什么要有这批测试（它们锁的不是实现细节，而是一个真实的事故）：
    首发阵容按官方说明要开赛前 20–40 分钟才公布；自动预测 6 小时一轮。
    两者节奏对不上时——比赛 19:45 开踢、自动预测落在 14:00 与 20:00——
    那场比赛在 14:00 拿不到阵容，20:00 又已开赛被排除，
    **阵容信号在线上一次都不会触发**。代码写了、单测过了，生产里是死代码。

    Lineups are published 20–40 min before kickoff; auto-predict runs every
    6 h. When the two cadences miss each other, the lineup signal never fires
    in production even though it is fully implemented and unit-tested.

本模块锁住的四件事：
    1. 只有进入窗口的比赛会被重算，窗口外的留给自动预测（否则额度翻倍）
    2. 已开赛的比赛**绝不**重算（否则赛前预测变事后诸葛亮，复盘结论作废）
    3. 拿到阵容后打标记，下一轮跳过（否则每 15 分钟重复请求同一批比赛）
    4. 任何一场失败都不影响其他场，也不向上抛
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import main
from config import load_settings
from repository import PredictionRepository
from service import PREMATCH_LINEUP_WINDOW, PREMATCH_REFRESH_CAP, PredictionService
from signal_ab import CHANGE_APPLIED, SIGNAL_LINEUP
from tests.sample_data import FakeAPI, fixture, standings_response
from api_client import flatten_standings

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}


def run(coro):
    return asyncio.run(coro)


def _lineup(fixture_id: int, team_id: int, formation: str = "4-4-2") -> list[dict]:
    """一份"够用"的首发：11 人 + 阵型，足以算出攻守倾向。

    ⚠️ 默认 4-4-2 是**中性基准**，tilt 恒为 0、因子恒等中性 —— 用来测
    "阵容拿到了但对预测没有影响"很合适，但想测"阵容真的起了作用"
    （写 applied 行、打标记、下一轮跳过）必须换非中性阵型，如 4-3-3。
    """
    def side(tid: int) -> dict:
        players = [{"player": {"id": tid * 100 + i, "name": f"P{i}"}}
                   for i in range(11)]
        return {"team": {"id": tid}, "formation": formation,
                "startXI": [{"player": p["player"]} for p in players],
                "coach": {"id": tid, "name": "C"}}
    return [side(team_id), side(team_id + 1)]


class RefreshAPI(FakeAPI):
    """在 FakeAPI 上补赛前信号端点，并记录阵容被请求了哪些比赛。"""

    def __init__(self, *args, lineups=None, injuries=None,
                 predict_boom: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._lineups = lineups if lineups is not None else {}
        self._injuries = injuries if injuries is not None else []
        self.predict_boom = predict_boom
        self.lineup_calls: list[int] = []
        self.injury_calls: list[int] = []

    async def get_lineups(self, fixture_id):
        self.lineup_calls.append(fixture_id)
        return self._lineups.get(fixture_id, [])

    async def get_injuries(self, fixture_id, **kwargs):
        self.injury_calls.append(fixture_id)
        return self._injuries


def _fx(fid: int, minutes: float, home=1, away=2) -> dict:
    kickoff = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return fixture(fid, home, f"主队{home}", away, f"客队{away}", kickoff)


@pytest.fixture()
def repo(tmp_path):
    return PredictionRepository(str(tmp_path / "refresh.db"))


def _service(repo, api, **env_extra):
    settings = load_settings({**ENV, **env_extra})
    svc = PredictionService(settings, api)
    svc.repo = repo
    return svc


# ── 一、只重算窗口内的比赛 ──────────────────────────────────


def test_only_matches_inside_window_are_refreshed(repo):
    """窗口内 1 场 + 窗口外 1 场 → 只有窗口内那场被重算并请求阵容。

    这是本任务存在的全部意义：如果它也把 5 小时后的比赛一起算了，
    那就只是"又跑了一次自动预测"，阵容照样拿不到。
    """
    near, far = _fx(101, 20), _fx(102, 5 * 60)
    repo.save_matches("PL", [near, far], source="api-football")
    api = RefreshAPI({2026: [near, far]})
    svc = _service(repo, api)

    stats = run(svc.prematch_refresh())

    assert stats["checked"] == 1
    assert stats["refreshed"] == 1
    assert api.lineup_calls == [101]  # 只对进入窗口的那场问阵容


def test_window_is_configurable(repo):
    """窗口参数必须真的生效，而不是写死 45 分钟。"""
    fx = _fx(101, 90)  # 默认窗口外，2 小时窗口内
    repo.save_matches("PL", [fx], source="api-football")
    api = RefreshAPI({2026: [fx]})
    svc = _service(repo, api)

    assert run(svc.prematch_refresh())["checked"] == 0
    assert run(svc.prematch_refresh(window_seconds=2 * 3600))["checked"] == 1


# ── 二、已开赛绝不重算（红线） ──────────────────────────────


def test_started_match_is_never_refreshed(repo):
    """已开赛的比赛再补预测 = 事后诸葛亮，会直接毁掉复盘与回测结论。"""
    started = _fx(101, -5)          # 5 分钟前开踢
    finished = _fx(102, -90, 3, 4)  # 更早
    finished["fixture"]["status"]["short"] = "FT"
    repo.save_matches("PL", [started, finished], source="api-football")
    api = RefreshAPI({2026: [started, finished]})
    svc = _service(repo, api)

    stats = run(svc.prematch_refresh(window_seconds=10 * 3600))

    assert stats["checked"] == 0
    assert stats["refreshed"] == 0
    assert api.lineup_calls == []


def test_finished_match_inside_window_is_skipped(repo):
    """状态已是 FT 但开赛时间仍在未来（数据滞后）时也必须跳过。"""
    fx = _fx(101, 10)
    fx["fixture"]["status"]["short"] = "FT"
    repo.save_matches("PL", [fx], source="api-football")
    api = RefreshAPI({2026: [fx]})

    assert run(_service(repo, api).prematch_refresh())["checked"] == 0


# ── 三、拿到阵容后打标记，下一轮跳过 ────────────────────────


def test_second_run_skips_fixture_with_lineups_already_applied(repo):
    """同一场比赛不该每 15 分钟被重复请求一次阵容（额度是钱买的）。

    PREMATCH_TILT 必须 > 0 才谈得上"标记"：默认 0 时阵型倾向不启用，
    阵容因子恒等中性，capture 刻意不写 applied 行（写"强度 0、两版概率
    完全相同"的空行会把统计稀释成"信号没用"），标记也就无从谈起。
    """
    fx = _fx(101, 20)
    repo.save_matches("PL", [fx], source="api-football")
    api = RefreshAPI({2026: [fx]}, lineups={101: _lineup(101, 1, "4-3-3")})
    svc = _service(repo, api, PREMATCH_TILT="0.2")

    first = run(svc.prematch_refresh())
    assert first["refreshed"] == 1
    assert api.lineup_calls == [101]

    second = run(svc.prematch_refresh())
    assert second["skipped"] == 1
    assert second["refreshed"] == 0
    assert api.lineup_calls == [101]  # 没有第二次请求


def test_fixture_without_lineups_is_retried_next_run(repo):
    """官方还没公布阵容 → 没标记 → 下一轮继续试（这才是轮询的意义）。"""
    fx = _fx(101, 20)
    repo.save_matches("PL", [fx], source="api-football")
    api = RefreshAPI({2026: [fx]}, lineups={})  # 一直拿不到
    svc = _service(repo, api)

    assert run(svc.prematch_refresh())["refreshed"] == 1
    assert run(svc.prematch_refresh())["refreshed"] == 1
    assert api.lineup_calls == [101, 101]


def test_marker_comes_from_signal_ab_ledger(repo):
    """标记必须落在 signal_ab 的 applied 行上，而不是另设一份真值。"""
    fx = _fx(101, 20)
    repo.save_matches("PL", [fx], source="api-football")
    api = RefreshAPI({2026: [fx]}, lineups={101: _lineup(101, 1, "4-3-3")})
    svc = _service(repo, api, PREMATCH_TILT="0.2")
    run(svc.prematch_refresh())

    rows = [r for r in repo.signal_ab_for_fixture(101)
            if r["signal_type"] == SIGNAL_LINEUP]
    assert rows and rows[0]["change_type"] == CHANGE_APPLIED
    assert svc._lineup_already_applied(101) is True


def test_missing_marker_never_blocks_a_refresh(repo):
    """标记读不出来时宁可多请求一次，也不能永远不再取阵容。"""
    svc = _service(repo, RefreshAPI({2026: []}))
    broken = SimpleNamespace(signal_ab_for_fixture=lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("boom")))
    svc.repo = broken
    assert svc._lineup_already_applied(101) is False


# ── 四、开关与容错 ──────────────────────────────────────────


def test_disabled_when_prematch_off(repo):
    """总开关关闭时一轮都不跑，且说清原因（不能静默返回空）。"""
    fx = _fx(101, 20)
    repo.save_matches("PL", [fx], source="api-football")
    api = RefreshAPI({2026: [fx]})
    svc = _service(repo, api, PREMATCH_ENABLED="0")

    stats = run(svc.prematch_refresh())

    assert stats["refreshed"] == 0
    assert api.lineup_calls == []
    assert "PREMATCH_ENABLED" in stats.get("skipped_reason", "")


def test_single_match_failure_does_not_stop_the_batch(repo):
    """一场建模/预测失败，其余照常；失败要有留痕，不能吞掉。"""
    near = [_fx(101, 10), _fx(102, 15, 3, 4)]
    repo.save_matches("PL", near, source="api-football")
    api = RefreshAPI({2026: near})
    svc = _service(repo, api)

    async def boom(model, kickoff, fx, *_a, **_k):
        if (fx.get("fixture") or {}).get("id") == 101:
            raise RuntimeError("boom")
        return await PredictionService._predict_one(svc, model, kickoff, fx)

    svc._predict_one = boom  # type: ignore[assignment]
    stats = run(svc.prematch_refresh())

    assert stats["refreshed"] == 1
    assert any("101" in e for e in stats["errors"])


def test_cap_limits_one_round(repo):
    """一轮最多重算 cap 场：超出的按开赛先后排序后留到下一轮。"""
    many = [_fx(200 + i, 5 + i) for i in range(PREMATCH_REFRESH_CAP + 3)]
    repo.save_matches("PL", many, source="api-football")
    api = RefreshAPI({2026: many})
    svc = _service(repo, api)

    stats = run(svc.prematch_refresh())

    assert stats["refreshed"] == PREMATCH_REFRESH_CAP
    assert stats["checked"] == PREMATCH_REFRESH_CAP


def test_empty_schedule_is_not_an_error(repo):
    """窗口内没比赛是常态（一天里大部分时间都如此），不能记成错误。"""
    api = RefreshAPI({2026: []})
    stats = run(_service(repo, api).prematch_refresh())
    assert stats["refreshed"] == 0 and stats["errors"] == []


# ── 五、调度注册 ────────────────────────────────────────────


def test_job_registered_when_enabled():
    app = main.build_application(load_settings({**ENV, "PREMATCH_REFRESH_MINUTES": "10"}))
    assert len(app.job_queue.get_jobs_by_name("prematch_refresh")) == 1


def test_job_not_registered_when_interval_is_zero():
    """填 0 = 明确不要这个任务，不能悄悄按默认值注册。"""
    app = main.build_application(load_settings({**ENV, "PREMATCH_REFRESH_MINUTES": "0"}))
    assert app.job_queue.get_jobs_by_name("prematch_refresh") == ()


def test_job_not_registered_when_prematch_disabled():
    app = main.build_application(
        load_settings({**ENV, "PREMATCH_ENABLED": "0", "PREMATCH_REFRESH_MINUTES": "10"}))
    assert app.job_queue.get_jobs_by_name("prematch_refresh") == ()


def test_job_registered_without_chat_id():
    """刷新是为了数据闭环，不是推送——没配群号也必须跑。"""
    env = {k: v for k, v in ENV.items() if k not in ("CHAT_ID", "ADMIN_ID")}
    app = main.build_application(load_settings(env))
    assert len(app.job_queue.get_jobs_by_name("prematch_refresh")) == 1


# ── 六、配置解析 ────────────────────────────────────────────


def test_refresh_minutes_default_and_bounds():
    assert load_settings(ENV).prematch_refresh_minutes == 15
    assert load_settings({**ENV, "PREMATCH_REFRESH_MINUTES": "30"}).prematch_refresh_minutes == 30
    # 越界与乱码都要回退默认，不能让 999 变成"每 999 分钟"或让 abc 崩掉
    assert load_settings({**ENV, "PREMATCH_REFRESH_MINUTES": "999"}).prematch_refresh_minutes == 15
    assert load_settings({**ENV, "PREMATCH_REFRESH_MINUTES": "abc"}).prematch_refresh_minutes == 15


def test_window_constant_matches_official_publish_time():
    """45 分钟窗口对应官方"赛前 20–40 分钟公布"，不是随手取的数。"""
    assert PREMATCH_LINEUP_WINDOW == 45 * 60
