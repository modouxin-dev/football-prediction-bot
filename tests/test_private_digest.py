"""私聊点「生成今日预测」的输出归属回归测试。

用户实测现象：在机器人私聊里点「🎯 生成今日预测」，内容却发到了群里，
私聊里收不到。本组测试锁定「手动触发必须原地回复发起会话」，
而不是走定时推送的 chat_target 出口。
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main
from config import load_settings
from repository import PredictionRepository
from service import PredictionService
from support import begin_task
from templates import MENU_ITEMS
from tests.sample_data import FakeAPI

# 群号与私聊用户号必须不同：相同就测不出「发错会话」这类缺陷。
GROUP_CHAT = "-1001234567890"
PRIVATE_CHAT = 777

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": GROUP_CHAT, "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


class TodayAPI(FakeAPI):
    """返回「今天」的赛程，忽略赛季参数。"""

    def __init__(self, items):
        super().__init__({})
        self.items = items
        self.calls = 0

    async def get_fixtures(self, league_id, season, date_from, date_to):
        self.calls += 1
        return self.items


def _fixture(i, base):
    from tests.sample_data import fixture as mk
    return mk(1000 + i, 1 + i, f"主队{i + 1}", 100 + i, f"客队{i + 1}",
              base + timedelta(minutes=30 * i), status="NS")


def today_fixtures(count=3):
    now = datetime.now(timezone.utc)
    base = now + timedelta(hours=1)
    return [_fixture(i, base) for i in range(count)]


class FakeMessage:
    """记录每条回复落到哪个 chat。reply_text 只记本消息所属会话。"""

    def __init__(self, text, chat_id, user_id):
        self.text = text
        self.chat_id = chat_id
        self.from_user = SimpleNamespace(id=user_id)
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((self.chat_id, text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append((self.chat_id, text))


class FakeQuery:
    def __init__(self, data, chat_id, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat_id=chat_id)
        self.answers, self.edits = [], []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
        self.edits.append((self.message.chat_id, text, parse_mode, reply_markup))


def make_ctx(api):
    service = PredictionService(SETTINGS, api)
    service.repo = PredictionRepository(":memory:")
    service.sync.repo = service.repo
    service.local_first = False
    bot = SimpleNamespace(send_message=AsyncMock())
    app = SimpleNamespace(
        bot=bot,
        bot_data={"settings": SETTINGS, "service": service, "api": api,
                  "cache_manager": main.CacheManager()},
        job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []),
    )
    return SimpleNamespace(application=app, user_data={}, bot=bot), service


def digest_label():
    for key, label in MENU_ITEMS:
        if key == "digest":
            return label
    raise AssertionError("菜单项 digest 缺失")


def test_private_text_menu_answers_in_private_chat():
    """底部键盘「生成今日预测」：必须回复到发起的私聊，不能发到群。"""
    ctx, _ = make_ctx(TodayAPI(today_fixtures(3)))
    msg = FakeMessage(digest_label(), PRIVATE_CHAT, PRIVATE_CHAT)
    update = SimpleNamespace(callback_query=None,
                             effective_user=SimpleNamespace(id=PRIVATE_CHAT),
                             effective_message=msg)
    run(main.on_menu_text(update, ctx))

    assert msg.replies, "私聊点了按钮却没有任何回复"
    assert all(c == PRIVATE_CHAT for c, _, _ in msg.replies)
    # 首条是「正在生成」提示，汇总随后到达
    assert any("今日预测汇总" in text for _, text, _ in msg.replies)
    # 群里不应该收到任何东西：send_message 是定时推送的出口
    assert ctx.bot.send_message.await_count == 0


def test_private_callback_menu_edits_in_private_chat():
    """内联按钮「生成今日预测」：就地编辑所在私聊消息。"""
    ctx, _ = make_ctx(TodayAPI(today_fixtures(3)))
    q = FakeQuery("menu:digest", PRIVATE_CHAT, PRIVATE_CHAT)
    update = SimpleNamespace(callback_query=q,
                             effective_user=SimpleNamespace(id=PRIVATE_CHAT),
                             effective_message=None)
    run(main.on_menu(update, ctx))

    assert q.edits, "私聊点了内联按钮却没有任何输出"
    assert all(c == PRIVATE_CHAT for c, *_ in q.edits)
    assert any("今日预测汇总" in text for _, text, *_ in q.edits)
    assert ctx.bot.send_message.await_count == 0


def test_group_menu_still_answers_in_group():
    """群里点同一个按钮：回复群本身（不是私聊），避免修好私聊又弄坏群。"""
    ctx, _ = make_ctx(TodayAPI(today_fixtures(3)))
    msg = FakeMessage(digest_label(), int(GROUP_CHAT), 555)
    update = SimpleNamespace(callback_query=None,
                             effective_user=SimpleNamespace(id=555),
                             effective_message=msg)
    run(main.on_menu_text(update, ctx))

    assert msg.replies
    assert all(c == int(GROUP_CHAT) for c, _, _ in msg.replies)


# ---- 「点了没反应」：并发守卫与长任务的可见反馈 ----------------------------------

def test_private_text_digest_feedback_when_guard_hit():
    """守卫命中（上一次还没跑完）时，私聊必须给出可见反馈，不能静默吞掉。

    生成几十场要十几秒，用户必然会重复点；此前命中守卫直接 return，
    界面上毫无变化，用户只能以为机器人坏了（或以为内容发到别处了）。
    """
    ctx, _ = make_ctx(TodayAPI(today_fixtures(3)))
    run(begin_task(ctx.application.bot_data, PRIVATE_CHAT, "digest", PRIVATE_CHAT))
    msg = FakeMessage(digest_label(), PRIVATE_CHAT, PRIVATE_CHAT)
    update = SimpleNamespace(callback_query=None,
                             effective_user=SimpleNamespace(id=PRIVATE_CHAT),
                             effective_message=msg)
    run(main.on_menu_text(update, ctx))
    assert msg.replies, "守卫命中时私聊完全没有反馈，用户会以为机器人坏了"


def test_private_text_digest_acknowledges_before_long_task():
    """底部键盘入口：长任务开始前先给一条「正在生成」，不要十几秒无动静。"""
    ctx, _ = make_ctx(TodayAPI(today_fixtures(3)))
    msg = FakeMessage(digest_label(), PRIVATE_CHAT, PRIVATE_CHAT)
    update = SimpleNamespace(callback_query=None,
                             effective_user=SimpleNamespace(id=PRIVATE_CHAT),
                             effective_message=msg)
    run(main.on_menu_text(update, ctx))
    assert any("正在生成" in text for _, text, _ in msg.replies), \
        "十几秒的取数过程里界面毫无动静，用户只能反复点击"


def test_callback_digest_says_waiting_when_guard_hit():
    """内联按钮入口：命中守卫时要说明「上一次还在生成」，而不是只闪一个 toast。"""
    ctx, _ = make_ctx(TodayAPI(today_fixtures(3)))
    run(begin_task(ctx.application.bot_data, PRIVATE_CHAT, "digest", PRIVATE_CHAT))
    q = FakeQuery("menu:digest", PRIVATE_CHAT, PRIVATE_CHAT)
    update = SimpleNamespace(callback_query=q,
                             effective_user=SimpleNamespace(id=PRIVATE_CHAT),
                             effective_message=None)
    run(main.on_menu(update, ctx))
    joined = " ".join(a for a in q.answers if a)
    assert "还在生成" in joined or "稍候" in joined, \
        f"命中守卫时只给了无信息量的提示：{q.answers}"


def test_digest_guard_is_per_chat_not_per_user():
    """同一个人在群里点了还没跑完，私聊再点不应被吞掉。

    守卫 key 曾只按 user_id，群里一次十几秒的取数会把私聊的点击全部静默吸收。
    两个会话互不相干，不该互相阻塞。
    """
    user = 555  # 同一个人
    ctx, _ = make_ctx(TodayAPI(today_fixtures(3)))
    run(begin_task(ctx.application.bot_data, user, "digest", int(GROUP_CHAT)))
    msg = FakeMessage(digest_label(), user, user)  # 私聊 chat_id 等于用户 ID
    update = SimpleNamespace(callback_query=None,
                             effective_user=SimpleNamespace(id=user),
                             effective_message=msg)
    run(main.on_menu_text(update, ctx))
    assert any("今日预测汇总" in text for _, text, _ in msg.replies), \
        "私聊被群里进行中的任务阻塞了"
