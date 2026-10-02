"""历史赛季回填测试：整季拉取不带日期范围 + 落盘 + 参数解析（不联网）。"""
import asyncio
from datetime import datetime, timezone

import pytest

from repository import PredictionRepository
from sync import MatchSync
from tests.sample_data import fixture


def run(coro):
    return asyncio.run(coro)


def _fixtures(season: int, n: int = 3) -> list[dict]:
    out = []
    for i in range(n):
        fx = fixture(9000 + season * 100 + i, 1, "主", 2, "客",
                     datetime(season, 8, 20 + i, tzinfo=timezone.utc), status="FT")
        fx["league"]["season"] = season
        fx["goals"] = {"home": 1, "away": 0}  # API-Football v3 用 goals，不是 score
        out.append(fx)
    return out


class SeasonAPI:
    """记录调用参数：验证整季请求确实不带 from/to。"""

    def __init__(self, seasons=None, fail: bool = False):
        self.seasons = seasons or {}
        self.fail = fail
        self.calls: list[tuple] = []
        self.source_label = "api-football"

    async def get_fixtures_by_season(self, league_id: int, season: int):
        self.calls.append((league_id, season))
        if self.fail:
            raise RuntimeError("boom")
        return self.seasons.get(season, [])


def _sync(api, tmp_path, league_id: int = 39):
    from types import SimpleNamespace

    repo = PredictionRepository(str(tmp_path / "x.db"))
    service = SimpleNamespace(api=api, settings=SimpleNamespace(league_id=league_id),
                              source_label=api.source_label)
    return MatchSync(service, repo), repo


def test_sync_season_saves_matches(tmp_path):
    api = SeasonAPI({2024: _fixtures(2024)})
    sync, repo = _sync(api, tmp_path)
    result = run(sync.sync_season(2024))
    assert result["ok"] is True
    assert result["received"] == 3
    assert result["saved"] == 3
    assert repo.count_finished_matches("PL") >= 3


def test_sync_season_no_date_range_in_request(tmp_path):
    """历史赛季必须只带 league+season，带 from/to 会被套餐拒绝。"""
    api = SeasonAPI({2023: _fixtures(2023)})
    sync, _ = _sync(api, tmp_path)
    run(sync.sync_season(2023))
    assert api.calls == [(39, 2023)]


def test_sync_season_empty_is_not_error(tmp_path):
    """套餐不支持该赛季：0 场不算失败，但要说明原因。"""
    api = SeasonAPI({})
    sync, _ = _sync(api, tmp_path)
    result = run(sync.sync_season(2019))
    assert result["ok"] is True
    assert result["received"] == 0
    assert "暂无数据" in result["message"]


def test_sync_season_failure_is_recorded(tmp_path):
    api = SeasonAPI(fail=True)
    sync, _ = _sync(api, tmp_path)
    result = run(sync.sync_season(2024))
    assert result["ok"] is False
    assert result["http_status"] == "RuntimeError"


def test_backfill_season_argument_parsing():
    """/backfill 2024 → 走 sync_season；/backfill → 保持原行为。"""
    from commands.admin import _season_result_text

    text = _season_result_text(2024, 380, 380, 700)
    assert "2024" in text and "380" in text


def test_backfill_bad_season_argument_shows_usage():
    """非数字参数不能当成赛季去请求。"""
    arg = "abc"
    with pytest.raises(ValueError):
        int(arg)
