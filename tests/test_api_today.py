"""api.py 默认日期必须按项目时区（Asia/Shanghai）取，不能用 UTC。

反例：上海 10-05 07:00，UTC 仍是 10-04 23:00。用 UTC 取「今天」会返回
昨天赛程，看板与 Telegram 推送差一整天。
"""
import api


def test_today_uses_project_timezone(monkeypatch):
    """跨天场景下必须取上海日期（10-05），而非 UTC 日期（10-04）。"""
    from datetime import datetime, timezone as _tz

    monkeypatch.setattr(api, "_TZ_NAME", "Asia/Shanghai")
    real = datetime

    class FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            # 冻结在 UTC 23:30；now(tz) 应返回该时刻在 tz 下的本地时间
            cur = datetime(2026, 10, 4, 23, 30, tzinfo=_tz.utc)
            return cur.astimezone(tz) if tz else cur

    monkeypatch.setattr(api, "datetime", FrozenDT)
    assert api._today() == "2026-10-05"


def test_today_falls_back_to_utc_on_bad_tz(monkeypatch):
    """时区名无效时退化为 UTC 且不抛异常，看板不应因配置写错而崩。"""
    monkeypatch.setattr(api, "_TZ_NAME", "Not/AZone")
    assert len(api._today()) == 10


def test_fixtures_default_date_uses_project_tz(monkeypatch):
    """/fixtures 不传 date 时，返回的 date 必须是上海日期而非 UTC 日期。

    只测 _today() 不够 —— 调用点可能压根没用它。这里从 HTTP 端点验证。
    """
    from datetime import datetime, timezone as _tz
    from fastapi.testclient import TestClient
    import api as _api

    class FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            cur = datetime(2026, 10, 4, 23, 30, tzinfo=_tz.utc)
            return cur.astimezone(tz) if tz else cur

    monkeypatch.setattr(_api, "_TZ_NAME", "Asia/Shanghai")
    monkeypatch.setattr(_api, "datetime", FrozenDT)
    monkeypatch.setattr(_api, "_repo", lambda: type("R", (), {
        "matches_with_predictions": staticmethod(lambda day, limit=300: []),
    })())

    body = TestClient(_api.app).get("/fixtures").json()
    assert body["date"] == "2026-10-05", f"端点默认日期应为上海 10-05，实得 {body['date']}"
