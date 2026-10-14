"""赛前二次推送 与 /test 收件人路由 / Pre-match re-push & /test routing.

两件真实事故，各自对应一组断言：

1. **/test 写死发群**：管理员在私聊里敲 /test 验证，内容全飞进 CHAT_ID，
   本人一条都看不到 —— 看起来就像"机器人没理我"。
   → 手动触发必须发到发起会话；/test group 才发群。

2. **赛前刷新只落库、不推送**：08:00 推送的概率里没有当天伤停与首发；
   赛前刷新把新概率写进库，用户看到的仍是早上那条，信号接了等于没接。
   → 进入窗口且拿到阵容后推一次，靠 prematch_push 表去重（每场一次）。

    为什么"拿到阵容才推"而不是"进窗口就推"：官方明确说明首发阵容要开赛前
    20–40 分钟才公布，T-60 拿到的往往只有更新过的伤停与赔率。
    在窗口内优先等阵容；始终拿不到（部分赛事官方不提供）则临近开赛兜底推一次。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import scheduler
from config import load_settings
from repository import PredictionRepository
from scheduler import (PREMATCH_PUSH_NOTE, NotificationManager,
                       prematch_push, run_push)

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "-100999", "SEASON": "2026", "ADMIN_ID": "555"}


def run(coro):
    return asyncio.run(coro)


class FakeBot:
    """记录每条消息的收件人 —— 本批测试断言的就是 chat_id。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=len(self.sent))


class FakeApp:
    def __init__(self, settings, service=None, repo=None) -> None:
        self.bot = FakeBot()
        self.bot_data = {"settings": settings, "service": service}
        self.repo = repo


def _settings(**over):
    env = dict(ENV)
    env.update(over)
    return load_settings(env)


# ── /test 收件人路由 ────────────────────────────────────────────────

class _Msg:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)
        return _Msg()

    async def edit_text(self, text, **kw):
        self.texts.append(text)
        return None


class _Update:
    def __init__(self, chat_id: int) -> None:
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_message = _Msg()


class _Ctx:
    def __init__(self, app, args=None) -> None:
        self.application = app
        self.args = args or []


class _Service:
    """只提供 run_push 需要的 build_predictions。"""

    def __init__(self, predictions) -> None:
        self._p = predictions
        self.last_note = None

    async def build_predictions(self, **kw):
        return self._p


def _pred(fid: int = 1, minutes_ahead: int = 30):
    """一份"够推送用"的 Prediction。

    用真实 Prediction 而不是 SimpleNamespace：digest 视图会读
    p.fixture['league']['id']、p.analysis、p.kickoff、p.home/away，
    用假对象会在视图层炸掉，测的就不是收件人路由了。
    """
    from analyzer import LeagueModel, TeamStrength
    from service import Prediction

    return Prediction(
        fixture_id=fid,
        league="英超",
        round_label="第 1 轮",
        venue="主场",
        home="曼城",
        away="阿森纳",
        home_id=1,
        away_id=2,
        kickoff=datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead),
        analysis={"win_prob": 0.48, "draw_prob": 0.28, "loss_prob": 0.24},
        home_strength=TeamStrength(games_home=10, games_away=10),
        away_strength=TeamStrength(games_home=10, games_away=10),
        model=LeagueModel(),
        fixture={"id": fid, "league": {"id": 39},
                 "date": (datetime.now(timezone.utc)
                          + timedelta(minutes=minutes_ahead)).isoformat()},
    )


def test_run_push_defaults_to_chat_target():
    """不传 chat_id 时行为不变：仍发到配置的 CHAT_ID（定时任务依赖它）。"""
    settings = _settings()
    app = FakeApp(settings, _Service([_pred()]))
    res = run(run_push(app))
    assert res.sent == 1
    assert app.bot.sent[0]["chat_id"] == settings.chat_target


def test_run_push_uses_explicit_chat_id():
    """显式传入时发到该会话 —— 私聊里触发就进私聊。"""
    settings = _settings()
    app = FakeApp(settings, _Service([_pred()]))
    run(run_push(app, chat_id=777))
    assert app.bot.sent[0]["chat_id"] == 777
    assert app.bot.sent[0]["chat_id"] != settings.chat_target


def test_test_cmd_sends_to_current_chat_by_default():
    """/test 不带参数 → 发到当前会话，群里一条都没有。"""
    from commands.admin import test_cmd

    settings = _settings()
    app = FakeApp(settings, _Service([_pred()]))
    update = _Update(chat_id=777)          # 私聊
    ctx = _Ctx(app, args=[])
    run(test_cmd(update, ctx))
    assert app.bot.sent and all(m["chat_id"] == 777 for m in app.bot.sent)
    assert all(m["chat_id"] != settings.chat_target for m in app.bot.sent)


def test_test_cmd_group_arg_goes_to_chat_target():
    """/test group → 显式发到 CHAT_ID，保留"验证定时推送路径"的用途。"""
    from commands.admin import test_cmd

    settings = _settings()
    app = FakeApp(settings, _Service([_pred()]))
    ctx = _Ctx(app, args=["group"])
    run(test_cmd(_Update(chat_id=777), ctx))
    assert app.bot.sent and all(m["chat_id"] == settings.chat_target
                                for m in app.bot.sent)


def test_test_cmd_reports_destination():
    """回显必须写明目的地 —— 此前只说"已向 CHAT_ID 发送"，无法判断去哪。"""
    from commands.admin import test_cmd

    settings = _settings()
    app = FakeApp(settings, _Service([_pred()]))
    update = _Update(chat_id=777)
    run(test_cmd(update, _Ctx(app, args=[])))
    joined = " ".join(update.effective_message.texts)
    assert "当前会话" in joined


def test_notification_manager_target_helper():
    """显式优先、缺省退回；这是所有出口共用的真值来源。"""
    settings = _settings()
    app = FakeApp(settings)
    nm = NotificationManager(app)
    assert nm._target(42) == 42
    assert nm._target() == settings.chat_target
    assert nm._target(None) == settings.chat_target


@pytest.fixture(autouse=True)
def _clean_prematch_push():
    """每个用例前清空去重表。

    conftest 只清 matches/predictions/sync_log，而本模块所有用例都拿
    fixture 101 做实验 —— 不清表的话第一条用例写下的"已推送"会漏给后面
    每一条，表现为"本该推送的却返回 0"，而且看不出原因。
    """
    repo = PredictionRepository()
    try:
        conn = repo._connect()
        conn.execute("DELETE FROM prematch_push")
        conn.commit()
    except Exception:
        pass
    yield


# ── 赛前二次推送 ───────────────────────────────────────────────────

def _entry(fid: int, lead_seconds: float, lineup: bool, prediction=None):
    return {"fixture_id": fid, "lead_seconds": lead_seconds, "lineup": lineup,
            "prediction": prediction if prediction is not None else _pred(fid)}


def _push_app(**over):
    settings = _settings(**over)
    repo = PredictionRepository()
    service = SimpleNamespace(repo=repo, settings=settings)
    app = FakeApp(settings, service, repo)
    return app, repo, settings


def test_prematch_push_sends_once_when_lineup_ready():
    """拿到阵容 → 立刻推一次。"""
    app, repo, _ = _push_app()
    entries = [_entry(101, lead_seconds=35 * 60, lineup=True)]
    assert run(prematch_push(app, entries)) == 1
    assert len(app.bot.sent) == 1
    assert app.bot.sent[0]["chat_id"] == app.bot_data["settings"].chat_target
    assert repo.prematch_pushed(101)


def test_prematch_push_dedups_same_fixture():
    """同一场只推一次：赛前刷新每 15 分钟一轮，不去重就是刷屏。"""
    app, repo, _ = _push_app()
    entries = [_entry(101, lead_seconds=35 * 60, lineup=True)]
    run(prematch_push(app, entries))
    again = run(prematch_push(app, [_entry(101, lead_seconds=20 * 60, lineup=True)]))
    assert again == 0
    assert len(app.bot.sent) == 1


def test_prematch_push_waits_for_lineup_inside_window():
    """进窗口但还没拿到阵容 → 再等一轮（T-60 的阵容多半还没公布）。"""
    app, repo, _ = _push_app()
    entries = [_entry(101, lead_seconds=55 * 60, lineup=False)]
    assert run(prematch_push(app, entries)) == 0
    assert app.bot.sent == []
    assert not repo.prematch_pushed(101)


def test_prematch_push_forces_near_kickoff_without_lineup():
    """始终拿不到阵容（部分赛事官方不提供）→ 临近开赛兜底推，不能无限等。"""
    app, repo, _ = _push_app()
    entries = [_entry(101, lead_seconds=15 * 60, lineup=False)]
    assert run(prematch_push(app, entries)) == 1
    assert repo.prematch_pushed(101)


def test_prematch_push_skips_outside_lead_window():
    """窗口外不推 —— 那属于早间推送的职责，重复推是噪音。"""
    app, repo, _ = _push_app()
    entries = [_entry(101, lead_seconds=90 * 60, lineup=True)]
    assert run(prematch_push(app, entries)) == 0
    assert app.bot.sent == []


def test_prematch_push_disabled_is_noop():
    app, repo, _ = _push_app(PREMATCH_PUSH_ENABLED="0")
    entries = [_entry(101, lead_seconds=30 * 60, lineup=True)]
    assert run(prematch_push(app, entries)) == 0
    assert app.bot.sent == []


def test_prematch_push_note_mentions_live_data():
    """推送必须说明这是更新版 —— 否则用户以为和早上那条重复。"""
    app, repo, _ = _push_app()
    run(prematch_push(app, [_entry(101, lead_seconds=30 * 60, lineup=True)]))
    assert PREMATCH_PUSH_NOTE in app.bot.sent[0]["text"]


def test_prematch_push_marks_only_after_send():
    """标记早于发送会把一次失败变成永久不再推送。"""
    app, repo, _ = _push_app()

    class BoomBot(FakeBot):
        async def send_message(self, **kw):
            raise RuntimeError("网络抖动")

    app.bot = BoomBot()
    with pytest.raises(RuntimeError):
        run(prematch_push(app, [_entry(101, lead_seconds=30 * 60, lineup=True)]))
    assert not repo.prematch_pushed(101)
