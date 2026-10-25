"""/prediction/{fixture_id} 端点验证 / Checks for the prediction-detail endpoint.

用内存 SQLite 造真实结构的 predictions 行，验证：
  1. 落盘派生量（λ / 大小球 / BTTS / 最可能比分）能被正确读出
  2. inputs 解析失败时降级为 None，而不是整条丢弃
  3. 未生成预测的比赛返回 404（「没算」≠「算了但缺项」）
  4. 阵容与赔率取不到时保留原因，不影响其余字段展示
  5. 方向沿用落盘的 result，不重新推导

不联网；实时客户端用替身。
"""
from __future__ import annotations

import asyncio
import json

import pytest

import api as apimod
from repository import PredictionRepository


@pytest.fixture(autouse=True)
def _reset_client():
    apimod._LIVE_CLIENT = None
    yield
    apimod._LIVE_CLIENT = None


class FakeClient:
    """主源替身：可分别控制阵容/赔率的成败。"""

    def __init__(self, lineups=None, odds=None, raise_odds=False):
        self._lineups = lineups if lineups is not None else []
        self._odds = odds
        self._raise_odds = raise_odds

    async def get_lineups(self, fid):
        return self._lineups

    async def get_odds(self, fid):
        if self._raise_odds:
            raise RuntimeError("赔率端点限流")
        return self._odds


def _seed(db_path, **over):
    """建库并插入一条预测。"""
    repo = PredictionRepository(db_path)
    row = {
        "fixture_id": "12345",
        "league": "140",
        "home": "Real Sociedad",
        "away": "Getafe",
        "kickoff": "2026-10-26T19:00:00+00:00",
        "home_prob": 0.57, "draw_prob": 0.23, "away_prob": 0.20,
        "best_score": "1-0",
        "result": "home",
        "level_key": "medium",
        "created_at": "2026-10-26T10:00:00+00:00",
        "inputs": json.dumps({
            "lambda_home": 1.20, "lambda_away": 0.75,
            "over_2_5": 0.41, "btts": 0.48,
            "top_scores": [["1-0", 0.19], ["2-0", 0.14]],
            "data_completeness": "部分",
            "odds_error": None,
        }),
    }
    row.update(over)
    conn = repo._connect()
    conn.execute(
        "INSERT INTO predictions(fixture_id, league, home, away, kickoff, "
        "home_prob, draw_prob, away_prob, best_score, result, level_key, "
        "inputs, created_at) VALUES(:fixture_id,:league,:home,:away,:kickoff,"
        ":home_prob,:draw_prob,:away_prob,:best_score,:result,:level_key,"
        ":inputs,:created_at)", row)
    conn.commit()
    return repo


async def _call(fid, tmp_path):
    apimod._DB_PATH = str(tmp_path / "t.db")
    resp = await apimod.prediction_detail(fid)
    return json.loads(resp.body)


def test_detail_exposes_stored_derived_values(tmp_path):
    db = tmp_path / "t.db"
    _seed(str(db))
    apimod._LIVE_CLIENT = FakeClient()
    data = asyncio.run(_call("12345", tmp_path))

    assert data["result"] == "home"
    assert data["best_score"] == "1-0"
    assert data["level_key"] == "medium"
    # λ / 大小球 / BTTS 都来自预测时落盘，不应重新计算
    assert data["lambda_home"] == pytest.approx(1.20)
    assert data["lambda_away"] == pytest.approx(0.75)
    assert data["over_2_5"] == pytest.approx(0.41)
    assert data["btts"] == pytest.approx(0.48)
    assert data["top_scores"][0][0] == "1-0"


def test_broken_inputs_degrades_gracefully(tmp_path):
    db = tmp_path / "t.db"
    _seed(str(db), inputs="{not json")
    apimod._LIVE_CLIENT = FakeClient()
    data = asyncio.run(_call("12345", tmp_path))

    # 概率与方向仍在——缺派生量只该少几行，不该整条不展示
    assert data["home_prob"] == pytest.approx(0.57)
    assert data["result"] == "home"
    assert data["over_2_5"] is None
    assert data["lambda_home"] is None


def test_missing_prediction_is_404(tmp_path):
    (tmp_path / "t.db").touch()
    db = tmp_path / "t.db"
    _seed(str(db))
    apimod._LIVE_CLIENT = FakeClient()
    with pytest.raises(Exception) as exc:
        asyncio.run(_call("99999", tmp_path))
    assert "404" in str(exc.value)


def test_lineups_and_odds_failure_keeps_reason(tmp_path):
    db = tmp_path / "t.db"
    _seed(str(db))
    apimod._LIVE_CLIENT = FakeClient(raise_odds=True)
    data = asyncio.run(_call("12345", tmp_path))

    # 赔率挂了，方向/比分/大小球仍要能看
    assert data["result"] == "home"
    assert data["over_2_5"] == pytest.approx(0.41)
    assert data["odds"] is None
    assert "odds" in data["errors"]


def test_odds_normalized_when_available(tmp_path):
    db = tmp_path / "t.db"
    _seed(str(db))
    raw_odds = [{"bookmakers": [
        {"name": "B365", "bets": [{"id": 1, "name": "Match Winner", "values": [
            {"value": "Home", "odd": "2.10"}, {"value": "Draw", "odd": "3.40"},
            {"value": "Away", "odd": "3.80"}]}]},
        {"name": "Pin", "bets": [{"id": 1, "name": "Match Winner", "values": [
            {"value": "Home", "odd": "2.05"}, {"value": "Draw", "odd": "3.50"},
            {"value": "Away", "odd": "3.90"}]}]},
    ]}]
    apimod._LIVE_CLIENT = FakeClient(odds=raw_odds)
    data = asyncio.run(_call("12345", tmp_path))

    assert data["odds"]["home"] == pytest.approx(2.075, abs=1e-6)
    assert sum(data["odds"]["implied"].values()) == pytest.approx(1.0, abs=1e-3)


def test_no_client_reports_reason(tmp_path):
    db = tmp_path / "t.db"
    _seed(str(db))
    apimod._LIVE_CLIENT = False
    data = asyncio.run(_call("12345", tmp_path))
    assert data["lineups"] == []
    assert "client" in data["errors"]
