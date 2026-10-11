"""/backtest 端点与看板首页缓存策略的回归测试。

背景（真实缺口）：
    repository.py 早就有 save_backtest_run / backtest_history，
    scheduler.py 的 backtest_job 每天跑完也会落盘，但 api.py **没有**
    任何端点暴露它，web/index.html 也从不请求——结果是「回测跑了、数据
    落盘了、看板上却完全看不到」，用户只能靠猜。

    另一个坑：index.html 是单文件内联脚本，若被浏览器/代理缓存，
    服务端修好了用户拿到的仍是旧脚本，表现为「明明部署了还是打不开」。

依赖 fastapi，未安装时整体跳过（与 tests/test_api.py 同一口径）。
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="未安装 requirements-web.txt 的可选依赖")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
from repository import PredictionRepository  # noqa: E402

_RESULT = {
    "total_predictions": 1110,
    "correct_predictions": 597,
    "accuracy": 0.5378,
    "log_loss": 0.9692,
    "rps": 0.1962,
    "brier": 0.5768,
    "ece": 0.03,
    "corpus": {"db": 200, "history": 1140},
    "verdict": "显著优于泊松",
    "by_level": {},
    "calibration": [],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import paths

    db = tmp_path / "t.db"
    monkeypatch.setattr(paths, "DB_PATH", db)
    monkeypatch.setattr(api, "_DB_PATH", str(db))
    return TestClient(api.app)


def test_backtest_empty_when_no_runs(client):
    """没跑过回测时必须返回 empty，绝不编造数字。"""
    data = client.get("/backtest").json()
    assert data["status"] == "empty"
    assert data["count"] == 0
    assert data["items"] == []
    assert data["message"], "空状态必须给出可执行提示"


def test_backtest_returns_persisted_run(client, tmp_path):
    """落盘后的回测结论必须能通过端点读到，字段与落盘值一致。"""
    repo = PredictionRepository(tmp_path / "t.db")
    rid = repo.save_backtest_run(
        competition="PL", variant="market", min_history=200,
        result=_RESULT, season=2024,
    )
    assert rid is not None

    data = client.get("/backtest").json()
    assert data["status"] == "ok"
    assert data["count"] == 1
    item = data["items"][0]
    assert item["n"] == 1110
    assert abs(item["accuracy"] - 0.5378) < 1e-9
    assert abs(item["log_loss"] - 0.9692) < 1e-9
    assert item["corpus_db"] == 200
    assert item["corpus_csv"] == 1140
    assert item["variant"] == "market"
    assert item["verdict"] == "显著优于泊松"


def test_backtest_respects_limit(client, tmp_path):
    repo = PredictionRepository(tmp_path / "t.db")
    for i in range(5):
        repo.save_backtest_run(
            competition="PL", variant="market", min_history=200,
            result=_RESULT, season=2024,
        )
    assert len(client.get("/backtest?limit=2").json()["items"]) == 2
    assert len(client.get("/backtest?limit=50").json()["items"]) == 5


def test_index_disables_caching(client):
    """首页必须禁用缓存。

    index.html 内联了全部脚本，缓存住旧版本会让用户一直看到
    「正在加载数据…」，而服务端其实早已修好。
    """
    resp = client.get("/")
    assert resp.status_code == 200
    cc = resp.headers.get("cache-control", "").lower()
    assert "no-store" in cc, f"首页未禁用缓存，cache-control={cc!r}"
