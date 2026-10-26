"""/oddsprobe 自动选场：不得传字符串给按日取赛程接口。

存在理由 / Why
--------------
``/oddsprobe`` 曾报::

    AttributeError: 'str' object has no attribute 'isoformat'

根因是这一行::

    day = (today + timedelta(days=off)).isoformat()   # 生成 str
    fx = await api.get_fixtures_by_date(day)          # 接口内部再调 day.isoformat()

接口入口后来加了 ``_as_date`` 归一化（能兜住），但**源头不该产生字符串**——
这里锁住源头：传给接口的一定是 ``date`` 对象。

同时锁住额度保护：``/odds`` 是付费端点，无上限逐场试会快速烧掉日配额。
"""
from __future__ import annotations

import ast
import asyncio
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _oddsprobe_source() -> str:
    tree = ast.parse((ROOT / "commands" / "admin.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "oddsprobe_cmd":
            return ast.get_source_segment(
                (ROOT / "commands" / "admin.py").read_text(encoding="utf-8"), node)
    raise AssertionError("找不到 oddsprobe_cmd")


def test_autopick_passes_date_object_not_isoformat_string():
    """核心断言：不得对 date 调 .isoformat() 后再传给按日接口。

    ⚠️ 为什么走 AST 的 Call 节点而不是字符串搜索：
    第一次写这个测试时用了 ``assert "...isoformat()" not in src``，
    结果**命中了函数自己的注释**（注释里引用了那行错误写法），
    于是「已修复」的代码反而过不了测试——这是假阳性。

    字符串搜索分不清代码与注释；AST 只认真正的调用节点。
    """
    src = _oddsprobe_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "isoformat"):
            # 允许在 _build_version 之类不相关的调用里出现；
            # 这里要禁的是「把 timedelta 的结果转字符串」
            seg = ast.get_source_segment(src, node) or ""
            if "timedelta" in seg:
                pytest.fail(f"自动选场把 date 转成了字符串：{seg}")


def test_autopick_calls_get_fixtures_by_date_with_date_object():
    """传给 get_fixtures_by_date 的实参不得是 .isoformat() 的结果。"""
    src = _oddsprobe_source()
    for line in src.splitlines():
        if "get_fixtures_by_date" in line and "def " not in line:
            assert ".isoformat()" not in line, (
                f"传给按日接口的是字符串：{line.strip()}"
            )


def test_probe_tries_are_capped():
    """/odds 是付费端点，探测场次必须有上限。"""
    src = _oddsprobe_source()
    assert "ODDS_PROBE_MAX_TRIES" in src, "缺少探测次数上限"


def test_error_output_includes_build_version():
    """报错要带构建版本——否则无法区分「代码错」与「生产跑旧构建」。

    本次排查中，修复已合入 main 但生产仍在旧构建，
    若报错自带版本，第一轮就能分辨，不必反复确认部署状态。
    """
    src = _oddsprobe_source()
    assert "_build_version()" in src


# ---- 出口必须分片：实测 Message is too long -------------------------------
# 生产日志（2026-10-26 07:54）:
#   File "/app/commands/admin.py", line 1431, in oddsprobe_cmd
#   telegram.error.BadRequest: Message is too long
# 一场比赛的盘口全列出来有几十个 bet，拼成单条远超 Telegram 4096。
# 异常发生在最后一步——前面取数、解析全部白做，用户收不到任何回复。


def test_output_uses_reply_html_not_bare_reply_text():
    """oddsprobe 出口必须走 reply_html（内含 split_html_blocks 分片）。

    ⚠️ 同样必须用 AST 而不是字符串搜索——第一版用
    ``assert "reply_text" not in src`` 又命中了函数自己的注释
    （注释里写了「不能直接 reply_text」），已修复的代码过不了测试。
    这是本文件里第二次踩同一个坑，故明确：判代码一律走 AST Call 节点。
    """
    src = _oddsprobe_source()
    tree = ast.parse(src)

    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)

    assert "reply_html" in called, "出口没走 reply_html，长盘口会 400"
    assert "reply_text" not in called, (
        "仍有裸 reply_text 调用——单条超 4096 会抛 Message is too long，"
        "且异常发生在最后一步，前面取数全白做"
    )


def test_split_html_blocks_actually_splits_long_odds_output():
    """分片工具对超长盘口文本必须真的切成多块。

    光断言「调用了 reply_html」不够——如果分片函数对这种文本返回一块，
    照样 400。这里构造真实的超长盘口文本验证。
    """
    from bot_handler import split_html_blocks

    lines = ["💱 盘口探测", "探测对象　fixture 1570431", ""]
    for i in range(80):  # 80 个 bet：一场比赛的实际盘口数量级
        lines.append(f"id={i} · Bet Name {i}　（3 项）")
        lines.append("　" + " / ".join(f"Home {i}={1.9 + i / 100:.2f}" for _ in range(3)))
    text = "\n".join(lines)

    assert len(text) > 4096, f"构造的文本只有 {len(text)} 字符，测不出问题"
    blocks = split_html_blocks(text)
    assert len(blocks) > 1, "分片函数没切开——长文本仍会撞 4096"
    for b in blocks:
        assert len(b) <= 4096, f"分出来仍有 {len(b)} 字符的超长块"
