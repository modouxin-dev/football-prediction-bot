import asyncio
import dataclasses
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

import main
from api_client import APIError
from config import load_settings
from service import PredictionService
from tests.sample_data import NOW, FakeAPI, default_fixtures

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self):
        self.replies, self.edits = [], []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeQuery:
    def __init__(self, data, edit_error=None):
        self.data, self.edit_error = data, edit_error
        self.answers, self.edits = [], []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        if self.edit_error:
            raise self.edit_error
        self.edits.append((text, parse_mode, reply_markup))


def make_app(api=None, settings=SETTINGS, warm=True):
    api = api or FakeAPI({2026: default_fixtures()})
    service = PredictionService(settings, api)
    if warm:  # 预先生成预测，模拟“已经推送过”
        run(service.build_predictions(now=NOW))
    bot = SimpleNamespace(send_message=AsyncMock())
    job_queue = SimpleNamespace(get_jobs_by_name=lambda name: [])
    app = SimpleNamespace(bot=bot, bot_data={"settings": settings, "service": service, "api": api}, job_queue=job_queue)
    return app, service, api


def command_update(user_id=555):
    msg = FakeMessage()
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=msg), msg


# ---- 推送 -------------------------------------------------------------------
def live_fixtures(*hours):
    """以真实当前时间为基准的赛程（run_push 内部使用真实时钟）。"""
    from datetime import datetime, timedelta, timezone

    from tests.sample_data import fixture

    now = datetime.now(timezone.utc)
    teams = [(1, "Alpha FC"), (2, "Beta & Sons"), (3, "Gamma"), (4, "Delta")]
    return [
        fixture(200 + i, teams[i % 4][0], teams[i % 4][1], teams[(i + 1) % 4][0], teams[(i + 1) % 4][1], now + timedelta(hours=h))
        for i, h in enumerate(hours)
    ]


def test_run_push_sends_html_messages_with_keyboard_to_chat_id():
    app, _, _ = make_app(FakeAPI({2026: live_fixtures(2, 5)}), warm=False)
    result = run(main.run_push(app))
    assert result.sent == 2 and result.note is None
    for call in app.bot.send_message.await_args_list:
        kwargs = call.kwargs
        assert kwargs["chat_id"] == 555 and str(kwargs["parse_mode"]) in ("HTML", "ParseMode.HTML")
        assert "胜平负概率" in kwargs["text"] and kwargs["reply_markup"].inline_keyboard


def test_run_push_widens_window_only_for_manual_tests():
    far = FakeAPI({2026: live_fixtures(24 * 6)})  # 6 天后：超出 36 小时窗口
    app, _, _ = make_app(far, warm=False)
    assert run(main.run_push(app)).sent == 0  # 定时推送不放宽
    result = run(main.run_push(app, widen=True))  # /test 会放宽，方便看到示例
    assert result.sent == 1 and "放宽" in result.note


def test_run_push_reports_when_nothing_is_upcoming():
    app, _, _ = make_app(FakeAPI({2026: []}), warm=False)
    result = run(main.run_push(app, widen=True))
    assert result.sent == 0 and result.note is None
    app.bot.send_message.assert_not_awaited()


def test_run_push_requires_chat_id():
    no_chat = dataclasses.replace(SETTINGS, chat_id=None)
    app, _, _ = make_app(settings=no_chat, warm=False)
    with pytest.raises(RuntimeError, match="CHAT_ID"):
        run(main.run_push(app))


def test_daily_push_failure_notifies_admins_instead_of_failing_silently():
    class Failing(FakeAPI):
        async def get_fixtures(self, *a, **k):
            raise APIError("HTTP 403 无权访问")

    app, _, _ = make_app(Failing({}), warm=False)
    run(main.daily_push(SimpleNamespace(application=app)))
    args = app.bot.send_message.await_args.kwargs
    assert args["chat_id"] == 555 and "每日推送失败" in args["text"] and "403" in args["text"]


# ---- 命令 -------------------------------------------------------------------
def test_test_command_is_admin_only_and_shows_the_users_id():
    app, _, _ = make_app(warm=False)
    update, msg = command_update(user_id=999)
    run(main.test_cmd(update, SimpleNamespace(application=app)))
    assert "仅管理员" in msg.replies[-1] and "999" in msg.replies[-1]
    app.bot.send_message.assert_not_awaited()


def test_test_command_reports_real_failure_instead_of_claiming_success():
    """回归：旧版不管数据源是否报错都会回复“测试推送完成”。"""

    class Failing(FakeAPI):
        async def get_fixtures(self, *a, **k):
            raise APIError("HTTP 403 无权访问（响应：You are not subscribed to this API.）")

    app, _, _ = make_app(Failing({}), warm=False)
    update, msg = command_update()
    run(main.test_cmd(update, SimpleNamespace(application=app)))
    assert msg.edits and "❌" in msg.edits[-1] and "not subscribed" in msg.edits[-1] and "✅" not in msg.edits[-1]


def test_test_command_with_no_matches_says_nothing_was_sent():
    app, _, _ = make_app(FakeAPI({2026: []}), warm=False)
    update, msg = command_update()
    run(main.test_cmd(update, SimpleNamespace(application=app)))
    assert "没有可推送的比赛" in msg.edits[-1] and "✅" not in msg.edits[-1]


def test_status_shows_account_plan_schedule_and_stale_season_warning():
    stale = dataclasses.replace(SETTINGS, season=SETTINGS.expected_season - 1)
    app, _, _ = make_app(settings=stale)
    update, msg = command_update()
    run(main.status_cmd(update, SimpleNamespace(application=app)))
    text = msg.replies[-1]
    assert "套餐：Pro" in text and "数据源连通：✅" in text and "今日请求：12 / 7500" in text
    assert "按日期应为" in text and "下次推送" in text and "RapidAPI" in text


def test_status_surfaces_data_source_errors():
    class NoAccess(FakeAPI):
        async def get_account_status(self):
            raise APIError("HTTP 403 无权访问")

    app, _, _ = make_app(NoAccess({2026: []}))
    update, msg = command_update()
    run(main.status_cmd(update, SimpleNamespace(application=app)))
    assert "数据源连通：❌ HTTP 403" in msg.replies[-1]


def test_is_admin():
    update, _ = command_update(555)
    assert main.is_admin(update, SETTINGS)
    other, _ = command_update(1)
    assert not main.is_admin(other, SETTINGS)
    assert not main.is_admin(SimpleNamespace(effective_user=None), SETTINGS)


# ---- 按钮 -------------------------------------------------------------------
def press(app, data, edit_error=None):
    query = FakeQuery(data, edit_error)
    run(main.on_button(SimpleNamespace(callback_query=query), SimpleNamespace(application=app)))
    return query


def active_tab(query):
    markup = query.edits[-1][2]
    return [b.text for row in markup.inline_keyboard for b in row if b.text.startswith("●")]


def test_buttons_switch_views_in_place_and_keep_the_keyboard():
    app, _, _ = make_app()
    for data, marker, tab in [
        ("deep:101", "深度分析", "● 🔍 深度分析"),
        ("odds:101", "赔率对比", "● 💰 赔率对比"),
        ("h2h:101", "历史交锋", "● 📊 历史交锋"),
        ("home:101", "胜平负概率", "● 📈 预测"),
    ]:
        q = press(app, data)
        assert q.answers == [(None, False)]  # 先应答，消除按钮转圈
        text, parse_mode, _ = q.edits[-1]
        assert marker in text and str(parse_mode) in ("HTML", "ParseMode.HTML") and active_tab(q) == [tab]


def test_refresh_refetches_odds_and_returns_to_prediction_tab():
    app, _, api = make_app()
    q = press(app, "refresh:101")
    assert api.odds_calls[-1] == (101, True)
    assert "胜平负概率" in q.edits[-1][0] and active_tab(q) == ["● 📈 预测"]


def test_button_on_expired_prediction_shows_alert_and_does_not_edit():
    app, _, _ = make_app(warm=False)
    q = press(app, "deep:101")
    assert q.edits == [] and q.answers[0][1] is True and "已过期" in q.answers[0][0]


def test_h2h_api_failure_is_shown_in_place_with_navigation_kept():
    app, _, _ = make_app(FakeAPI({2026: default_fixtures()}, h2h_error=APIError("HTTP 429 额度用尽")))
    q = press(app, "h2h:101")
    assert "获取数据失败" in q.edits[-1][0] and "429" in q.edits[-1][0]
    assert active_tab(q) == ["● 📊 历史交锋"]


def test_clicking_the_current_tab_again_is_ignored_but_other_errors_propagate():
    app, _, _ = make_app()
    press(app, "home:101", edit_error=BadRequest("Message is not modified: specified new message content..."))
    with pytest.raises(BadRequest):
        press(app, "home:101", edit_error=BadRequest("Message can't be edited"))


# ---- 应用装配 / 日志 ------------------------------------------------------------
def test_application_registers_handlers_and_schedules_daily_job_in_local_time():
    s = load_settings({**ENV, "PUSH_TIME": "07:30", "TIMEZONE": "Asia/Shanghai"})
    app = main.build_application(s)
    jobs = app.job_queue.get_jobs_by_name("daily_push")
    assert len(jobs) == 1

    async def check():
        await app.job_queue.start()
        try:
            return jobs[0].next_t.astimezone(s.timezone).strftime("%H:%M")
        finally:
            await app.job_queue.stop()

    assert run(check()) == "07:30"  # 按 TIMEZONE 计时，而不是写死的 08:00 UTC


def test_no_daily_job_without_chat_id():
    env = {k: v for k, v in ENV.items() if k not in ("CHAT_ID", "ADMIN_ID")}
    app = main.build_application(load_settings(env))
    assert app.job_queue.get_jobs_by_name("daily_push") == ()


def test_redacting_formatter_masks_secrets_in_messages_and_tracebacks():
    fmt = main.RedactingFormatter("%(message)s", ["SECRET-TOKEN"])
    try:
        raise RuntimeError("GET https://api.telegram.org/botSECRET-TOKEN/getUpdates failed")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "url botSECRET-TOKEN", None, sys.exc_info())
    out = fmt.format(record)
    assert "SECRET-TOKEN" not in out and "***" in out


def test_setup_logging_silences_httpx_which_would_print_the_bot_token():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        main.setup_logging(SETTINGS)
        assert logging.getLogger("httpx").level == logging.WARNING
        assert isinstance(root.handlers[0].formatter, main.RedactingFormatter)
    finally:
        root.handlers[:], root.level = saved[0], saved[1]
