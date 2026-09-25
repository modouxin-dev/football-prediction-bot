"""模型信心等级（🟢高/🟡中/🔴低）与 HTML 卡片输出测试。"""
import pytest

from analyzer import calculate_prediction_level, validate_probabilities
from bot_handler import build_prediction_payload, split_html_blocks
from config import load_settings
from service import PredictionService

SETTINGS = load_settings(
    {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k", "CHAT_ID": "555",
     "SEASON": "2026", "ADMIN_ID": "555"}
)

DISCLAIM_WORDS = ("必胜", "稳赢", "100%准确", "确定中奖")


# ---- 三级判定 -----------------------------------------------------------------
def test_high_confidence():
    result = calculate_prediction_level({"home_win": 0.75, "draw": 0.15, "away_win": 0.10})
    assert result["key"] == "high"
    assert result["emoji"] == "🟢" and result["name"] == "高"
    assert result["result"] == "主胜"


def test_medium_confidence():
    result = calculate_prediction_level({"home_win": 0.55, "draw": 0.30, "away_win": 0.15})
    assert result["key"] == "medium"
    assert result["emoji"] == "🟡"


def test_low_confidence():
    result = calculate_prediction_level({"home_win": 0.42, "draw": 0.32, "away_win": 0.26})
    assert result["key"] == "low"
    assert result["emoji"] == "🔴"


def test_medium_prob_but_small_gap_is_low():
    """概率达到 50% 但领先不足 8 个百分点 → 只能算低信心（两条件须同时满足）。"""
    result = calculate_prediction_level({"home_win": 0.50, "draw": 0.44, "away_win": 0.06})
    assert result["key"] == "low"
    assert result["gap"] < 0.08


def test_level_is_calculated_not_manual():
    """等级必须由概率推导：同一组概率重复调用结果稳定，且 result 是概率最高的项。"""
    probs = {"home_win": 0.20, "draw": 0.25, "away_win": 0.55}
    r1 = calculate_prediction_level(probs)
    r2 = calculate_prediction_level(probs)
    assert r1 == r2
    assert r1["result"] == "客胜"


# ---- 输入校验 -----------------------------------------------------------------
def test_missing_field_raises():
    with pytest.raises(ValueError, match="缺少概率字段"):
        calculate_prediction_level({"home_win": 0.5, "draw": 0.5})


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_out_of_range_raises(bad):
    with pytest.raises(ValueError, match="概率超出范围"):
        validate_probabilities({"home_win": bad, "draw": 0.5, "away_win": 0.5})


def test_total_not_one_raises():
    with pytest.raises(ValueError, match="概率总和异常"):
        validate_probabilities({"home_win": 0.5, "draw": 0.5, "away_win": 0.5})


def test_total_within_tolerance_passes():
    validate_probabilities({"home_win": 0.334, "draw": 0.333, "away_win": 0.333})


# ---- 统一数据结构 ---------------------------------------------------------------
def test_payload_contains_required_fields():
    from datetime import datetime, timedelta, timezone

    from tests.test_prediction import FullAPI, now_fixtures  # 复用已有桩

    import asyncio

    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = asyncio.run(svc.predict_fixture(1001, now_fixtures()))
    payload = build_prediction_payload(p, SETTINGS.timezone)

    for key in ("home_team", "away_team", "home_win", "draw", "away_win",
                "result", "level_key", "level_name", "level_emoji", "source", "season"):
        assert key in payload, key
    assert payload["level_key"] in ("high", "medium", "low")
    # 概率总和约等于 100%
    total = payload["home_win"] + payload["draw"] + payload["away_win"]
    assert abs(total - 1.0) < 0.02


# ---- HTML 输出 -----------------------------------------------------------------
def test_card_html_escapes_special_characters():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    """球队名含 < & > 时必须转义，不能拼进 HTML 造成解析错乱。"""
    from bot_handler import BotUI
    from unittest.mock import Mock

    p = Mock()
    p.home = 'A <b> & "x"'
    p.away = "B > C"
    p.season = 2026
    p.source = "API-Football"
    p.kickoff = datetime.now(timezone.utc)
    p.level = {"name": "中", "emoji": "🟡", "key": "medium", "result": "主胜"}
    p.insufficient = False
    p.low_sample = False
    p.has_team_data = True
    p.data_completeness = "完整"
    p.home_strength = SimpleNamespace(attack_home=1.1, defense_home=0.9)
    p.away_strength = SimpleNamespace(attack_away=1.0, defense_away=1.1)
    p.league = "Premier League"
    p.analysis = {"win_prob": 0.5, "draw_prob": 0.3, "loss_prob": 0.2,
                  "best_score": "1-0", "lambda_home": 1.2, "lambda_away": 0.9}
    p.has_team_data = True
    p.low_sample = False
    p.data_completeness = "完整"
    p.created_at = datetime.now(timezone.utc)
    p.best = None
    p.odds = None

    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    assert "&lt;b&gt;" in text and "&amp;" in text  # 已转义
    assert "<b> & " not in text


def test_no_markdown_table_in_output():
    """Telegram 移动端对 Markdown 表格兼容性差，输出里不得出现表格语法。"""
    from tests.test_prediction import FullAPI, now_fixtures
    import asyncio

    from bot_handler import BotUI

    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = asyncio.run(svc.predict_fixture(1001, now_fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    # 不得出现 Markdown 表格：表头分隔行（| --- |）或整行以 | 包裹
    assert "---" not in text
    assert not [ln for ln in text.split("\n") if ln.strip().startswith("|")]


def test_card_shows_source_and_season():
    from types import SimpleNamespace

    from tests.test_prediction import FullAPI, now_fixtures
    import asyncio

    from bot_handler import BotUI

    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = asyncio.run(svc.predict_fixture(1001, now_fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    # 脚注小字承载来源与赛季，正文不再堆这些字段
    assert "SOURCE:" in text and "SEASON: 2026" in text


def test_percentages_use_one_decimal():
    from tests.test_prediction import FullAPI, now_fixtures
    import asyncio
    import re

    from bot_handler import BotUI

    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = asyncio.run(svc.predict_fixture(1001, now_fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    for m in re.findall(r"(\d+\.\d)%", text):
        assert len(m.split(".")[1]) == 1, m


@pytest.mark.parametrize("word", DISCLAIM_WORDS)
def test_no_forbidden_absolute_words(word):
    """禁止出现必胜/稳赢等绝对化表述。"""
    from tests.test_prediction import FullAPI, now_fixtures
    import asyncio

    from bot_handler import BotUI

    svc = PredictionService(SETTINGS, FullAPI(now_fixtures()))
    p = asyncio.run(svc.predict_fixture(1001, now_fixtures()))
    text = BotUI.format_prediction_card(p, SETTINGS.timezone)
    assert word not in text


# ---- 长文本拆分 -----------------------------------------------------------------
def test_long_text_is_split():
    long_text = "\n".join(f"line {i}" for i in range(400))
    blocks = split_html_blocks(long_text, limit=500)
    assert len(blocks) > 1
    assert all(len(b) <= 500 for b in blocks)
    assert "".join(blocks).count("line") == 400


def test_short_text_not_split():
    assert split_html_blocks("short text", limit=500) == ["short text"]
