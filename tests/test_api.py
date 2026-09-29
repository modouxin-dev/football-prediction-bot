"""Web API 桥接层 / API bridge.

api.py 依赖可选包 fastapi；未安装时整体跳过，不让 CI 因缺可选依赖而变红。
"""
import pytest

fastapi = pytest.importorskip("fastapi", reason="未安装 requirements-web.txt 的可选依赖")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
import repository  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把 API 指向临时库，避免污染真实数据。"""
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "api.db"))
    return TestClient(api.app)


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "db_path" in body and "persistent" in body


def test_health_reports_persistence_state(client):
    """持久化字段必须是布尔值——Web 与机器人共用同一个库，这个信号很关键。"""
    body = client.get("/health").json()
    assert isinstance(body["persistent"], bool)


def test_stats_returns_numbers(client):
    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    for key in ("settled", "hit", "pending", "streak"):
        assert isinstance(body[key], int), key


def test_stats_by_level_is_wellformed(client):
    body = client.get("/stats").json()
    for key, slot in body["by_level"].items():
        assert slot["total"] >= 0 and slot["hit"] >= 0
        assert slot["rate"] is None or 0.0 <= slot["rate"] <= 1.0


def test_stats_reflects_written_data(client, monkeypatch, tmp_path):
    """/stats 读的必须是机器人落盘的那份数据，不能是空壳。"""
    db = tmp_path / "api.db"
    repo = repository.PredictionRepository(str(db))
    conn = repo._connect()
    conn.execute(
        "INSERT OR REPLACE INTO predictions "
        "(fixture_id, season, league, home, away, kickoff, model_version, source, "
        " level_key, result, home_prob, draw_prob, away_prob, best_score, created_at,"
        " inputs, actual_home, actual_away, settled_at) "
        "VALUES ('1',2026,'PL','A','B','2026-01-01T15:00:00','v1','s','high','主胜',"
        " 0.6,0.2,0.2,'1-0','2026-01-01T00:00:00','{}',2,0,'2026-01-01T20:00:00')"
    )
    conn.commit()
    body = client.get("/stats").json()
    assert body["settled"] == 1
    assert body["hit"] == 1


def test_commands_endpoint_lists_all(client):
    """/commands 暴露的指令数应与 dispatcher 一致。"""
    from commands import build_dispatcher

    body = client.get("/commands").json()
    assert len(body["commands"]) == len(build_dispatcher())
    names = {c["name"] for c in body["commands"]}
    assert {"start", "stats", "storage", "predict"} <= names


def test_commands_marks_admin_only(client):
    body = client.get("/commands").json()
    admin = {c["name"] for c in body["commands"] if c["admin_only"]}
    assert {"test", "status", "stats", "storage"} <= admin


def test_unknown_route_is_404(client):
    assert client.get("/nope").status_code == 404


# ============================================================================
# 看板端点（Stage 5）
# ============================================================================

def test_index_serves_dashboard(client):
    """/ 必须返回看板页面本身。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "html" in r.headers["content-type"]
    assert "<html" in r.text.lower()


def test_dashboard_has_no_external_dependencies(client):
    """页面上不得引用 CDN——离线/内网环境必须能渲染，否则就是加载死锁。"""
    html = client.get("/").text
    for bad in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr", "googleapis"):
        assert bad not in html, f"页面引用了外部资源 {bad}"


def test_model_health_endpoint(client):
    r = client.get("/health/model")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "empty", "insufficient")


def test_model_health_reports_insufficient_samples(client, tmp_path, monkeypatch):
    """样本不足时必须明确标记，不能让看板画出看似可信的曲线。"""
    import analytics

    db = tmp_path / "few.db"
    from repository import PredictionRepository

    PredictionRepository(str(db))._connect().close()
    monkeypatch.setattr(api, "_DB_PATH", str(db))
    body = client.get("/health/model").json()
    assert body["status"] == "empty"
    assert body["message"]


def test_audit_endpoint(client):
    body = client.get("/audit").json()
    assert body["status"] in ("ok", "empty")
    assert isinstance(body["items"], list)


def test_audit_limit_is_clamped(client):
    """超大 limit 必须被夹住，否则会拖慢响应、影响 Bot 性能。"""
    body = client.get("/audit?limit=99999").json()
    assert len(body["items"]) <= 200


def test_strength_endpoint(client):
    body = client.get("/strength").json()
    assert body["status"] in ("ok", "insufficient", "error")
    assert isinstance(body["teams"], list)


def test_strength_accepts_competition_filter(client):
    assert client.get("/strength?competition=PL").status_code == 200


def test_commands_endpoint_is_fast(client):
    """启动时已预热，响应必须在 200ms 预算内（实测未预热时约 800ms）。"""
    import time

    t0 = time.perf_counter()
    client.get("/commands")
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 200, f"/commands 耗时 {elapsed:.0f}ms，超出 200ms 预算"


def test_all_endpoints_within_latency_budget(client):
    """每个端点都要满足 200ms 预算。"""
    import time

    for path in ("/health", "/stats", "/health/model", "/audit", "/strength", "/commands"):
        t0 = time.perf_counter()
        r = client.get(path)
        elapsed = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200, path
        assert elapsed < 200, f"{path} 耗时 {elapsed:.0f}ms，超出 200ms 预算"
