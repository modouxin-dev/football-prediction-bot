"""没有伤停时不再取球队名单（/players/squads）。

缺陷来自实测（scripts/quota_ledger.py 跑出来的账本）：
`_context_signals` 无条件为主客两队各取一次名单，而名单的唯一用途
是给 `weighted_absence_load` 提供位置加权。该函数在 `not injuries`
时直接 `return 0.0`（context.py:193）——**没有伤停时名单取回来也
完全不被使用**，等于每场预测白烧 2 次请求。合成场景下（12 场/轮）
一轮就是 24 次，占冷启动请求量的 43%；容器重启清空缓存后会再烧一遍。

本文件锁住两端：没伤停必须省，有伤停必须照取（省过头会让位置加权失效）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from config import load_settings
from repository import PredictionRepository
from service import PredictionService

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}


def run(coro):
    return asyncio.run(coro)


class SquadSpy:
    """只实现名单/球员统计的最小替身，记录被调用的球队 id。"""

    provider = "rapidapi"
    quota_remaining = "88"

    def __init__(self, squad=None, squad_error=False):
        self.squad = squad if squad is not None else [
            {"player": {"id": 9, "pos": "Defender"}},
            {"player": {"id": 8, "pos": "Attacker"}},
        ]
        self.squad_error = squad_error
        self.squad_calls: list[str] = []

    async def get_squad(self, team_id):
        self.squad_calls.append(str(team_id))
        if self.squad_error:
            raise RuntimeError("名单接口不可用")
        return self.squad

    async def get_team_player_stats(self, team_id, season):
        return []


@pytest.fixture()
def repo(tmp_path):
    return PredictionRepository(str(tmp_path / "squad.db"))


def _service(repo, api):
    settings = load_settings(dict(ENV))
    svc = PredictionService(settings, api)
    svc.repo = repo
    return svc


def _injuries(team_id: int):
    return [{"team": {"id": team_id},
             "player": {"id": 9, "name": "X", "type": "Injury"}}]


def test_no_squad_request_when_no_injuries(repo):
    """没有伤停 → 一次名单请求都不发（此前无条件发 2 次）。"""
    api = SquadSpy()
    svc = _service(repo, api)
    kickoff = datetime.now(timezone.utc) + timedelta(hours=2)

    loads, _xi, _ratings, _weather = run(
        svc._context_signals(kickoff, 1, 2, None, [], None, 2026))

    assert api.squad_calls == [], f"无伤停却取了名单：{api.squad_calls}"
    assert loads == {"home": 0.0, "away": 0.0}


def test_squad_requested_when_injuries_present(repo):
    """有伤停 → 名单照取，位置加权不能因为省额度而失效。"""
    api = SquadSpy()
    svc = _service(repo, api)
    kickoff = datetime.now(timezone.utc) + timedelta(hours=2)

    loads, _xi, _ratings, _weather = run(
        svc._context_signals(kickoff, 1, 2, None, _injuries(1), None, 2026))

    assert api.squad_calls == ["1", "2"], f"有伤停却没取名单：{api.squad_calls}"
    assert loads["home"] > 0, "主队有伤停却算不出缺阵负荷"
    assert loads["away"] == 0.0


def test_squad_failure_still_degrades_gracefully(repo):
    """名单接口失败 → 退化为按人数计，不能让预测抛错。"""
    api = SquadSpy(squad_error=True)
    svc = _service(repo, api)
    kickoff = datetime.now(timezone.utc) + timedelta(hours=2)

    loads, _xi, _ratings, _weather = run(
        svc._context_signals(kickoff, 1, 2, None, _injuries(1), None, 2026))

    assert api.squad_calls == ["1", "2"]
    assert loads["home"] > 0, "名单取不到时应退化为按人数计，而不是 0"
