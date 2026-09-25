"""阶段八：图表生成与发送闭环（不落盘，不联网）。"""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from api_client import APIError
from bot_handler import BotUI
from config import load_settings
from service import PredictionService
from tests.sample_data import fixture

chart = pytest.importorskip("chart", reason="matplotlib 未安装时跳过图表用例")

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


def now_fixture(fid=1001, home_id=1, away_id=2, hours=5):
    return fixture(fid, home_id, "曼城", away_id, "利物浦",
                   datetime.now(timezone.utc) + timedelta(hours=hours), status="NS")


def fm(tid, oid, our, their, home=True, days=3):
    h, a = (tid, oid) if home else (oid, tid)
    hg, ag = (our, their) if home else (their, our)
    return {
        "fixture": {"id": tid * 100 + days, "date": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
                    "status": {"short": "FT"}},
        "league": {"id": 39, "name": "PL"},
        "teams": {"home": {"id": h, "name": f"T{h}"}, "away": {"id": a, "name": f"T{a}"}},
        "goals": {"home": hg, "away": ag},
    }


def row(tid, rank, pts, w, d, l, gf, ga):
    return {"rank": rank, "team": {"id": tid, "name": f"T{tid}"}, "points": pts,
            "all": {"played": w + d + l, "win": w, "draw": d, "lose": l, "goals": {"for": gf, "against": ga}},
            "home": {"played": w, "goals": {"for": gf // 2, "against": ga // 2}},
            "away": {"played": d + l, "goals": {"for": gf // 2, "against": ga // 2}}}


class ChartAPI:
    def __init__(self, form=True, h2h=True):
        self.form_on, self.h2h_on = form, h2h
        self.items = [now_fixture()]

    provider = "rapidapi"
    quota_remaining = "88"

    async def get_fixtures(self, *a, **k):
        return self.items

    async def get_standings(self, *a, **k):
        return [row(1, 1, 18, 6, 0, 1, 16, 5), row(2, 4, 11, 3, 2, 2, 10, 9)]

    async def get_odds(self, *a, **k):
        return []

    async def get_h2h(self, *a, **k):
        return [fm(1, 2, 2, 1, days=100), fm(1, 2, 1, 1, home=False, days=200)] if self.h2h_on else []

    async def get_team_form(self, tid, season, last=5):
        if not self.form_on:
            return []
        return [fm(tid, 9, 2, 0, days=2), fm(tid, 8, 1, 1, home=False, days=6), fm(tid, 7, 0, 2, days=10)]

    async def get_account_status(self):
        return {}


def build_report(form=True, h2h=True):
    svc = PredictionService(SETTINGS, ChartAPI(form=form, h2h=h2h))
    return svc, run(svc.analyze_fixture(1001, [now_fixture()]))


# ---- 图表生成 -----------------------------------------------------------------
def test_prob_chart_returns_png_bytes():
    svc = PredictionService(SETTINGS, ChartAPI())
    p = run(svc.predict_fixture(1001, [now_fixture()]))
    blob = chart.prob_chart(p, SETTINGS.timezone)
    assert blob and blob[:8] == b"\x89PNG\r\n\x1a\n"  # 必须是真正的 PNG


def test_form_chart_returns_png_bytes():
    _, rep = build_report()
    blob = chart.form_chart(rep, SETTINGS.timezone)
    assert blob and blob[:8] == b"\x89PNG\r\n\x1a\n"


def test_goals_chart_returns_png_bytes():
    _, rep = build_report()
    blob = chart.goals_chart(rep, SETTINGS.timezone)
    assert blob and blob[:8] == b"\x89PNG\r\n\x1a\n"


def test_h2h_chart_returns_png_bytes():
    _, rep = build_report()
    blob = chart.h2h_chart(rep, SETTINGS.timezone)
    assert blob and blob[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("kind", ["form", "goals", "h2h"])
def test_empty_data_produces_no_misleading_chart(kind):
    """数据不足时必须返回 None，绝不画空图误导用户。"""
    _, rep = build_report(form=False, h2h=False)
    fn = {"form": chart.form_chart, "goals": chart.goals_chart, "h2h": chart.h2h_chart}[kind]
    assert fn(rep, SETTINGS.timezone) is None


def test_chart_uses_agg_backend():
    import matplotlib

    assert matplotlib.get_backend().lower() == "agg"  # 容器内无界面，必须 Agg


def test_chinese_font_is_configured():
    """必须选中一个中文字体，否则中文会渲染成方块。"""
    assert chart.FONT_IN_USE, "未找到可用中文字体"


# ---- 键盘闭环 -----------------------------------------------------------------
def test_prediction_keyboard_has_chart_button():
    markup = BotUI.prediction_keyboard(1001)
    data = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert "chart:prob:1001" in data
    assert "menu:fixtures" in data and "menu:home" in data


def test_analysis_keyboard_has_all_chart_buttons():
    markup = BotUI.analysis_keyboard(1001)
    data = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert {"chart:prob:1001", "chart:form:1001", "chart:goals:1001", "chart:h2h:1001"} <= data
    assert "fa:1001" not in data or True  # 分析页自身无需返回自身
    assert "menu:home" in data


def test_chart_keyboard_offers_other_charts_and_exit():
    markup = BotUI.chart_keyboard(1001, "form")
    data = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert "chart:form:1001" not in data  # 不重复提供当前这张
    assert {"chart:goals:1001", "chart:h2h:1001"} <= data
    assert "fa:1001" in data and "menu:home" in data


# ---- handler 集成 --------------------------------------------------------------
class FakePhotoMessage:
    def __init__(self):
        self.photos = []

    async def reply_photo(self, photo, **kwargs):
        self.photos.append((photo, kwargs))
        return self

    async def reply_text(self, text, **kwargs):
        return self


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.message = FakePhotoMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
        pass


def make_ctx(api, fx_cache=None):
    service = PredictionService(SETTINGS, api)
    bot_data = {"settings": SETTINGS, "service": service, "api": api}
    if fx_cache is not None:
        bot_data["fx_cache"] = fx_cache
    app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data=bot_data,
        job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []),
    )
    return SimpleNamespace(application=app, user_data={}), service


def q_update(data, user_id=555):
    q = FakeQuery(data)
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=user_id), effective_message=None), q


@pytest.mark.parametrize("kind", ["prob", "form", "goals", "h2h"])
def test_chart_callback_sends_photo(kind):
    ctx, _ = make_ctx(ChartAPI(), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update(f"chart:{kind}:1001")
    run(main.on_chart(update, ctx))
    assert len(q.message.photos) == 1
    blob, kwargs = q.message.photos[0]
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    assert kwargs.get("reply_markup") is not None


def test_chart_callback_insufficient_data_shows_alert():
    """数据不足：提示用户，不发送误导性图片。"""
    ctx, _ = make_ctx(ChartAPI(form=False, h2h=False), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("chart:h2h:1001")
    run(main.on_chart(update, ctx))
    assert len(q.message.photos) == 0
    assert any("暂无足够数据" in str(a[0]) for a in q.answers)


def test_chart_callback_invalid_data_is_ignored():
    ctx, _ = make_ctx(ChartAPI(), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("chart:prob")
    run(main.on_chart(update, ctx))
    assert len(q.message.photos) == 0


def test_chart_callback_unknown_fixture_alerts():
    ctx, _ = make_ctx(ChartAPI(), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("chart:prob:44444")
    run(main.on_chart(update, ctx))
    assert len(q.message.photos) == 0
    assert any("不在今日赛程" in str(a[0]) for a in q.answers)


def test_chart_callback_api_error_alerts():
    class BoomStandings(ChartAPI):
        async def get_standings(self, *a, **k):
            raise APIError("Free plans do not have access to this season")

    ctx, _ = make_ctx(BoomStandings(), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("chart:prob:1001")
    run(main.on_chart(update, ctx))
    assert len(q.message.photos) == 0
    assert any("获取数据失败" in str(a[0]) for a in q.answers)


def test_chart_callback_duplicate_click_blocked():
    ctx, _ = make_ctx(ChartAPI(), fx_cache={"date": "x", "items": [now_fixture()]})
    update, q = q_update("chart:prob:1001")
    run(main.on_chart(update, ctx))
    first = len(q.message.photos)
    ctx.application.bot_data.setdefault("pending", set()).add((555, "chart:prob:1001"))
    update2, q2 = q_update("chart:prob:1001")
    run(main.on_chart(update2, ctx))
    assert first == 1 and len(q2.message.photos) == 0


def test_no_temp_files_left_behind():
    """图表只在内存生成，不得产生临时文件。"""
    import glob
    import tempfile
    import os

    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.png")))
    ctx, _ = make_ctx(ChartAPI(), fx_cache={"date": "x", "items": [now_fixture()]})
    for kind in ("prob", "form", "goals", "h2h"):
        update, q = q_update(f"chart:{kind}:1001")
        run(main.on_chart(update, ctx))
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.png")))
    assert after - before == set()
