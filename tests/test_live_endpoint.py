"""/live 端点端到端验证 / End-to-end checks for the live endpoint.

用真实形状的 API-Football 响应做替身，验证：
  1. 进行中比赛能正确筛出（NS/FT 被过滤）
  2. 阵容 startXI → start_xi 摊平正确
  3. 伤停按队分组（原始是一名球员一条）
  4. 赔率取中位数并给出去水后的隐含概率
  5. 三种「空」语义必须可区分：无比赛 / 套餐不支持 / 客户端不可用
  6. detail=0 不触发详情请求（省额度）

不联网、不依赖真实 Key；替身客户端完全可控。
"""
from __future__ import annotations

import asyncio
import json

import pytest

import api as apimod
from repository import PredictionRepository


@pytest.fixture(autouse=True)
def _isolate_cache():
    """/live 现在走 60 秒 TTL 的库级缓存，测试之间会互相污染：
    前一个用例写进去的结果会被下一个用例直接命中，
    导致「本该重新拉取」的场景拿到别人的旧数据。
    缓存是被测行为的一部分，所以只能在用例之间清库，
    不能在端点里关掉它。"""
    yield
    try:
        conn = PredictionRepository(apimod._DB_PATH)._connect()
        conn.execute("DELETE FROM external_cache")
        conn.commit()
    except Exception:
        pass


def mk_fx(fid, home, away, hs, as_, status="2H", elapsed=67):
    """构造一场 API-Football 形状的比赛。"""
    return {
        "fixture": {"id": fid, "date": "2026-10-25T19:00:00+00:00",
                    "status": {"short": status, "elapsed": elapsed}},
        "league": {"id": 140, "name": "La Liga", "code": "PD"},
        "teams": {"home": {"id": 541, "name": home},
                  "away": {"id": 532, "name": away}},
        "goals": {"home": hs, "away": as_},
    }


LINEUPS = [
    {"team": {"id": 541, "name": "Real Sociedad"}, "formation": "4-3-3",
     "startXI": [{"player": {"id": 1, "name": "Remiro", "pos": "G"}},
                 {"player": {"id": 2, "name": "Traore", "pos": "D"}},
                 {"player": {"id": 3, "name": "Zubeldia", "pos": "D"}}],
     "substitutes": []},
    {"team": {"id": 532, "name": "Getafe"}, "formation": "4-4-2",
     "startXI": [{"player": {"id": 11, "name": "Soria", "pos": "G"}}]},
]

# 原始伤停是一名球员一条，同队会重复出现
INJURIES = [
    {"team": {"id": 541, "name": "Real Sociedad"},
     "player": {"id": 9, "name": "Oyarzabal", "type": "Missing Fixture",
                "reason": "肌肉受伤"}},
    {"team": {"id": 541, "name": "Real Sociedad"},
     "player": {"id": 7, "name": "Barrenetxea", "reason": "膝盖"}},
    {"team": {"id": 532, "name": "Getafe"},
     "player": {"id": 21, "name": "Mata", "reason": "停赛"}},
]

ODDS = [{"bookmakers": [
    {"name": "Bet365", "bets": [{"id": 1, "name": "Match Winner", "values": [
        {"value": "Home", "odd": "2.10"}, {"value": "Draw", "odd": "3.40"},
        {"value": "Away", "odd": "3.80"}]}]},
    {"name": "Pinnacle", "bets": [{"id": 1, "name": "Match Winner", "values": [
        {"value": "Home", "odd": "2.05"}, {"value": "Draw", "odd": "3.50"},
        {"value": "Away", "odd": "3.90"}]}]},
]}]


class FakeClient:
    """DataSourceRouter 的替身：primary 指回自己，便于读 last_live_error。"""

    def __init__(self, rows, lineups=None, injuries=None, odds=None):
        self.rows = rows
        self.primary = self
        self.last_live_error = None
        self._lineups = LINEUPS if lineups is None else lineups
        self._injuries = INJURIES if injuries is None else injuries
        self._odds = ODDS if odds is None else odds

    async def get_live_fixtures(self):
        return self.rows

    async def get_lineups(self, fid):
        return self._lineups

    async def get_injuries(self, fid):
        return self._injuries

    async def get_odds(self, fid):
        return self._odds


@pytest.fixture(autouse=True)
def _reset_client():
    """每个用例前后都还原全局单例，避免互相污染。"""
    apimod._LIVE_CLIENT = None
    yield
    apimod._LIVE_CLIENT = None


async def _call(**kw):
    resp = await apimod.live(**kw)
    return json.loads(resp.body)


def test_live_returns_in_progress_matches_with_details():
    apimod._LIVE_CLIENT = FakeClient([mk_fx(111, "Real Sociedad", "Getafe", 1, 0)])
    data = asyncio.run(_call(detail=1))

    assert data["status"] == "ok"
    assert data["count"] == 1
    item = data["items"][0]

    # 比分：1-0，且不能把 None 当成 0
    assert item["home_score"] == 1
    assert item["away_score"] == 0
    # 状态中文映射
    assert item["status_short"] == "2H"
    assert item["status_cn"] == "下半场"
    assert item["elapsed"] == 67


def test_lineups_flattened_from_startxi():
    apimod._LIVE_CLIENT = FakeClient([mk_fx(1, "A", "B", 0, 0)])
    item = asyncio.run(_call(detail=1))["items"][0]

    assert len(item["lineups"]) == 2
    first = item["lineups"][0]
    assert first["formation"] == "4-3-3"
    # startXI（驼峰）必须摊平成 start_xi 且球员名提到顶层
    assert [p["name"] for p in first["start_xi"]] == ["Remiro", "Traore", "Zubeldia"]
    assert first["start_xi"][0]["pos"] == "G"


def test_injuries_grouped_by_team():
    apimod._LIVE_CLIENT = FakeClient([mk_fx(1, "A", "B", 0, 0)])
    item = asyncio.run(_call(detail=1))["items"][0]

    # 三名伤员分属两队 → 聚合后应为两队
    assert len(item["injuries"]) == 2
    by_team = {g["team"]: g["players"] for g in item["injuries"]}
    assert len(by_team["Real Sociedad"]) == 2   # 原始两条同队记录
    assert len(by_team["Getafe"]) == 1
    assert by_team["Getafe"][0]["reason"] == "停赛"


def test_odds_median_and_implied_sum_to_one():
    apimod._LIVE_CLIENT = FakeClient([mk_fx(1, "A", "B", 0, 0)])
    item = asyncio.run(_call(detail=1))["items"][0]

    odds = item["odds"]
    assert odds is not None
    # 两家报价取中位数：home (2.10, 2.05) → 2.075
    assert odds["home"] == pytest.approx(2.075, abs=1e-6)
    assert odds["n_bookmakers"] == 2
    # 去水后三个隐含概率之和必须≈1，否则用户会算出 >100%
    total = sum(odds["implied"].values())
    assert total == pytest.approx(1.0, abs=1e-3)


def test_only_in_progress_statuses_kept():
    apimod._LIVE_CLIENT = FakeClient([
        mk_fx(1, "A", "B", 0, 0, "NS"),   # 未开始 → 过滤
        mk_fx(2, "C", "D", 3, 1, "FT"),   # 已完场 → 过滤
        mk_fx(3, "E", "F", 1, 1, "HT"),   # 中场 → 保留
    ])
    data = asyncio.run(_call(detail=0))
    assert data["count"] == 1
    assert data["items"][0]["status_short"] == "HT"


def test_empty_distinguished_from_unsupported():
    # 空列表 + 无错误 → 此刻确实没比赛
    apimod._LIVE_CLIENT = FakeClient([])
    none_now = asyncio.run(_call(detail=0))
    assert none_now["status"] == "empty"
    assert "没有正在进行" in none_now["message"]

    # 空列表 + 有错误 → 套餐不支持，必须说清是权限问题
    denied = FakeClient([])
    denied.last_live_error = "套餐权限不足: live 能力未订阅"
    apimod._LIVE_CLIENT = denied
    unsupported = asyncio.run(_call(detail=0))
    assert unsupported["status"] == "unsupported"
    assert "套餐" in unsupported["message"]


def test_unavailable_client_does_not_raise():
    apimod._LIVE_CLIENT = False
    data = asyncio.run(_call())
    assert data["status"] == "unavailable"
    assert data["count"] == 0


def test_detail_zero_skips_detail_calls():
    apimod._LIVE_CLIENT = FakeClient([mk_fx(1, "A", "B", 1, 0)])
    item = asyncio.run(_call(detail=0))["items"][0]
    # detail=0 仍要有比分（这是本板块的核心），但不请求阵容/伤停/赔率
    assert item["home_score"] == 1
    assert item["lineups"] == []
    assert item["injuries"] == []
    assert item["odds"] is None


def test_truncation_reported_not_silent():
    many = [mk_fx(i, f"H{i}", f"A{i}", 0, 0) for i in range(20)]
    apimod._LIVE_CLIENT = FakeClient(many)
    data = asyncio.run(_call(detail=0))
    # 单轮上限存在，超出部分必须告知，不能悄悄砍掉
    assert data["count"] == apimod.LIVE_MAX_MATCHES
    assert data["truncated"] == 20 - apimod.LIVE_MAX_MATCHES
    assert data["message"]


def test_single_detail_failure_does_not_break_match():
    class Broken(FakeClient):
        async def get_odds(self, fid):
            raise RuntimeError("赔率端点限流")

    apimod._LIVE_CLIENT = Broken([mk_fx(1, "A", "B", 2, 1)])
    item = asyncio.run(_call(detail=1))["items"][0]
    # 赔率挂了不影响比分与阵容
    assert item["home_score"] == 2
    assert len(item["lineups"]) == 2
    assert item["odds"] is None
    # 失败原因必须留痕
    assert "odds" in item["errors"]
