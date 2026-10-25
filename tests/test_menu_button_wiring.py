"""内联菜单按钮必须有真实分支，不能落进「功能正在开发中」。

回归背景（实测，非推测）：看板/汇总消息底部的「📊 准确率统计」按钮
（callback `menu:stats`）点击后显示「该功能正在开发中」，
而 `/stats` 指令本身早已实现并注册。

原因是按钮用的 key 不在 `MENU_ITEMS` 里，而既有的回归测试
（test_menu.py）只扫描 MENU_ITEMS —— 按钮集合与测试集合不是同一份，
漏网的按钮永远不会被测到。

因此这里改为**扫描源码里实际出现的 callback_data**，
而不是扫描一份手工维护的清单：清单可以不更新，源码不会。
"""
from __future__ import annotations

import inspect
import re

import pytest

import keyboards
import main


def _callback_keys() -> set[str]:
    """源码里实际写出的 `menu:xxx` 回调键。"""
    src = inspect.getsource(keyboards)
    return set(re.findall(r'callback_data=f?"menu:([a-z_]+)"', src)) | set(
        re.findall(r'callback_data="menu:([a-z_]+)"', src))


def test_every_menu_callback_has_branch():
    """每个 menu:* 回调都必须在 on_menu_key 里有分支。"""
    handler = inspect.getsource(main.on_menu_key)
    missing = [k for k in sorted(_callback_keys())
               if not re.search(rf'key\s*==\s*["\']{re.escape(k)}["\']', handler)]
    assert not missing, (
        f"这些菜单回调没有分支，点击会显示『功能正在开发中』：{missing}")


def test_stats_callback_does_not_fall_through_to_coming_soon():
    """/stats 已实现，其按钮不能再走 format_coming 兜底。"""
    from views.menu import MenuView

    handler = inspect.getsource(main.on_menu_key)
    assert re.search(r'key\s*==\s*["\']stats["\']', handler)
    # 兜底仍在，但 stats 必须在它之前被接住
    assert handler.index('key == "stats"') < handler.index("format_coming")


def test_dispatcher_available_for_admin_commands():
    """admin_only 指令走 dispatcher.execute() 才带权限拦截。

    按钮回调若直接调命令函数会绕过判权，因此 dispatcher 必须存进 bot_data。
    """
    from commands import build_dispatcher

    d = build_dispatcher()
    assert "stats" in d
    spec = d.get("stats")
    assert spec.admin_only, "/stats 是管理员指令，按钮调用必须经判权"
