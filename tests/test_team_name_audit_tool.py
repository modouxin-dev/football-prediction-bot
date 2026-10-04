"""队名体检脚本自检 / self-check for tools/team_name_audit.py.

把体检脚本接进 CI，否则它跑不跑全靠人想起来——而收录表是持续增长的，
漏一次就可能把张冠李戴的键带上线。这里只断言「没有 ERROR 级问题」，
WARN 需要人工判断，不当门禁。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "team_name_audit.py"


def _load():
    spec = importlib.util.spec_from_file_location("team_name_audit", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["team_name_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_audit_tool_reports_no_error():
    """静态自检不得有 ERROR 级问题（WARN 需人工判断，不算失败）。"""
    mod = _load()
    rep = mod.Report()
    for fn in (
        mod.check_self_hit,
        mod.check_collision,
        mod.check_dup_key,
        mod.check_dup_cn,
        mod.check_width,
        mod.check_hygiene,
        mod.check_unstable_key,
    ):
        fn(rep)
    errors = rep.of(mod.ERROR)
    assert errors == [], "队名体检发现 ERROR 级问题：\n" + "\n".join(
        f"  · {e['check']}: {e['message']}" for e in errors
    )


def test_audit_tool_collision_is_isolated():
    """撞车键必须已被隔离出索引，否则就是张冠李戴。"""
    mod = _load()
    rep = mod.Report()
    mod.check_collision(rep)
    for row in rep.of(mod.WARN) + rep.of(mod.ERROR):
        if row["check"] == "collision":
            assert row.get("isolated") is True, (
                f"撞车键 {row.get('key')!r} 未隔离：{row['message']}"
            )


def test_audit_tool_is_importable_without_api_key():
    """脚本不依赖 API Key 也能做静态自检（--fetch 才需要）。"""
    mod = _load()
    assert hasattr(mod, "check_coverage")
    assert mod.ERROR == "ERROR"
