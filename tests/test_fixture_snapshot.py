"""赛程快照落库：历史推送卡片在缓存过期 / 机器人重启后仍可点开。

背景：赛程缓存 fx_cache 的 TTL 只有 10 分钟，比赛一旦开赛或跨天，
重新拉取的「今日赛程」里就没有它了，而用户手上的卡片还在。此前逻辑只查
「传入缓存 → 重新拉取今日赛程」两级，两级都落空就抛 KeyError，
上层于是提示「该场比赛已不在今日赛程中」——预测明明算过却打不开。
修复是加第三级：读库里保存的原始赛程快照。
"""
import asyncio

import pytest

from analyzer import MatchAnalyzer
from api_client import flatten_standings
from config import load_settings
from repository import PredictionRepository
from service import PredictionService
from tests.sample_data import fixture, standings_response

ENV = {"TELEGRAM_TOKEN": "1:a", "RAPID_API_KEY": "k", "ADMIN_ID": "1"}


def run(coro):
    return asyncio.run(coro)


class API:
    """可控的赛程接口：empty=True 时返回空，模拟「今日赛程里已无此场」。"""

    season_in_use = 2026
    source_label = "API-Football"

    def __init__(self, rows):
        self.rows = rows
        self.fx_calls = 0
        self.empty = False

    async def get_fixtures(self, *a, **k):
        self.fx_calls += 1
        if self.empty:
            return []
        return [fixture(1001, 1, "Alpha FC", 2, "Beta & Sons", now_dt(-1))]

    async def get_standings(self, *a, **k):
        return self.rows

    async def get_odds(self, *a, **k):
        return []

    async def get_h2h(self, *a, **k):
        return []

    async def get_team_form(self, *a, **k):
        return []


def now_dt(hours=2):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def svc(tmp_path):
    s = load_settings({**ENV, "DB_PATH": str(tmp_path / "p.db")})
    api = API(flatten_standings(standings_response()))
    return PredictionService(s, api, MatchAnalyzer()), s, api


# ---- 快照落库 ---------------------------------------------------------------
def test_fixture_snapshot_is_persisted(tmp_path):
    """算过的比赛必须把原始赛程快照写进库，否则缓存一过期就找不回来。"""
    service, s, _ = svc(tmp_path)
    p = run(service.predict_fixture(1001, None))
    snap = service.repo.get_fixture_json(p.fixture_id)
    assert isinstance(snap, dict) and snap, "赛程快照必须落库"
    assert str((snap.get("fixture") or {}).get("id")) == str(p.fixture_id)


def test_snapshot_survives_restart(tmp_path):
    """换一个仓储实例（等价机器人重启）仍能读回快照。"""
    service, s, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    fresh = PredictionRepository(str(tmp_path / "p.db"))
    assert fresh.get_fixture_json(1001), "重启后快照必须还在"


def test_missing_snapshot_returns_none_not_error(tmp_path):
    """没算过的比赛读快照返回 None，不能抛异常。"""
    service, s, _ = svc(tmp_path)
    assert service.repo.get_fixture_json(999999) is None


# ---- 三级查找 ---------------------------------------------------------------
def test_stale_card_opens_via_snapshot(tmp_path):
    """核心回归：今日赛程已无此场 + 缓存为空 → 仍能用快照打开。"""
    service, s, api = svc(tmp_path)
    run(service.predict_fixture(1001, None))  # 先算一次，产生快照
    api.empty = True  # 此后今日赛程里再也查不到这场
    # fixtures=[] 表示缓存里没有、且不再触发重新拉取（真实场景：缓存过期）
    p2 = run(service.predict_fixture(1001, []))
    assert str(p2.fixture_id) == "1001"


def test_stale_card_opens_when_cache_is_none(tmp_path):
    """缓存为 None（过期）且重新拉取也落空 → 同样用快照兜住。"""
    service, s, api = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    api.empty = True  # 缓存过期 + 重新拉取也落空
    p2 = run(service.predict_fixture(1001, None))
    assert str(p2.fixture_id) == "1001"
    assert api.fx_calls >= 1  # 确实尝试过重新拉取


def test_unknown_fixture_still_raises_keyerror(tmp_path):
    """快照兜底不能吞掉「真·不存在」：没有快照的比赛仍要抛 KeyError，
    否则会把 API 故障伪装成成功，用户看到的是空数据而不是错误提示。"""
    service, s, _ = svc(tmp_path)
    with pytest.raises(KeyError):
        run(service.predict_fixture(88888, []))


def test_cache_hit_does_not_touch_snapshot(tmp_path):
    """缓存命中时优先用缓存，不读库、不重新拉取。"""
    service, s, api = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    calls_before = api.fx_calls
    fx = [fixture(1001, 1, "Alpha FC", 2, "Beta & Sons", now_dt(-1))]
    run(service.predict_fixture(1001, fx))
    assert api.fx_calls == calls_before  # 命中缓存，零额外请求


def test_snapshot_not_wiped_by_resave(tmp_path):
    """重算预测时新快照为空，不能把库里已有的快照抹掉。"""
    service, s, _ = svc(tmp_path)
    run(service.predict_fixture(1001, None))
    p = service.get(1001)
    original = p.fixture
    p.fixture = {}  # 异常情形：本次没有赛程数据
    service.repo.save(p)
    assert service.repo.get_fixture_json(1001), "旧快照必须保留"
    p.fixture = original  # 还原，避免影响后续断言
