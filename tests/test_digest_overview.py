"""每日推送「焦点战总览」的单测 / Digest overview tests.

总览页是每天唯一一条定时推送的正文，改排版最容易悄悄毁掉的就是它。
这里守四条红线：

1. **焦点战不超过 3 场**：再多就失去「一眼扫完」的意义，推送会退化成长列表
2. **焦点战选取确定**：同一批比赛任何时候算出的三场必须一样，否则用户会
   以为推送在乱跳
3. **其余场次折叠**：只给「联赛 + 场数」和「剩余 N 场」，不逐场平铺
4. **按钮可点且合法**：callback_data 不超过 Telegram 的 64 字节硬上限，
   按钮文案在移动端一行内

全部用假数据构造，不碰数据库、不发网络请求。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from formatkit import display_width
from keyboards import Keyboards
from templates import DISCLAIMER, LEAGUE_SHORT
from views.digest import (BUTTON_MAX_COLS, FOCUS_LIMIT, DigestView,
                          _pick_focus)

TZ = ZoneInfo("Asia/Shanghai")

# Telegram callback_data 硬上限（字节）。超过整个按钮会失效。
CALLBACK_MAX_BYTES = 64


def _mk(fixture_id, league_id, home, away, minute, probs=(0.55, 0.25, 0.20)):
    """构造一个够视图用的假预测。"""
    class _P:
        pass

    p = _P()
    p.fixture_id = fixture_id
    p.home = home
    p.away = away
    p.kickoff = datetime(2026, 10, 5, 19, minute, tzinfo=timezone.utc)
    p.analysis = {
        "win_prob": probs[0],
        "draw_prob": probs[1],
        "loss_prob": probs[2],
    }
    p.best = None
    p.fixture = {"league": {"id": league_id}}
    return p


def _overview(preds, note=None) -> str:
    return DigestView.format_daily_overview(preds, None, TZ, note=note)


# ---- 焦点战选取 ------------------------------------------------------------

def test_focus_capped_at_limit():
    """焦点战最多 3 场：给 8 场也只能出 3 场，否则推送又变成长列表。"""
    preds = [_mk(i, 39, f"Team{i}A", f"Team{i}B", 10 + i) for i in range(1, 9)]
    text = _overview(preds)
    # 概率三连是焦点战独有标记，出现次数 = 焦点战场数
    assert text.count("📊 <code>") == FOCUS_LIMIT


def test_focus_prefers_higher_priority_league():
    """焦点战优先取联赛优先级高的（英超 39 排在意甲 135 之前）。"""
    preds = [
        _mk(1, 135, "Lazio", "Roma", 10),      # 意甲，时间最早
        _mk(2, 39, "Arsenal", "Chelsea", 30),  # 英超，时间晚
    ]
    focus = _pick_focus(preds)
    assert len(focus) == 2
    # 英超排在 LEAGUE_ORDER 更前，应优先入选
    assert focus[0].fixture_id == 2


def test_focus_is_deterministic_regardless_of_input_order():
    """同一批比赛打乱输入顺序，焦点战必须完全一致。

    若选取依赖 dict 遍历顺序或随机打分，同一天两次推送可能选出不同的三场，
    用户会以为推送在乱跳。
    """
    preds = [_mk(i, 39 + (i % 3), f"H{i}", f"A{i}", 10 + i) for i in range(1, 9)]
    first = [p.fixture_id for p in _pick_focus(preds)]
    import random
    shuffled = list(preds)
    random.Random(7).shuffle(shuffled)
    second = [p.fixture_id for p in _pick_focus(shuffled)]
    assert first == second


def test_focus_and_rest_do_not_overlap():
    """「剩余 N 场」必须等于总数减去焦点战数，不能重复计数或漏计。"""
    preds = [_mk(i, 39, f"H{i}", f"A{i}", 10 + i) for i in range(1, 9)]
    text = _overview(preds)
    assert f"剩余 <b>{len(preds) - FOCUS_LIMIT}</b> 场" in text


# ---- 正文排版 --------------------------------------------------------------

def test_overview_has_probability_triple():
    """每场焦点战都要有「主—平—客」三连概率，这是总览的核心信息。"""
    text = _overview([_mk(1, 39, "Arsenal", "Chelsea", 30)])
    assert "📊 <code>55% — 25% — 20%</code>" in text


def test_overview_has_kickoff_and_verdict():
    """时间 + 结论 + 概率都要在同一场的信息块里。"""
    text = _overview([_mk(1, 39, "Arsenal", "Chelsea", 30, probs=(0.6, 0.2, 0.2))])
    assert "🕐" in text
    assert "主胜" in text
    assert "60%" in text


def test_overview_folds_remaining_matches():
    """其余场次只给联赛+场数概览，不逐场列出。

    场次必须多于 FOCUS_LIMIT 才会有「其他场次」——正好 3 场时全部是焦点战，
    本用例特意用 5 场来覆盖折叠分支。
    """
    preds = [_mk(i, 39 + i, f"H{i}", f"A{i}", 10 + i) for i in range(1, 6)]
    text = _overview(preds)
    assert "其他场次" in text
    assert "点下方按钮查看完整赛程" in text
    # 其余场次里不应出现概率三连（只有焦点战有）
    assert text.count("📊 <code>") == FOCUS_LIMIT


def test_overview_keeps_disclaimer():
    """免责声明必须保留：推送是面向投注场景的，缺了会有合规风险。"""
    assert DISCLAIMER in _overview([_mk(1, 39, "Arsenal", "Chelsea", 30)])


def test_overview_empty_falls_back_to_readable_empty_state():
    """无比赛时给出可读说明，绝不返回空白消息。"""
    text = _overview([], note="国际比赛日")
    assert "暂无比赛" in text
    assert "国际比赛日" in text


def test_overview_shows_league_name_of_focus():
    """焦点战上方要有联赛名，否则不知道是哪国联赛。

    用短名（英超）而非全称（英格兰超级联赛）：推送首屏宽度紧张，全称会把
    标题撑长，而国别恰恰是唯一需要一眼看到的信息。
    """
    text = _overview([_mk(1, 39, "Arsenal", "Chelsea", 30)])
    assert LEAGUE_SHORT[39] in text


def test_overview_note_is_escaped():
    """备注是外部传入的，必须转义，否则一个 & 就能让整条推送 400 失败。"""
    text = _overview([_mk(1, 39, "Arsenal", "Chelsea", 30)], note="A & B <c>")
    assert "A &amp; B" in text
    assert "<c>" not in text


# ---- 按钮 ------------------------------------------------------------------

def test_focus_entries_returns_id_and_label():
    """每个焦点战都要给出可点的 fixture_id 和按钮文案。"""
    preds = [_mk(101, 39, "Arsenal", "Chelsea", 30), _mk(102, 140, "Real", "Barca", 35)]
    entries = DigestView.focus_entries(preds)
    assert [fid for fid, _ in entries] == [101, 102]
    assert all(label.strip() for _, label in entries)


def test_button_label_within_mobile_width():
    """按钮文案必须在一行内：超长队名要被截断，不能把按钮撑成两行。"""
    p = _mk(1, 39, "Borussia Monchengladbach", "Bayern Munchen", 30)
    _, label = DigestView.focus_entries([p])[0]
    # 允许 emoji + 空格的额外宽度
    assert display_width(label) <= BUTTON_MAX_COLS + 3


def test_button_label_keeps_short_team_names():
    """短队名不能被截断逻辑误伤。

    队名会先本地化成中文短名（Arsenal → 阿森纳），断言因此用中文：这也顺带
    守住「按钮上必须是中文短名」——若哪天退化成英文全称，按钮会被撑爆。
    """
    p = _mk(1, 39, "Arsenal", "Chelsea", 30)
    _, label = DigestView.focus_entries([p])[0]
    assert "阿森纳" in label and "切尔西" in label


def test_focus_entries_skips_missing_fixture_id():
    """拿不到 id 的场次跳过，而不是塞一个假 id 让用户点到不存在的比赛。"""
    p = _mk(1, 39, "Arsenal", "Chelsea", 30)
    p.fixture_id = None
    assert DigestView.focus_entries([p]) == []


def test_digest_keyboard_callback_data_within_limit():
    """callback_data 超过 64 字节整个按钮会失效，这是 Telegram 硬限制。"""
    preds = [_mk(10 ** 9, 39, "Arsenal", "Chelsea", 30)]
    markup = Keyboards.digest_keyboard(DigestView.focus_entries(preds))
    for row in markup.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode()) <= CALLBACK_MAX_BYTES


def test_digest_keyboard_has_focus_and_exits():
    """键盘 = 焦点战按钮 + 底部出口行 + 主菜单，不多不少。"""
    preds = [_mk(i, 39, f"H{i}", f"A{i}", 10 + i) for i in range(1, 6)]
    markup = Keyboards.digest_keyboard(DigestView.focus_entries(preds))
    rows = markup.inline_keyboard
    # 焦点战 3 行 + 底部出口 1 行 + 主菜单 1 行
    assert len(rows) == FOCUS_LIMIT + 2
    # 底部出口行：完整赛程 + 准确率统计
    exit_labels = [b.text for b in rows[-2]]
    assert "完整赛程" in exit_labels[0]
    assert "准确率统计" in exit_labels[1]
    assert "主菜单" in rows[-1][0].text


def test_digest_keyboard_exits_use_existing_routes():
    """出口按钮必须复用已有 callback，不新增路由（避免改一处连带改 handler）。"""
    markup = Keyboards.digest_keyboard([(1, "⚽ A vs B")])
    data = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert "menu:fixtures" in data
    assert "menu:stats" in data
    assert "menu:home" in data


def test_digest_keyboard_empty_focus_still_has_exits():
    """没有焦点战（如全部场次无 id）时仍要有出口，不能给出空键盘。"""
    markup = Keyboards.digest_keyboard([])
    rows = markup.inline_keyboard
    assert len(rows) == 2  # 出口行 + 主菜单
    assert "menu:fixtures" in [b.callback_data for b in rows[0]]
