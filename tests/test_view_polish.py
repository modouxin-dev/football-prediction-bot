"""视图排版测试 / View polish tests (Stage 10)

覆盖两类东西：

1. 对齐原语（hbar / display_width / align_cjk / status_emoji）
   —— 中英混排的等宽表格必须按显示宽度算，按字符数补空格必然错开。
2. 概率行的百分比列宽一致
   —— 「9%」和「100%」若宽度不同，右侧数字会一前一后，这是本次美化的核心诉求。

另有一条回归测试：预测主卡必须能脱离 BotUI 的 MRO 独立渲染。
"""
from __future__ import annotations

import asyncio
import re

import pytest

from formatkit import align_cjk, display_width, hbar, pad_cjk, status_emoji
from views.prediction import PredictionView

from tests.test_prediction import SETTINGS, FullAPI, now_fixtures


def _run(coro):
    # 必须用 asyncio.run 而不是 get_event_loop().run_until_complete：
    # 全量跑测试时前一个用例可能已关闭默认循环，复用会抛 RuntimeError。
    return asyncio.run(coro)


def _prediction():
    svc = __import__("service").PredictionService(SETTINGS, FullAPI(now_fixtures()))
    return _run(svc.predict_fixture(1001, now_fixtures()))


# ---- 对齐原语 ----------------------------------------------------------------

@pytest.mark.parametrize("prob,expected", [
    (0.0, "░" * 10),
    (1.0, "█" * 10),
    (0.5, "█████" + "░" * 5),
])
def test_hbar_basic(prob, expected):
    assert hbar(prob) == expected


@pytest.mark.parametrize("prob", [-0.5, 1.5])
def test_hbar_clamps_out_of_range(prob):
    """越界概率不能抛异常，也不能画出超过 10 格的条。"""
    assert len(hbar(prob)) == 10


def test_hbar_custom_width():
    assert len(hbar(0.5, width=8)) == 8


def test_display_width_counts_cjk_as_two():
    assert display_width("abc") == 3
    assert display_width("阿森纳") == 6
    assert display_width("阿森纳(A)") == 9  # 3×2 + ( + A + )


def test_align_cjk_pads_to_display_width():
    """补空格后显示宽度必须精确等于目标列宽——这是对齐的全部意义。"""
    assert display_width(align_cjk("阿森纳", 10)) == 10
    assert display_width(align_cjk("Arsenal", 10)) == 10
    assert display_width(pad_cjk("曼城", 8)) == 8


def test_align_cjk_right_aligns():
    out = align_cjk("主", 6, "right")
    assert display_width(out) == 6 and out.endswith("主")


def test_align_cjk_truncates_with_ellipsis():
    out = align_cjk("非常非常长的球队名字", 8)
    # 截断后仍要补足列宽：省略号只占 1 列，否则总宽差 1，表格照样错开
    assert display_width(out) == 8
    assert "…" in out


@pytest.mark.parametrize("short,expected", [
    ("NS", "🕐"), ("FT", "✅"), ("LIVE", "🔴"), ("PST", "⏸"), ("CANC", "❌"),
])
def test_status_emoji_known(short, expected):
    assert status_emoji(short) == expected


@pytest.mark.parametrize("short", ["", None, "XXYY"])
def test_status_emoji_unknown_never_raises(short):
    """未收录的状态必须给兜底图标，不能抛异常把整页赛程搞崩。"""
    assert status_emoji(short) == "⚪"


# ---- 概率行对齐 --------------------------------------------------------------

def _prob_percentages(text: str) -> list[str]:
    """抽出概率行里 <code> 块内的百分比，用于校验列宽一致。"""
    return re.findall(r"<code>[█░▰▱ ]+\s*(\d+%)</code>", text)


def test_card_probability_percentages_share_one_width():
    p = _prediction()
    text = PredictionView.format_prediction_card(p, SETTINGS.timezone)
    pcts = _prob_percentages(text)
    assert len(pcts) == 3, f"应抽出三行概率，实际 {pcts}"
    # 补位后每行「条形+百分比」的等宽块长度一致，右侧数字才不会错开
    widths = {len(m) for m in re.findall(r"<code>([^<]+)</code>", text) if "%" in m}
    assert len(widths) == 1, f"概率行宽度不一致：{widths}"


def test_card_marks_the_top_outcome():
    """最高概率那行要有 👈，让用户一眼看到结论，而不是自己去比数字。"""
    p = _prediction()
    text = PredictionView.format_prediction_card(p, SETTINGS.timezone)
    assert text.count("👈") == 1


def test_predict_command_percentages_share_one_width():
    p = _prediction()
    text = PredictionView.format_prediction(p, SETTINGS.timezone)
    # 只取带概率条的等宽块：正文里还有「价值偏差 +19.00%」这类行，
    # 把它们算进来会误判成宽度不一致
    widths = {len(m) for m in re.findall(r"<code>([▰▱ ]+\s*\d+%)</code>", text)}
    assert len(widths) == 3 or len(widths) == 1, f"概率行宽度不一致：{widths}"
    assert len(widths) == 1, f"概率行宽度不一致：{widths}"


# ---- 回归：主卡不再依赖 MRO ---------------------------------------------------

def test_prediction_card_renders_without_botui_mro():
    """预测主卡必须能直接由 PredictionView 渲染。

    历史实现用 cls._hbar()，而 _hbar 定义在 CommonView 上。经 BotUI
    （同时继承 CommonView 与 PredictionView）调用时侥幸能取到；一旦有人
    直接调 PredictionView.format_prediction_card 就是 AttributeError。
    现在概率条改用 formatkit.hbar 模块级函数，不再依赖继承链。
    """
    p = _prediction()
    text = PredictionView.format_prediction_card(p, SETTINGS.timezone)
    assert "FOOTBALL INSIGHT" in text
