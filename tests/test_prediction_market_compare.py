"""单场预测卡：整合「模型 vs 市场」对照。

为什么要把对照放进主卡：
    判断「模型相对市场有没有增量」的界面原先孤立在 format_odds_detail，
    要点进第二页才看得到。而主卡显示的概率本身是融合口径（线上 0.75 市场
    + 0.25 统计），不给市场基准，用户无法判断这个数字到底是模型判断还是
    照抄市场——差值才是模型真正贡献的部分。

为什么只放对照块、不放各家公司报价：
    主卡已有 47 行，再塞 6 行公司报价会让手机上多滑一屏。完整表留在
    format_odds_detail，主卡只回答「跟市场差多少」。

为什么两处必须同源：
    主卡与详情页各写一套差值算法时，出现「主卡 +5.1pp、详情页 +2.3pp」
    这种不一致后无从排查——两边看起来都对，只有对比才发现。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from bot_handler import BotUI
from config import load_settings
from formatkit import display_width
from service import PredictionService
from tests.sample_data import (FakeAPI, fixture, flatten_standings,
                               odds_response, standings_response)

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)

# 主卡与详情页共用的差值格式：+5.1pp
GAP_RE = re.compile(r"([+-]\d+\.\d)pp")


class FullAPI(FakeAPI):
    def __init__(self, fixtures, odds=None):
        # odds 传 [] 表示「没有赔率」；FakeAPI 的 None 会走默认预置值，
        # 所以这里必须显式传空列表才能造出无赔率场景。
        super().__init__({}, odds=odds_response([("Bet365", 2.1, 3.3, 3.5)])
                         if odds is None else odds)
        self.items = fixtures
        self.standings = flatten_standings(standings_response())

    async def get_fixtures(self, league_id, season, date_from, date_to):
        return self.items

    async def get_standings(self, league_id, season):
        return self.standings


def _fixtures():
    base = datetime.now(timezone.utc)
    return [fixture(1001, 1, "曼城", 2, "阿森纳", base + timedelta(hours=2), status="NS")]


def _prediction(odds=None):
    fx = _fixtures()
    api = FullAPI(fx, odds=odds)
    svc = PredictionService(SETTINGS, api)
    return asyncio.run(svc.predict_fixture(1001, fx))


def _card(odds=None) -> str:
    return BotUI.format_prediction_card(_prediction(odds), SETTINGS.timezone)


def _pre_blocks(text: str) -> list[list[str]]:
    """取出所有 <pre> 块，返回每块的行列表（已去 HTML 转义）。"""
    blocks = []
    for m in re.finditer(r"<pre>(.*?)</pre>", text, re.S):
        body = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        blocks.append(body.split("\n"))
    return blocks


def test_card_shows_market_compare_when_odds_present():
    """有赔率时，主卡必须给出市场对照——否则融合概率无从解读。"""
    card = _card()
    assert "市场对照" in card, "主卡缺少市场对照节"
    assert "市场(去水)" in card, "主卡缺少去水后的市场概率基准"


def test_card_hides_market_compare_without_odds():
    """没有赔率时不显示该节：不能拿空数据造出一张对比表。"""
    card = _card(odds=[])
    assert "市场对照" not in card, "无赔率时不应出现市场对照节"
    assert "市场(去水)" not in card


def test_card_gap_matches_odds_detail_page():
    """主卡与详情页的差值必须同源——两处算法分叉后无法被发现。"""
    p = _prediction()
    card = BotUI.format_prediction_card(p, SETTINGS.timezone)
    detail = BotUI.format_odds_detail(p, SETTINGS.timezone)
    card_gaps = GAP_RE.findall(card)
    detail_gaps = GAP_RE.findall(detail)
    assert card_gaps, "主卡没有输出任何差值"
    assert card_gaps == detail_gaps, (
        f"主卡与详情页差值不一致：主卡 {card_gaps} / 详情页 {detail_gaps}"
    )


def test_compare_block_rows_are_same_width():
    """对照块表头与数据行必须同宽——差一列整张表就歪了。"""
    card = _card()
    blocks = [b for b in _pre_blocks(card) if any("市场(去水)" in ln for ln in b)]
    assert blocks, "主卡没有渲染出对照块"
    rows = blocks[0]
    widths = {display_width(ln) for ln in rows}
    assert len(widths) == 1, f"对照块各行宽度不一致：{widths}"


def test_compare_block_uses_pp_not_percent():
    """差值用百分点（pp）：写成 +5.1% 会被读成「概率 5.1%」。"""
    card = _card()
    assert "pp" in card, "差值应使用 pp 单位"
    # 差值行不能出现裸的百分号写法（允许概率行用 %）
    rows = [ln for b in _pre_blocks(card) for ln in b if ln.startswith("差值")]
    assert rows, "对照块缺少差值行"
    assert all("%" not in ln for ln in rows), f"差值行不应使用 %：{rows}"
