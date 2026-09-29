"""网关安全边界测试。

锁死的行为：
- 未配置 chat_id 白名单时拒绝全部请求（fail-closed）。
- 非白名单 chat_id / 非白名单命令一律静默丢弃。
- 命令参数注入（如 "/test; rm -rf /"）不得绕过白名单。
- 回执只保留白名单字段，源码/Token/路径不得带出。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[1] / "agent-terminal" / "gateway"
sys.path.insert(0, str(GATEWAY))


@pytest.fixture()
def app(monkeypatch):
    """每个用例重新加载模块，并清空队列。"""
    monkeypatch.setenv("TG_ALLOWED_CHAT_IDS", "123456789")
    for mod in list(sys.modules):
        if mod == "app":
            del sys.modules[mod]
    module = importlib.import_module("app")
    module._QUEUE.clear()
    return module


def _msg(chat_id: int, text: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def test_whitelist_chat_and_command_enqueues(app):
    result = app.handle_update(_msg(123456789, "/test"))
    assert result["ok"] is True
    assert "job_id" in result
    assert app.drain_queue()[0]["task"] == "run_tests"


def test_unknown_chat_is_dropped(app):
    result = app.handle_update(_msg(999999999, "/test"))
    assert result == {"ok": True}
    assert app.drain_queue() == []


def test_unknown_command_is_dropped(app):
    result = app.handle_update(_msg(123456789, "/deploy-prod"))
    assert result == {"ok": True}
    assert app.drain_queue() == []


def test_arguments_cannot_override_command(app):
    """命令必须整串精确匹配，任何附加参数一律拒绝。"""
    for text in (
        "/test; rm -rf /",
        "/test --repo=evil/repo",
        "/test extra args",
        "/test\n/deploy-staging",
    ):
        result = app.handle_update(_msg(123456789, text))
        assert result == {"ok": True}, f"{text} 不应被放行"
    assert app.drain_queue() == []


def test_missing_allowlist_denies_everything(monkeypatch):
    """未配置白名单时必须拒绝全部，不能因配置缺失而放开。"""
    monkeypatch.delenv("TG_ALLOWED_CHAT_IDS", raising=False)
    for mod in list(sys.modules):
        if mod == "app":
            del sys.modules[mod]
    module = importlib.import_module("app")
    result = module.handle_update(_msg(123456789, "/test"))
    assert result == {"ok": True}
    assert module.drain_queue() == []


def test_malformed_update_is_ignored(app):
    for update in ({}, {"message": {}}, {"message": {"text": "/test"}}):
        assert app.handle_update(update) == {"ok": True}
    assert app.drain_queue() == []


def test_report_only_keeps_allowlisted_fields(app):
    report = {
        "status": "passed",
        "commit": "abc1234",
        "source_code": "print('leak')",
        "GH_TOKEN": "ghp_secret",
        "build_path": "/workspace/source",
    }
    clean = app._sanitize_report(report)
    assert set(clean) == {"status", "commit"}
    assert "source_code" not in clean
    assert "GH_TOKEN" not in clean
    assert "build_path" not in clean


def test_all_commands_map_to_fixed_tasks(app):
    assert app.COMMANDS == {
        "/test": "run_tests",
        "/clone-approved": "clone_approved_repo",
        "/deploy-staging": "deploy_staging",
    }
