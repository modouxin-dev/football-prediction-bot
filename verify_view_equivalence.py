#!/usr/bin/env python3
"""视图迁移等价性验证 / View migration equivalence check.

把 bot_handler.py 从 1135 行拆成 views/ + keyboards.py 时，最大的风险是
**迁移过程中悄悄改了行为**。543 个测试能覆盖大部分，但渲染类改动往往
只影响某个字符串细节，测试未必断言到。

本脚本对**同一组输入**调用全部视图方法，输出渲染结果的哈希，
用于对比迁移前后是否逐字节一致。

用法（由 verify_stage.sh 调用）：
    python verify_view_equivalence.py            # 输出 JSON 哈希
    python verify_view_equivalence.py --raw NAME # 输出某个方法的原始文本

注意：format_deep_report 含「数据更新时间」时间戳，跨分钟运行会变化，
比对时需先归一化时间片段（见 verify_stage.sh 中的归一化步骤）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from datetime import datetime, timedelta, timezone  # noqa: E402

from api_client import flatten_standings  # noqa: E402
from bot_handler import BotUI  # noqa: E402
from config import load_settings  # noqa: E402
from service import PredictionService  # noqa: E402
from tests.sample_data import (  # noqa: E402
    FakeAPI, default_fixtures, fixture, odds_response, standings_response,
)

ENV = {"TELEGRAM_TOKEN": "1:A", "RAPID_API_KEY": "k", "CHAT_ID": "555",
       "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)
run = asyncio.run


class FullAPI(FakeAPI):
    """带积分榜与赔率的仿真 API（与 tests/test_prediction.py 中一致）。"""

    def __init__(self, fixtures=None, standings=None, odds=None):
        super().__init__({})
        self.items = fixtures if fixtures is not None else default_fixtures()
        self.standings = standings if standings is not None else flatten_standings(standings_response())
        self.odds = odds if odds is not None else odds_response([("Bet365", 2.1, 3.3, 3.5)])

    async def get_fixtures(self, *a, **k):
        return self.items

    async def get_standings(self, *a, **k):
        return self.standings

    async def get_odds(self, *a, **k):
        return self.odds


def build_fixtures(n=3):
    base = datetime.now(timezone.utc)
    return [
        fixture(1001 + i, 1, f"主队{i + 1}", 2, f"客队{i + 1}",
                base + timedelta(hours=i + 1), status="NS")
        for i in range(n)
    ]


def cases():
    """返回 {名称: 无参可调用}。每次调用都会重新构造输入，保证两处一致。"""
    fx = build_fixtures()
    svc = PredictionService(SETTINGS, FullAPI(fx))
    p = run(svc.predict_fixture(1001, fx))
    report = run(svc.analyze_fixture(1001, fx))
    tz = SETTINGS.timezone
    st = flatten_standings(standings_response())
    return {
        "format_prediction": lambda: BotUI.format_prediction(p, tz),
        "format_prediction_card": lambda: BotUI.format_prediction_card(p, tz),
        "format_deep_analysis": lambda: BotUI.format_deep_analysis(p, tz),
        "format_odds_detail": lambda: BotUI.format_odds_detail(p, tz),
        "format_deep_report": lambda: BotUI.format_deep_report(report, tz),
        "confidence_text": lambda: BotUI.confidence_text(p),
        "risk_lines": lambda: BotUI.risk_lines(p),
        "get_strategy": lambda: BotUI.get_strategy(p),
        "get_confidence": lambda: BotUI.get_confidence(p),
        "matchup": lambda: BotUI.matchup(p, tz),
        "tz_label": lambda: BotUI.tz_label(tz, p.kickoff),
        "fmt_time": lambda: BotUI.fmt_time(p.kickoff, tz),
        "format_h2h": lambda: BotUI.format_h2h(p, [], tz),
        "format_welcome": lambda: BotUI.format_welcome(SETTINGS),
        "format_menu": lambda: BotUI.format_menu(SETTINGS),
        "format_help": lambda: BotUI.format_help(),
        "format_coming": lambda: BotUI.format_coming("stats"),
        "error_hint": lambda: BotUI.error_hint(ValueError("HTTP 403 无权访问")),
        "web_text_deployed": lambda: BotUI.web_text("https://x.example.com"),
        "web_text_undeployed": lambda: BotUI.web_text(""),
        "format_fixtures_page": lambda: BotUI.format_fixtures_page(
            fx, 0, 1, "today", tz, "英超", None, {}),
        "format_standings_page": lambda: BotUI.format_standings_page(
            st, tz, 20, "英超", datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)),
    }


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--raw":
        value = cases()[sys.argv[2]]()
        print("|".join(value) if isinstance(value, list) else str(value))
        return 0

    out = {}
    for name, fn in cases().items():
        try:
            value = fn()
            value = "|".join(value) if isinstance(value, list) else str(value)
            out[name] = hashlib.sha256(value.encode()).hexdigest()[:16]
        except Exception as exc:  # 崩溃也要记录下来，两处应一致
            out[name] = f"ERR:{type(exc).__name__}"
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
