"""未注册命令的兜底回复（不联网）。

背景：PTB 对没有任何 CommandHandler 匹配的命令默认不回应。用户发一个打错的
命令（或早已下线的旧命令），界面上只有自己那条孤零零的消息，机器人毫无反应
——从用户视角与「机器人掉线了」无法区分。

本组测试锁住四件事：
1. 兜底能识别并回复，且回复里带被打错的那个命令名
2. /cmd@BotName 群里写法能正确剥离出 cmd
3. 管理员看得到管理指令，普通用户看不到（不暴露 /backfill 这类）
4. 已注册命令不会被兜底误拦（回归）
"""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from config import load_settings
from templates import PANEL_COLLAPSED, PANEL_HINTS

ENV = {
    "TELEGRAM_TOKEN": "123456:TEST-TOKEN",
    "RAPID_API_KEY": "k",
    "CHAT_ID": "555",
    "SEASON": "2026",
    "ADMIN_ID": "555",
}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self


def make_update(text, user_id=555):
    msg = FakeMessage(text)
    return SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_message=msg,
    ), msg


def make_ctx(user_id=555, public=None, admin=None, known=None):
    app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={
            "settings": SETTINGS,
            "fx_cache": None,
            "public_commands": public if public is not None else [
                ("fixtures", "今日赛程"), ("predict", "比赛预测"),
            ],
            "admin_commands": admin if admin is not None else [
                ("backfill", "回填历史"), ("stats", "命中率统计"),
            ],
            # 已注册指令白名单：兜底靠它判断「group 0 是否已经处理过」
            "known_commands": known if known is not None else {
                "fixtures", "predict", "start", "help", "test", "backfill", "stats",
            },
        },
        job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []),
    )
    return SimpleNamespace(application=app, user_data={})


# ---- 兜底确实会回复 -----------------------------------------------------------

def test_unknown_command_gets_a_reply():
    """核心诉求：不能静默忽略。"""
    update, msg = make_update("/nosuchcmd")
    run(main.on_unknown_command(update, make_ctx()))
    assert msg.replies, "未知命令必须有回复，静默忽略会让用户以为机器人掉线"


def test_reply_echoes_the_offending_command():
    """回复要点名是哪个命令没找到，否则用户不知道自己打错了什么。"""
    update, msg = make_update("/nosuchcmd")
    run(main.on_unknown_command(update, make_ctx()))
    body = msg.replies[0][0]
    assert "nosuchcmd" in body


def test_reply_lists_available_commands():
    update, msg = make_update("/nosuchcmd")
    run(main.on_unknown_command(update, make_ctx()))
    body = msg.replies[0][0]
    assert "/fixtures" in body
    assert "/predict" in body


# ---- 群里 /cmd@BotName 写法 ---------------------------------------------------

def test_bot_suffix_is_stripped():
    """Telegram 在群里会把命令补成 /cmd@BotName，@ 后面不参与匹配。"""
    update, msg = make_update("/nosuchcmd@MyTestBot")
    run(main.on_unknown_command(update, make_ctx()))
    body = msg.replies[0][0]
    assert "nosuchcmd" in body
    assert "MyTestBot" not in body


def test_command_with_trailing_args():
    """/nosuchcmd arg1 arg2 只取第一段作为命令名。"""
    update, msg = make_update("/nosuchcmd arg1 arg2")
    run(main.on_unknown_command(update, make_ctx()))
    assert "nosuchcmd" in msg.replies[0][0]
    assert "arg1" not in msg.replies[0][0]


# ---- 权限：管理指令不能泄露给普通用户 -----------------------------------------

def test_normal_user_does_not_see_admin_commands():
    """普通用户不该看到 /backfill 这类管理指令。"""
    update, msg = make_update("/nosuchcmd", user_id=999)
    run(main.on_unknown_command(update, make_ctx()))
    body = msg.replies[0][0]
    assert "/backfill" not in body
    assert "/stats" not in body


def test_admin_sees_admin_commands():
    """管理员自己打错字时应该看到完整列表，否则会以为机器人坏了。"""
    update, msg = make_update("/nosuchcmd", user_id=555)
    run(main.on_unknown_command(update, make_ctx(user_id=555)))
    body = msg.replies[0][0]
    assert "/backfill" in body
    assert "/stats" in body


# ---- 边界：空输入不炸 ---------------------------------------------------------

@pytest.mark.parametrize("text", ["/", "", "   "])
def test_degenerate_input_is_ignored(text):
    """单独一个斜杠或空串不是命令，静默跳过即可，不必打扰用户。"""
    update, msg = make_update(text)
    run(main.on_unknown_command(update, make_ctx()))
    assert not msg.replies


def test_missing_bot_data_does_not_crash():
    """bot_data 里没有命令清单时（如单测直接调）不能抛异常。"""
    ctx = make_ctx()
    ctx.application.bot_data.pop("public_commands")
    ctx.application.bot_data.pop("admin_commands")
    update, msg = make_update("/nosuchcmd")
    run(main.on_unknown_command(update, ctx))
    assert msg.replies


# ---- 注册位置：必须在 group 1，否则会抢先拦掉正常命令 -------------------------

def test_fallback_registered_with_group_one():
    """兜底必须注册在 group=1。

    放在 group 0 的话，它与所有 CommandHandler 同组竞争；虽然按注册顺序
    排在后面不会误拦，但一旦有人在中间插入 handler 就会被顶掉或误拦。
    group=1 保证 group 0 全部跑完没匹配才轮到它。
    """
    src = (main.__file__ and open(main.__file__, encoding="utf-8").read()) or ""
    hits = re.findall(r"on_unknown_command.*?group\s*=\s*(\d+)", src, re.S)
    assert hits, "未找到 on_unknown_command 的注册语句"
    assert all(h == "1" for h in hits), f"兜底必须注册在 group=1，实际为 {hits}"


def test_register_commands_exposes_command_lists():
    """兜底依赖 bot_data 里的命令清单，装配时必须写入。"""
    src = open(main.__file__, encoding="utf-8").read()
    assert "public_commands" in src
    assert "admin_commands" in src


# ---- 回归：已注册命令不能被兜底误认为未知 -------------------------------------

def test_registered_commands_are_known():
    """已注册的命令必须能在 dispatcher 里查到，不会落到兜底。

    /test 是管理员指令（立即推送一次预测），它存在且 admin_only；
    这里同时钉住它没有被误删——曾有人以为它不存在。
    """
    from commands import build_dispatcher
    d = build_dispatcher()
    for name in ("fixtures", "predict", "test", "help"):
        assert name in d, f"{name} 应该在已注册命令里"


def test_admin_only_flag_preserved():
    """/test 是管理员指令，普通用户触发应被 deny 而不是当作未知命令。"""
    from commands import build_dispatcher
    d = build_dispatcher()
    assert d.get("test").admin_only is True
    assert d.get("fixtures").admin_only is False


# ---- 面板收起文案：不再用会误导的 ⌨️ ------------------------------------------

def test_collapsed_hint_has_no_keyboard_icon():
    """收起态提示不能以 ⌨️ 开头：面板里并没有一个 ⌨️ 按键。

    用户据此去找 ⌨️ 按键找不到，以为是功能缺失。
    """
    assert "⌨️" not in PANEL_HINTS[PANEL_COLLAPSED]


def test_collapsed_hint_says_bottom_buttons():
    """必须说清移出的是「我们的底部按钮」，而不是用户的系统输入键盘。

    收起用的是 ReplyKeyboardRemove，系统字母键盘反而会回来；说
    「键盘已移出屏幕」与用户看到的画面相反。
    """
    hint = PANEL_HINTS[PANEL_COLLAPSED]
    assert "键盘已移出屏幕" not in hint
    assert "底部按钮" in hint


def test_collapsed_hint_keeps_reopen_paths():
    """唤回路径必须还在：按钮与 /panel 两个入口都保留。"""
    hint = PANEL_HINTS[PANEL_COLLAPSED]
    assert "/panel" in hint
    assert "打开面板" in hint


# ---- 回归（真 PTB 派发）：已注册命令绝不能被兜底追加「未识别」 ---------------
#
# 这一组是整个文件里最关键的一层，也正是上一版漏掉的那一层。
#
# 上一版只测了「dispatcher 里查得到 start/test」这种注册表层面的事实，
# 于是测试全绿、线上却炸：/start、/test 执行成功后，消息下面还会再挂一条
# 「❓ 未识别的命令」。用户看到指令明明生效了却报未识别，比静默忽略更迷惑。
#
# 根因：PTB 的 Application.process_update 遍历**所有** group，每个 group
# 内最多跑一个匹配的 handler，跨 group 继续往下走，只有抛
# ApplicationHandlerStop 才中断。所以「注册在 group=1」根本不代表
# 「group 0 处理过就不轮到我了」，必须显式查白名单。
#
# 下面用真实 Application + 真实 handler + 真实 process_update 跑通这条链路。
# 说明一处诚实的局限：本沙盒里 ExtBot 无法真正 initialize()，group 0 的
# CommandHandler 在 check_update 阶段就会因取不到 bot.username 报错，
# 因此这里无法断言「group 0 执行了」。但能被断言、也正是线上出事的那一环是：
# **group 0 存在 /start 的 CommandHandler，group 1 的兜底照样被触发**。
# 白名单加上后它才安静下来。

def _real_dispatch(monkeypatch, known):
    """真实 PTB 派发：group 0 放 /start，group 1 放兜底，返回收到的回复文本。"""
    from telegram import Chat, Message, MessageEntity, Update, User
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    replies: list[str] = []

    async def _recorder(message, text, keyboard=None, **kw):
        replies.append(text)

    monkeypatch.setattr(main, "reply_html", _recorder)

    app = ApplicationBuilder().token("123456:TEST-TOKEN").build()
    app.bot_data.update({
        "settings": SETTINGS,
        "public_commands": [("fixtures", "今日赛程"), ("predict", "比赛预测")],
        "admin_commands": [("test", "立即推送"), ("backfill", "回填历史")],
        "known_commands": known,
    })

    async def _start(update, context):
        pass

    app.add_handler(CommandHandler("start", _start, block=True))
    app.add_handler(
        MessageHandler(filters.COMMAND, main.on_unknown_command, block=True), group=1
    )
    app.add_error_handler(lambda u, c: None)
    app._initialized = True  # 跳过会联网的 initialize()

    def send(text):
        n = len(text.split()[0])
        msg = Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=User(id=555, first_name="u", is_bot=False), text=text,
            entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=n)],
        )
        msg.set_bot(app.bot)
        replies.clear()
        asyncio.run(app.process_update(Update(update_id=1, message=msg)))
        return list(replies)

    return send


def test_group_one_fallback_fires_even_when_command_is_registered(monkeypatch):
    """钉住根因：group 0 有 /start，group 1 的兜底仍会被调用。

    这是线上事故的成因——注册在 group=1 并不等于「前面处理过就不轮到我」。
    """
    send = _real_dispatch(monkeypatch, set())  # 白名单里没有 start
    out = send("/start")
    assert any("未识别" in t for t in out), (
        "若这条失败，说明兜底没被触发——要么 PTB 改了跨 group 语义，"
        "要么 handler 注册方式变了，两种情况都需要重新评估白名单是否仍必要"
    )


def test_registered_command_is_not_doubled_by_fallback(monkeypatch):
    """线上事故回归：/start 被执行后，不能再追加一条「未识别的命令」。"""
    send = _real_dispatch(monkeypatch, {"start", "fixtures", "predict", "test"})
    out = send("/start")
    assert not [t for t in out if "未识别" in t], f"已注册命令被兜底误报：{out}"


def test_unknown_command_still_answers_under_real_dispatch(monkeypatch):
    """同一条派发链路下，真正没注册的命令仍必须回应。"""
    send = _real_dispatch(monkeypatch, {"start", "fixtures", "predict", "test"})
    out = send("/nosuchcmd")
    assert any("未识别" in t for t in out), f"未知命令必须有回应，实际：{out}"


def test_whitelist_is_case_and_suffix_insensitive(monkeypatch):
    """/START 与 /start@Bot 都算已注册，不能因大小写或群聊后缀误报。"""
    for text in ("/START", "/start@MyTestBot"):
        send = _real_dispatch(monkeypatch, {"start"})
        out = send(text)
        assert not [t for t in out if "未识别" in t], f"{text} 被误报：{out}"


def test_register_commands_writes_known_commands():
    """白名单必须在装配阶段写入 bot_data，否则兜底会退化成逢命令就报。"""
    src = open(main.__file__, encoding="utf-8").read()
    assert "known_commands" in src


def test_register_commands_populates_whitelist():
    """装配层回归：白名单必须由 register_commands 真正填满。

    上面几条派发测试是直接往 bot_data 里塞 known_commands 的，绕过了装配函数。
    如果只留那几条，有人把装配那一行改成空集合，测试照样全绿而线上照炸。
    所以这里必须单独钉住装配结果。
    """
    from telegram.ext import ApplicationBuilder

    app = ApplicationBuilder().token("123456:TEST-TOKEN").build()
    n = main.register_commands(app)
    known = app.bot_data.get("known_commands")
    assert n > 0, "应当注册到指令"
    assert known, "register_commands 必须写入 known_commands"
    assert len(known) == n, f"白名单数量({len(known)})应与注册数({n})一致"
    # 线上出过事的那两条必须都在
    assert "start" in known
    assert "test" in known
