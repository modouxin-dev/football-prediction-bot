

def test_backup_source_cools_down_globally_after_429():
    """备用源 429 后必须整源冷却，而不是每个请求各睡各的。

    免费层 10 次/分钟，而一轮同步要对 10 个联赛各发一次请求。若只在单次
    请求内退避，后面 9 个会排队重复同样的退避 —— 实测一轮空转上百秒，
    而这段时间机器人还没向 Telegram 注册，表现为「不理人」。
    """
    import asyncio
    import football_data as fd

    calls = {"n": 0}

    class _Resp:
        status_code = 429

    class _Client:
        async def get(self, *a, **k):
            calls["n"] += 1
            return _Resp()

    api = fd.FootballDataAPI.__new__(fd.FootballDataAPI)
    api._client = _Client()
    api._owns_client = False
    api._base_url = "http://x"
    api._headers = {}
    api._timeout = 1
    api._rate_limited_until = 0.0

    async def first():
        try:
            await fd.FootballDataAPI._request(api, "/x", {})
        except fd.FootballDataError:
            pass

    asyncio.run(first())
    first_calls = calls["n"]
    assert first_calls >= 1
    # 冷却期内再发请求：必须立刻失败且**不再发出真实请求**
    async def second():
        try:
            await fd.FootballDataAPI._request(api, "/x", {})
        except fd.FootballDataError:
            pass

    asyncio.run(second())
    assert calls["n"] == first_calls, "冷却期内不得再发真实请求"


def test_startup_sync_is_bounded():
    """启动同步必须有超时上限——它阻塞 Telegram 注册。"""
    import ast
    import pathlib

    src = pathlib.Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    has_waitfor = any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "wait_for"
        for n in ast.walk(tree))
    assert has_waitfor, "启动同步必须用 asyncio.wait_for 设上限"
    assert "STARTUP_SYNC_TIMEOUT" in src
