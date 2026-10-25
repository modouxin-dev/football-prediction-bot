"""路由注册正确性。

回归背景（实测，非推测）：新增 /teammiss 时把 `@app.get("/live")` 装饰器
留在了新函数上方，导致 teammiss 同时注册了 /live 与 /teammiss 两个路径，
而真正的 live 函数失去路由。

后果：生产 `GET /live` 返回队名列表（含 names 字段、无 items），
前端读 items 为空 → 显示「此刻没有正在进行的比赛」，
而真实原因被完全掩盖——排查会一路走偏到套餐、数据源上。

这类错误不会让进程报错、不会让测试变红（因为测试直接调函数而非走路由），
只能靠**断言路由表本身**来拦截。
"""
from __future__ import annotations

import pytest


def _route_map():
    """{路径: 处理函数名}。"""
    from api import app
    out = {}
    for r in app.routes:
        path = getattr(r, "path", None)
        fn = getattr(r, "endpoint", None)
        if path and fn is not None:
            out[path] = getattr(fn, "__name__", str(fn))
    return out


def test_live_path_binds_to_live_handler():
    """/live 必须绑定到 live 函数。"""
    assert _route_map().get("/live") == "live"


def test_teammiss_path_binds_to_teammiss_handler():
    """/teammiss 必须绑定到 teammiss 函数。"""
    assert _route_map().get("/teammiss") == "teammiss"


def test_no_path_binds_to_two_endpoints():
    """同一路径不得被两个处理函数争用。

    装饰器堆叠（一个函数挂两个 @app.get）会让两条路径都指向同一函数，
    其中一条的正确实现被静默顶替。
    """
    from api import app
    paths = [getattr(r, "path", None) for r in app.routes
             if getattr(r, "path", "").startswith("/")
             and getattr(r, "endpoint", None) is not None]
    # 允许同一路径出现多次（如不同方法），这里只校验本次出问题的两个端点
    m = _route_map()
    assert m.get("/live") != m.get("/teammiss"), (
        f"/live 与 /teammiss 指向同一处理函数：{m.get('/live')}")


def test_live_response_has_items_not_names():
    """/live 的响应体必须含 items，绝不能是队名列表。

    直接用 TestClient 走真实路由（而不是调函数），
    这样才能覆盖路由层——上次的问题就只在路由层。
    """
    from fastapi.testclient import TestClient

    import api as apimod

    class C:
        def __init__(self):
            self.primary = self
            self.last_live_error = None

        async def get_live_fixtures(self):
            return []

        async def get_lineups(self, f):
            return []

        async def get_injuries(self, f):
            return []

        async def get_odds(self, f):
            return None

    apimod._LIVE_CLIENT = C()
    try:
        with TestClient(apimod.app) as client:
            data = client.get("/live?detail=0").json()
    finally:
        apimod._LIVE_CLIENT = None

    assert "items" in data, f"/live 返回了非预期结构：{list(data)[:6]}"
    assert "names" not in data, "/live 不应返回队名列表（那是 /teammiss 的结构）"
    assert data.get("status") in {"empty", "unsupported", "not_predicted", "ok", "error"}
