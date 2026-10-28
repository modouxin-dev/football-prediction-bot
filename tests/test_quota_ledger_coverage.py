"""本地额度账本覆盖率回归测试。

锁住两条真实缺陷（均在生产 /status 数据中暴露）：

1. 看板进程（api.py 的实时客户端）发出的请求完全不进本地账本 ——
   这是「数据源侧 3360 / 本地实测 280」对不上的机制之一。
   断言的是「是否接了 quota_sink」，不是文案。

2. ``_quota_day()`` 的结算时区不可配置，与数据源实际重置时刻对不上时
   无法对齐。断言 QUOTA_TZ 能生效，且默认行为不变。
"""

from __future__ import annotations

import ast
import os
import re
from datetime import timezone, timedelta

import pytest


# ---- 缺陷 1：看板客户端必须接 quota_sink -------------------------------

def _dashboard_router_calls() -> list[ast.Call]:
    """取出 _live_client() 内所有 DataSourceRouter(...) 调用。

    必须用 AST 而非字符串匹配：源码注释里也会出现 quota_sink 字样，
    按子串搜索会导致「撤掉修复测试仍然通过」的假绿。
    """
    tree = ast.parse(_read("api.py"))
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_live_client"),
        None,
    )
    assert fn is not None, "未找到 _live_client()，函数名是否已改？"
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _call_name(n) == "DataSourceRouter"
    ]


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def test_dashboard_live_client_passes_quota_sink():
    """看板进程同样会发真实请求，必须计入本地账本。

    改动前 api.py 里是：
        DataSourceRouter(FootballAPI(...))            # 无 quota_sink
    看板发出的请求一次都没被记账。
    """
    calls = _dashboard_router_calls()
    assert calls, "_live_client() 内没有 DataSourceRouter 调用"
    assert any(
        kw.arg == "quota_sink" for c in calls for kw in c.keywords
    ), (
        "看板实时客户端未接 quota_sink：看板发出的请求不进本地额度账本，"
        "导致本地计数系统性低于数据源侧"
    )


def test_dashboard_quota_sink_failure_is_non_fatal():
    """计数不可用不能让看板 500 —— 必须有降级分支。"""
    tree = ast.parse(_read("api.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_live_client"
    )
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn)), (
        "缺少降级分支：bump_quota 导入失败会导致看板整页 500"
    )


# ---- 缺陷 2：额度结算时区可对齐 ---------------------------------------

def test_quota_day_default_unchanged(monkeypatch):
    """未设 QUOTA_TZ 时，行为与改动前完全一致（Asia/Shanghai）。"""
    import repository

    monkeypatch.delenv("QUOTA_TZ", raising=False)
    monkeypatch.setenv("TIMEZONE", "Asia/Shanghai")
    assert repository._quota_day() == _now_in("Asia/Shanghai")


def test_quota_day_honours_quota_tz(monkeypatch):
    """QUOTA_TZ 应能覆盖 TIMEZONE，用于对齐数据源真实重置时刻。"""
    import repository

    monkeypatch.setenv("TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("QUOTA_TZ", "UTC")
    assert repository._quota_day() == _now_in("UTC")


def test_quota_tz_precedence_over_timezone(monkeypatch):
    """两个变量同时存在时 QUOTA_TZ 优先（额度结算与展示时区解耦）。"""
    import repository

    monkeypatch.setenv("TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("QUOTA_TZ", "America/New_York")
    assert repository._quota_day() == _now_in("America/New_York")


def test_quota_day_falls_back_when_tzdata_missing(monkeypatch):
    """容器缺 tzdata 时不能抛异常，退化为固定 +8。"""
    import repository

    monkeypatch.setenv("QUOTA_TZ", "Not/AZone")
    day = repository._quota_day()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day), f"退化分支未生效：{day!r}"


# ---- 辅助 -------------------------------------------------------------

def _read(name: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, name), encoding="utf-8") as fh:
        return fh.read()


def _now_in(tzname: str) -> str:
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d")
