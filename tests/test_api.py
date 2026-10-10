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
    """把 API 指向临时库，避免污染真实数据。

    用 ``with`` 而非 ``return TestClient(app)``：后者不会触发 lifespan，
    测到的是冷启动耗时（/commands 约 800ms，超 200ms 预算 5.7 倍），
    导致延迟断言在共享 runner 上随机变红。线上 uvicorn 会执行 lifespan，
    测试必须与之对齐。
    """
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "api.db"))
    with TestClient(api.app) as c:
        yield c


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


def _seed_mixed_competitions(db_path: str) -> None:
    """写入英超(PL) 与英冠(ELC)两类赛果，用于验证默认按当前联赛过滤。"""
    from datetime import date, timedelta

    repo = repository.PredictionRepository(db_path)
    conn = repo._connect()
    pl = [(1, "Arsenal FC"), (2, "Manchester City FC"),
          (3, "Liverpool FC"), (4, "Chelsea FC")]
    elc = [(101, "Hull City AFC"), (102, "Ipswich Town FC")]

    rows = []
    # 英超：4 队双循环重复多轮，凑够强度榜门槛（MIN_MATCHES_FOR_STRENGTH=60）
    while len(rows) < 64:
        for h in pl:
            for a in pl:
                if h[0] != a[0]:
                    rows.append(("PL", h, a))
    # 英冠：少量比赛，模拟早期同步混入的脏数据
    rows.extend([("ELC", elc[0], elc[1])] * 5)

    base = date(2026, 1, 1)
    for k, (comp, h, a) in enumerate(rows):
        d = (base + timedelta(days=k)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO matches (competition_code, season, utc_date,"
            " status, home_team_id, home_team_name, away_team_id, away_team_name,"
            " home_score, away_score, source, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (comp, 2026, d, "FINISHED", str(h[0]), h[1], str(a[0]), a[1],
             1, 0, "test", d))
    conn.commit()
    conn.close()


def test_strength_defaults_to_configured_league(client, monkeypatch):
    """默认必须按当前联赛过滤——否则英冠球队会混进英超强度榜。"""
    monkeypatch.setenv("LEAGUE_ID", "39")
    _seed_mixed_competitions(api._DB_PATH)
    body = client.get("/strength").json()
    assert body["status"] == "ok", body.get("message")
    teams = {t["name"] for t in body["teams"]}
    assert "Arsenal FC" in teams
    assert "Hull City AFC" not in teams
    assert "Ipswich Town FC" not in teams


def test_strength_empty_competition_means_no_filter(client, monkeypatch):
    """显式传空才表示「不过滤」，此时英冠球队应当出现。"""
    monkeypatch.setenv("LEAGUE_ID", "39")
    _seed_mixed_competitions(api._DB_PATH)
    body = client.get("/strength?competition=").json()
    teams = {t["name"] for t in body["teams"]}
    assert "Hull City AFC" in teams


def test_default_competition_follows_league_id(monkeypatch):
    monkeypatch.setenv("LEAGUE_ID", "39")
    assert api._default_competition() == "PL"
    monkeypatch.setenv("LEAGUE_ID", "40")
    assert api._default_competition() == "ELC"


def test_commands_endpoint_is_fast(client):
    """启动时已预热，响应必须在 200ms 预算内（实测未预热时约 800ms）。"""
    import time

    t0 = time.perf_counter()
    client.get("/commands")
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 200, f"/commands 耗时 {elapsed:.0f}ms，超出 200ms 预算"


def test_all_endpoints_within_latency_budget(client):
    """每个端点都要满足 200ms 预算。

    夹具必须用 ``with TestClient(...)`` 触发 lifespan：api.py 在启动时预热了
    重量级导入（commands → bot_handler → chart → matplotlib），而
    ``TestClient(app)`` 作为普通对象构造时**不会**执行 lifespan，测到的永远是
    冷启动耗时——/commands 实测 1151ms，超预算 5.7 倍，于是这条断言在共享
    runner 上随机变红，此前一直被误判为"环境抖动"。

    预热生效后全体端点实测最大 1.4ms，距 200ms 有 139 倍裕度，严格预算因此
    得以保留：不必放宽到 500ms，也不必标记 flaky。
    """
    import time

    for path in ("/health", "/stats", "/health/model", "/audit", "/strength", "/commands"):
        t0 = time.perf_counter()
        r = client.get(path)
        elapsed = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200, path
        assert elapsed < 200, f"{path} 耗时 {elapsed:.0f}ms，超出 200ms 预算"


def test_lifespan_warms_up_commands_cache():
    """预热必须真的执行：进入 lifespan 后指令清单已被缓存。

    这条锁住 api.py 的 lifespan 行为。若有人删掉预热，首请求会把约 800ms 的
    导入开销转嫁给用户——线上表现为第一个用户撞上明显卡顿，而不是 CI 变红。
    """
    import api as api_module

    api_module._COMMAND_CACHE = None       # 复位，确保观测到的是本次预热的结果
    with TestClient(api_module.app):
        pass                                # 进出上下文即完成 startup/shutdown
    assert api_module._COMMAND_CACHE, "lifespan 未预热指令清单，首请求会承担导入开销"

