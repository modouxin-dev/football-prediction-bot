"""单场预测卡：紧凑概率行与按钮精简。

为什么要有紧凑概率行：
    三行进度条适合细看，但转发、截图或快速扫一眼时，需要「48% — 28% — 24%」
    这种能整段引用的形式。它挂在结论行尾部，不额外占行——正文已经很长，
    多一行就让手机上要多滑一屏。

为什么要精简按钮：
    预测卡原先 4 行 7 个按钮，其中「联赛排名」「历史命中率」与这场比赛无关，
    「今日赛程」又与底部「返回赛程」重复。同一层出现两个回赛程的入口，
    用户不知道该点哪个；按钮一多，真正要点的「详细分析」反而被淹没。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bot_handler import BotUI
from config import load_settings
from keyboards import Keyboards
from service import PredictionService
from tests.sample_data import (FakeAPI, fixture, flatten_standings,
                               odds_response, standings_response)

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


class FullAPI(FakeAPI):
    def __init__(self, fixtures):
        super().__init__({})
        self.items = fixtures
        self.standings = flatten_standings(standings_response())
        self.odds = odds_response([("Bet365", 2.1, 3.3, 3.5)])

    async def get_fixtures(self, league_id, season, date_from, date_to):
        return self.items

    async def get_standings(self, league_id, season):
        return self.standings

    async def get_odds(self, fixture_id, fresh=False):
        return self.odds


def _fixtures():
    base = datetime.now(timezone.utc)
    return [fixture(1001, 1, "曼城", 2, "阿森纳", base + timedelta(hours=2), status="NS")]


def _card() -> str:
    api = FullAPI(_fixtures())
    svc = PredictionService(SETTINGS, api)
    p = asyncio.run(svc.predict_fixture(1001, _fixtures()))
    return BotUI.format_prediction_card(p, SETTINGS.timezone)


# ---- 紧凑概率行 --------------------------------------------------------------------
def test_card_has_compact_prob_line():
    """结论行必须带一行可整段引用的百分比，且顺序固定为主—平—客。"""
    text = _card()
    assert "预测结果" in text
    line = next(l for l in text.splitlines() if "预测结果" in l)
    assert " — " in line, f"结论行缺少紧凑概率：{line}"


def _compact_values(line: str) -> list[str]:
    """从结论行取出紧凑概率的三个数值。

    数值在 <code>…</code> 里，直接按 — 切会把前缀一起切进来，
    必须先定位等宽块。
    """
    import re

    m = re.search(r"<code>([^<]*—[^<]*)</code>", line)
    assert m, f"结论行里没有紧凑概率块：{line}"
    return [v.strip().rstrip("%") for v in m.group(1).split("—")]


def test_compact_probs_match_detail_probs():
    """紧凑行的三个数必须与三行进度条一致，不能两个来源各算一套。"""
    text = _card()
    lines = text.splitlines()
    detail = [
        l for l in lines
        if ("主胜" in l or "平局" in l or "客胜" in l) and "%" in l
    ]
    detail_vals = [l.rstrip(" 👈").rsplit("%", 2)[0][-3:].strip() for l in detail]
    line = next(l for l in lines if "预测结果" in l)
    compact = _compact_values(line)
    assert len(compact) == 3, f"紧凑概率不是三段：{line}"
    assert compact == detail_vals, f"紧凑值 {compact} 与明细 {detail_vals} 不一致"


def test_compact_probs_order_is_home_draw_away():
    """顺序必须与模型输出一致：主胜、平局、客胜。

    写反了会让用户把客胜概率当成平局，是静默错误——数字全对但含义全错。
    """
    api = FullAPI(_fixtures())
    svc = PredictionService(SETTINGS, api)
    p = asyncio.run(svc.predict_fixture(1001, _fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    line = next(l for l in text.splitlines() if "预测结果" in l)
    compact = [int(v) for v in _compact_values(line)]
    detail = [p.analysis["win_prob"], p.analysis["draw_prob"], p.analysis["loss_prob"]]
    assert compact == [round(x * 100) for x in detail], (
        f"紧凑行 {compact} 与模型概率 {detail} 顺序或取值不一致"
    )


# ---- 按钮精简 ----------------------------------------------------------------------
def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_prediction_keyboard_has_no_global_entries():
    """与这场比赛无关的全局入口（排名、命中率）不该出现在单场卡片上。"""
    data = _callbacks(Keyboards.prediction_keyboard(1001))
    assert "menu:standings" not in data, "联赛排名与单场比赛无关，应回主菜单"
    assert "menu:stats" not in data, "历史命中率与单场比赛无关，应回主菜单"


def test_prediction_keyboard_keeps_match_actions():
    """本场能做的操作必须都在：深入分析、概率图表、刷新、返回、主菜单。"""
    data = set(_callbacks(Keyboards.prediction_keyboard(1001)))
    assert {"deep:1001", "chart:prob:1001", "refresh:1001"} <= data
    assert "menu:fixtures" in data and "menu:home" in data


def test_prediction_keyboard_row_count_is_two():
    """4 行 7 个 → 2 行 5 个；按钮少了才看得清哪个是本场操作。

    原先「🔄 刷新数据」独占一行，Telegram 会把它拉满整行，变成又宽
    又空的长条。现并入出口行，按钮数量不变但少一行、不再有宽条。
    """
    markup = Keyboards.prediction_keyboard(1001)
    flat = [b for row in markup.inline_keyboard for b in row]
    assert len(markup.inline_keyboard) == 2, f"行数 {len(markup.inline_keyboard)}"
    assert len(flat) == 5, f"按钮数 {len(flat)}"


def test_prediction_keyboard_single_fixtures_exit():
    """返回赛程只能有一个入口，两个会让人不确定该点哪个。"""
    data = _callbacks(Keyboards.prediction_keyboard(1001))
    assert data.count("menu:fixtures") == 1, f"返回赛程出现了 {data.count('menu:fixtures')} 次"


@pytest.mark.parametrize("fid", [1, 1001, 999999])
def test_prediction_keyboard_callback_length(fid):
    """callback_data 上限 64 字节，超了 Telegram 直接拒绝整条消息。"""
    for cb in _callbacks(Keyboards.prediction_keyboard(fid)):
        assert len(cb.encode()) <= 64, f"{cb} 超长"
