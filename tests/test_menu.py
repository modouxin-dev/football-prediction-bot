"""主菜单与今日赛程的测试（不联网，全部使用仿真数据）。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, KeyboardButton

import main
from api_client import APIError
from bot_handler import BotUI, STATUS_TEXT
from config import load_settings
from service import PredictionService
from tests.sample_data import FakeAPI, fixture

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


class BoomAPI(FakeAPI):
    """让 get_fixtures 抛出指定的 APIError，用于验证错误必须如实上报。"""

    def __init__(self, message="Free plans do not have access to this season, try from 2022 to 2024"):
        super().__init__({})
        self.message = message
        self.calls = 0
        self.seasons_tried = []

    async def get_fixtures(self, league_id, season, date_from, date_to):
        self.calls += 1
        self.seasons_tried.append(season)
        raise APIError(self.message)


class TodayAPI(FakeAPI):
    """返回「今天」的赛程，忽略赛季参数。"""

    def __init__(self, items):
        super().__init__({})
        self.items = items
        self.calls = 0

    async def get_fixtures(self, league_id, season, date_from, date_to):
        self.calls += 1
        return self.items


def today_fixtures(count=3, hour=10):
    now = datetime.now(timezone.utc)
    items = []
    for i in range(count):
        items.append(
            fixture(
                1000 + i,
                1 + i,
                f"主队{i + 1}",
                100 + i,
                f"客队{i + 1}",
                now.replace(hour=hour, minute=0) + timedelta(minutes=30 * i),
                status="NS" if i % 2 == 0 else "FT",
            )
        )
    return items


# ---- 主菜单 -------------------------------------------------------------------
def test_menu_keyboard_has_all_buttons():
    from bot_handler import MENU_ITEMS

    markup = BotUI.menu_keyboard()
    flat = [b for row in markup.inline_keyboard for b in row]
    assert len(flat) == len(MENU_ITEMS)  # 不写死数量，跟随 MENU_ITEMS
    assert all(isinstance(b, InlineKeyboardButton) for b in flat)
    labels = [b.text for b in flat]
    assert "📅 今日赛程" in labels and "🏆 联赛排名" in labels
    assert {b.callback_data for b in flat} == {f"menu:{key}" for key, _ in MENU_ITEMS}


def test_reply_menu_keyboard_matches_menu_items():
    """底部键盘与 Inline 菜单必须共用同一份 MENU_ITEMS，保证两边完全一致。"""
    from bot_handler import MENU_ITEMS

    markup = BotUI.reply_menu_keyboard()
    flat = [b for row in markup.keyboard for b in row]
    assert len(flat) == len(MENU_ITEMS)  # 与 MENU_ITEMS 保持一致，不写死数量
    assert all(isinstance(b, KeyboardButton) for b in flat)
    assert [b.text for b in flat] == [label for _, label in MENU_ITEMS]
    assert [b.text for b in flat] == [b.text for row in main.ui.menu_keyboard().inline_keyboard for b in row]


def test_format_menu_shows_league_season_timezone():
    text = BotUI.format_menu(SETTINGS)
    assert "39" in text and "2026" in text and "Asia/Shanghai" in text


def test_format_help_and_coming():
    assert "使用帮助" in BotUI.format_help()
    assert "今日赛程" in BotUI.format_coming("fixtures")


# ---- 错误分类（不能把权限错误伪装成“今天没比赛”） ---------------------------------
def test_error_hint_free_plan():
    hint = BotUI.error_hint(APIError("Free plans do not have access to this season"))
    assert "套餐" in hint and "升级" in hint


@pytest.mark.parametrize(
    "message,keyword",
    [
        ("Rate limit reached", "超限"),
        ("HTTP 403 invalid key", "Key"),
        ("HTTP 401 unauthorized", "Key"),
        ("connect timeout", "网络"),
    ],
)
def test_error_hint_other_cases(message, keyword):
    assert keyword in BotUI.error_hint(APIError(message))


def test_get_today_fixtures_raises_permission_error_not_empty_list():
    """Free 套餐取不到赛季时必须抛错，绝不能返回空列表伪装成「今天没比赛」。"""
    svc = PredictionService(SETTINGS, BoomAPI())
    with pytest.raises(APIError) as exc:
        run(svc.get_today_fixtures())
    assert "season" in str(exc.value).lower()


def test_season_fallback_degrades_until_available():
    """2026/2025 赛季不可用时，应自动降级到 2024 并取到数据。"""
    class FallbackAPI(FakeAPI):
        def __init__(self):
            super().__init__({})
            self.tried = []

        async def get_fixtures(self, league_id, season, date_from, date_to):
            self.tried.append(season)
            if season >= 2025:
                raise APIError("Free plans do not have access to this season, try from 2022 to 2024")
            return today_fixtures(2)

    api = FallbackAPI()
    svc = PredictionService(SETTINGS, api)
    got = run(svc.get_today_fixtures())
    assert len(got) == 2
    assert api.tried == [2026, 2025, 2024]  # 逐级向下，一命中就停
    assert svc.season_in_use == 2024
    assert svc.last_note and "2024" in svc.last_note and "降级" in svc.last_note


def test_season_fallback_note_only_when_degraded():
    """配置赛季本身可用时不产生降级提示。"""
    svc = PredictionService(SETTINGS, TodayAPI(today_fixtures(2)))
    run(svc.get_today_fixtures())
    assert svc.season_in_use == 2026
    assert svc.last_note is None


def test_non_season_error_is_not_degraded():
    """Key 无效 / 限流这类错误不应触发降级探测（避免无意义消耗额度）。"""
    class KeyErrorAPI(FakeAPI):
        def __init__(self):
            super().__init__({})
            self.calls = 0

        async def get_fixtures(self, league_id, season, date_from, date_to):
            self.calls += 1
            raise APIError("HTTP 401 Invalid API key")

    api = KeyErrorAPI()
    svc = PredictionService(SETTINGS, api)
    with pytest.raises(APIError):
        run(svc.get_today_fixtures())
    assert api.calls == 1  # 只请求一次，不降级重试


def test_get_today_fixtures_returns_all_statuses():
    """今日赛程要包含已完场的比赛，不能只列未开赛的。"""
    items = today_fixtures(3)
    svc = PredictionService(SETTINGS, TodayAPI(items))
    got = run(svc.get_today_fixtures())
    assert len(got) == 3
    assert {(f["fixture"]["status"]["short"]) for f in got} == {"NS", "FT"}


def test_get_today_fixtures_falls_back_to_expected_season():
    """SEASON 变量过期（仍是 2025）时，应改用按日期推算的 2026 赛季再取一次。"""
    stale = load_settings({**ENV, "SEASON": "2025"})
    api = FakeAPI({2025: [], 2026: today_fixtures(2)})
    svc = PredictionService(stale, api)
    got = run(svc.get_today_fixtures())
    assert len(got) == 2
    assert api.fixture_calls == [2025, 2026]  # 先试配置的，再试推算的
    assert svc.season_in_use == 2026


# ---- 赛程排版与分页 -------------------------------------------------------------
def test_fixtures_page_groups_by_league():
    items = today_fixtures(3)
    text, markup, page, total = BotUI.format_fixtures_page(items, SETTINGS.timezone, 0, 5, "2026-09-25")
    assert page == 0 and total == 1
    assert text.count("🏆") == 1  # 同一联赛只打印一次标题
    assert "主队1" in text and "客队1" in text
    assert "未开始" in text and "已完场" in text  # 状态中文映射


def test_fixtures_page_pagination():
    items = today_fixtures(7)
    _, _, p0, total = BotUI.format_fixtures_page(items, SETTINGS.timezone, 0, 5, "d")
    assert total == 2 and p0 == 0
    text1, markup1, _, _ = BotUI.format_fixtures_page(items, SETTINGS.timezone, 1, 5, "d")
    assert "主队6" in text1 and "主队1" not in text1
    nav = [b for row in markup1.inline_keyboard for b in row if b.callback_data.startswith("fxp:")]
    assert {b.callback_data for b in nav} == {"fxp:0", "fxp:1"}


def test_fixtures_page_clamps_out_of_range_page():
    items = today_fixtures(3)
    _, _, p, _ = BotUI.format_fixtures_page(items, SETTINGS.timezone, 99, 5, "d")
    assert p == 0
    _, _, p2, _ = BotUI.format_fixtures_page(items, SETTINGS.timezone, -5, 5, "d")
    assert p2 == 0


def test_fixtures_page_empty_shows_reason_not_error():
    """空赛程必须是「高级空状态」：说明范围无比赛 + 给出下一步，而非一片空白。"""
    text, markup, _, _ = BotUI.format_fixtures_page([], SETTINGS.timezone, 0, 5, "2026-09-25")
    assert "NO FIXTURE IN THIS WINDOW" in text
    assert "暂无比赛" in text
    assert "稍后再试" in text  # 给出下一步指引，不能是死胡同
    assert any(b.callback_data == "menu:home" for row in markup.inline_keyboard for b in row)


def test_status_text_mapping_covers_common_codes():
    assert STATUS_TEXT["NS"] == "未开始"
    assert STATUS_TEXT["FT"] == "已完场"
    assert STATUS_TEXT["HT"] == "中场休息"


# ---- 防重复点击与用户隔离 ---------------------------------------------------------
def test_begin_task_blocks_duplicate_but_allows_other_users():
    bot_data = {}
    assert run(main.begin_task(bot_data, 1, "fixtures")) is True
    assert run(main.begin_task(bot_data, 1, "fixtures")) is False  # 同一用户并发被拦
    assert run(main.begin_task(bot_data, 2, "fixtures")) is True  # 不同用户互不干扰
    main.end_task(bot_data, 1, "fixtures")
    assert run(main.begin_task(bot_data, 1, "fixtures")) is True  # 结束后可再次执行


def test_page_cursor_is_stored_per_user():
    c1, c2 = SimpleNamespace(user_data={}), SimpleNamespace(user_data={})
    c1.user_data["fx_page"] = 3
    assert c1.user_data["fx_page"] == 3
    assert "fx_page" not in c2.user_data  # 不同用户数据不共享


# ---- handler 集成：菜单、赛程、错误提示 --------------------------------------------
class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies, self.edits = [], []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers, self.edits = [], []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
        self.edits.append((text, parse_mode, reply_markup))


def make_ctx(api, user_data=None):
    service = PredictionService(SETTINGS, api)
    app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={"settings": SETTINGS, "service": service, "api": api},
        job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []),
    )
    return SimpleNamespace(application=app, user_data=user_data or {}), service


def query_update(data):
    q = FakeQuery(data)
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=555), effective_message=None), q


def text_update(text):
    msg = FakeMessage(text)
    return SimpleNamespace(callback_query=None, effective_user=SimpleNamespace(id=555), effective_message=msg), msg


def test_menu_home_callback_renders_menu():
    ctx, _ = make_ctx(TodayAPI(today_fixtures(2)))
    update, q = query_update("menu:home")
    run(main.on_menu(update, ctx))
    assert q.edits and "Football Insight" in q.edits[0][0]


def test_menu_fixtures_callback_lists_fixtures():
    ctx, _ = make_ctx(TodayAPI(today_fixtures(2)))
    update, q = query_update("menu:fixtures")
    run(main.on_menu(update, ctx))
    assert q.edits and "今日赛程" in q.edits[0][0]


def test_menu_fixtures_shows_real_api_error():
    """赛季不可用时：逐级降级探测（每个赛季各请求一次），全部失败后抛出真实原因。"""
    api = BoomAPI()
    ctx, _ = make_ctx(api)
    update, q = query_update("menu:fixtures")
    run(main.on_menu(update, ctx))
    text = q.edits[0][0]
    assert "获取今日赛程失败" in text
    assert "套餐" in text  # 真实原因，不是“今天没有比赛”
    # 降级探测：候选赛季各请求一次，不重复刷同一个赛季
    assert api.calls == len({2026, 2025, 2024, 2023})
    assert sorted(api.seasons_tried) == [2023, 2024, 2025, 2026]


def test_fixtures_cached_on_second_click():
    api = TodayAPI(today_fixtures(2))
    ctx, _ = make_ctx(api)
    for _ in range(3):
        update, _ = query_update("menu:fixtures")
        run(main.on_menu(update, ctx))
    assert api.calls == 1  # 重复点击不会重复请求


def test_refresh_clears_cache():
    api = TodayAPI(today_fixtures(2))
    ctx, _ = make_ctx(api)
    for data in ("menu:fixtures", "menu:refresh"):
        update, _ = query_update(data)
        run(main.on_menu(update, ctx))
    assert api.calls == 2


def test_pagination_callback():
    ctx, _ = make_ctx(TodayAPI(today_fixtures(7)))
    update, _ = query_update("menu:fixtures")
    run(main.on_menu(update, ctx))
    update, q = query_update("fxp:1")
    run(main.on_fixtures_page(update, ctx))
    assert q.edits and "主队6" in q.edits[0][0]
    assert ctx.user_data["fx_page"] == 1


def test_reply_keyboard_text_triggers_fixtures():
    api = TodayAPI(today_fixtures(2))
    ctx, _ = make_ctx(api)
    update, msg = text_update("📅 今日赛程")
    run(main.on_menu_text(update, ctx))
    assert msg.replies and "今日赛程" in msg.replies[0][0]


def test_reply_keyboard_unknown_text_is_ignored():
    ctx, _ = make_ctx(TodayAPI(today_fixtures(1)))
    update, msg = text_update("随便说点什么")
    run(main.on_menu_text(update, ctx))
    assert msg.replies == []


def test_unexpected_exception_does_not_crash():
    class BrokenAPI(FakeAPI):
        async def get_fixtures(self, *a, **k):
            raise RuntimeError("boom")

    ctx, _ = make_ctx(BrokenAPI({}))
    update, q = query_update("menu:fixtures")
    run(main.on_menu(update, ctx))
    assert "获取今日赛程失败" in q.edits[0][0]
