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


def make_ctx(user_id=555, public=None, admin=None):
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
