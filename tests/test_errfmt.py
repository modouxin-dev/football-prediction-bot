"""errfmt.fmt_exc —— 日志必须带异常消息。

存在理由 / Why
--------------
仓库里曾大量写 ``log.warning("...%s", type(exc).__name__)``，
日志只留 ``AttributeError`` 而没有消息。

后果是真实的：``/oddsprobe`` 报
``AttributeError: 'str' object has no attribute 'isoformat'``，
而日志只显示 ``AttributeError``——看不出对象与属性，
一个本该一眼定位的问题多花了十几轮往返。

只打类型名 = 把最有用的半句话丢掉。这里锁住替代方案。
"""
from __future__ import annotations

from errfmt import fmt_exc


def test_includes_message_not_just_type():
    """核心断言：必须带消息。"""
    exc = AttributeError("'str' object has no attribute 'isoformat'")
    out = fmt_exc(exc)
    assert "AttributeError" in out
    assert "isoformat" in out, "消息丢了——这正是要修的那个问题"


def test_empty_message_falls_back_to_type_only():
    """消息为空时不留悬空冒号。"""
    assert fmt_exc(ValueError()) == "ValueError"
    assert not fmt_exc(ValueError()).endswith(": ")


def test_truncates_very_long_message():
    """超长消息要截断——整页 HTML 塞进 HTTPError 会淹没日志。"""
    exc = RuntimeError("x" * 5000)
    assert len(fmt_exc(exc)) <= 300


def test_respects_custom_limit():
    assert len(fmt_exc(RuntimeError("y" * 500), limit=50)) <= 50


def test_strips_surrounding_whitespace():
    assert fmt_exc(RuntimeError("  boom  ")) == "RuntimeError: boom"
