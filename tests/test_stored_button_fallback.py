"""端到端：推送卡片上的按钮，在「内存里没有」时必须落到存档而不是报错。

场景对应用户实际反馈：预测是**自动推送**的（一定算过、一定落盘），
过一阵子点卡片上的按钮，却提示「该场比赛已不在今日赛程中 / 预测已过期」。

根因是 service.get() 只查内存，机器人重启或缓存淘汰后必然 KeyError。
自动推送的预测既然已落盘，就该展示存档结论，而不是把用户打回赛程页。

本文件锁住四条性质（针对 on_button 这个卡片按钮总路由）：
1. 库里有结论 → 展示存档，不再报「已过期」；
2. 展示的是**当时**的数字，且标注这是存档；
3. 库里也没有 → 回落到原提示（不吞故障，也不假装成功）；
4. **只兜 KeyError**，API 故障必须原样上报。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import main as main_mod
from views.prediction import PredictionView

FIXTURE_ID = "888001"

ROW = {
    "fixture_id": FIXTURE_ID,
    "league": "英超",
    "home": "阿森纳",
    "away": "埃弗顿",
    "kickoff": "2026-10-24T18:30:00+00:00",
    "home_prob": 0.56,
    "draw_prob": 0.23,
    "away_prob": 0.21,
    "best_score": "2-1",
    "result": "主胜",
    "actual_home": None,
    "actual_away": None,
}


class _FakeQuery:
    """只实现 on_button / edit_view 真正用到的部分。"""

    def __init__(self, data: str):
        self.data = data
        self.alerted: str | None = None
        self.edited: str | None = None

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.alerted = text

    async def edit_message_text(self, text: str, **kwargs):
        self.edited = text


class _Service:
    """内存里没有（重启/淘汰）；库里可有可无，由 stored 控制。"""

    def __init__(self, stored):
        self._stored = stored

    def get(self, fixture_id):
        raise KeyError(str(fixture_id))  # 等价于机器人刚重启

    def stored_prediction(self, fixture_id):
        return self._stored


def _run(action: str, stored):
    query = _FakeQuery(f"{action}:{FIXTURE_ID}")
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "settings": SimpleNamespace(timezone=__import__("zoneinfo").ZoneInfo("Asia/Shanghai")),
        "service": _Service(stored),
    }))
    asyncio.run(main_mod.on_button(SimpleNamespace(callback_query=query), ctx))
    return query


# ---- 1. 库里有结论：展示存档，不再报「已过期」 --------------------------------

def test_stored_card_is_shown_instead_of_expired_alert():
    q = _run("home", dict(ROW))
    assert q.edited, "库里有结论时必须展示存档，而不是弹「已过期」"
    assert q.alerted is None, "不该再弹「这条预测已过期」"


def test_stored_card_shows_the_original_numbers():
    """展示的必须是**当时**的数字——用户就是要回看这个。"""
    q = _run("home", dict(ROW))
    assert "56%" in q.edited and "23%" in q.edited and "21%" in q.edited
    assert "阿森纳" in q.edited and "埃弗顿" in q.edited
    assert "2-1" in q.edited


def test_stored_card_is_marked_as_archive():
    """必须说明这是存档，否则用户会当成实时预测。"""
    q = _run("home", dict(ROW))
    assert "存档" in q.edited


def test_all_card_actions_fall_back_to_archive():
    """卡片上每个按钮都要能落到存档：深度分析 / 历史交锋 / 赔率 / 刷新。"""
    for action in ("deep", "h2h", "odds", "refresh"):
        q = _run(action, dict(ROW))
        assert q.edited and "阿森纳" in q.edited, f"{action} 未兜底"


# ---- 2. 库里也没有：回落到原提示 ---------------------------------------------

def test_falls_back_to_original_hint_when_nothing_stored():
    """确实没算过的比赛，照旧提示——不能凭空造一张卡片。"""
    q = _run("home", None)
    assert q.edited is None, "库里没有时不得展示卡片"
    assert q.alerted and "过期" in q.alerted


def test_incomplete_row_is_refused_not_guessed():
    """概率不全就当没有：宁可提示过期，也不补占位数字凑一张卡。"""
    row = dict(ROW, draw_prob=None)
    q = _run("home", row)
    assert q.edited is None, "结论不完整时必须拒绝展示"


# ---- 3. 不吞 API 故障 -------------------------------------------------------

def test_api_error_is_not_disguised_as_missing_match(monkeypatch):
    """只兜 KeyError。取数失败必须原样上报，不能伪装成「没有这场比赛」。"""
    from service import APIError

    class _Boom(_Service):
        def get(self, fixture_id):
            raise APIError("上游超时")

    class _Query(_FakeQuery):
        pass

    query = _Query(f"home:{FIXTURE_ID}")
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "settings": SimpleNamespace(timezone=__import__("zoneinfo").ZoneInfo("Asia/Shanghai")),
        "service": _Boom(dict(ROW)),
    }))
    with pytest.raises(APIError):
        asyncio.run(main_mod.on_button(SimpleNamespace(callback_query=query), ctx))
