"""Telegram Webhook 网关：只做指令校验与任务派发。

安全边界（不越界）：
- 只接受白名单 chat_id，其它静默丢弃。
- 只接受白名单命令，命令映射为固定任务名，不接受用户提供的路径/仓库/脚本。
- 不回传源码、Token、环境变量、构建机路径或原始错误日志。
- 本模块不执行任何 shell、不做任何文件外传。
"""

from __future__ import annotations

import os
from typing import Any

# 允许的 chat_id 必须由环境变量提供，缺失时拒绝全部请求（fail-closed）
def _allowed_chat_ids() -> set[int]:
    raw = os.environ.get("TG_ALLOWED_CHAT_IDS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            # 非法条目直接忽略，不因配置错误而放开权限
            continue
    return ids


# 命令 -> 固定任务名。写死在代码里，不受用户输入影响。
COMMANDS: dict[str, str] = {
    "/test": "run_tests",
    "/clone-approved": "clone_approved_repo",
    "/deploy-staging": "deploy_staging",
}

# 允许出现在回执里的字段（其余一律不回传）
REPORT_FIELDS = ("status", "commit", "tests", "secret_scan", "pr_url", "task")

# 一次性任务队列：进程内字典即可，容器销毁即清空
_QUEUE: list[dict[str, Any]] = []


def enqueue_fixed_task(task_name: str, chat_id: int) -> str:
    """把固定任务名入队，返回 job_id。不接受任意命令或参数。"""
    import uuid

    job_id = str(uuid.uuid4())
    _QUEUE.append({"job_id": job_id, "task": task_name, "chat_id": chat_id})
    return job_id


def _sanitize_report(report: dict[str, Any]) -> dict[str, Any]:
    """只保留白名单字段，防止把源码/Token/路径带出去。"""
    return {k: v for k, v in report.items() if k in REPORT_FIELDS}


def handle_update(update: dict[str, Any]) -> dict[str, Any]:
    """处理一条 Telegram update。任何不合法输入都返回 ok=True 的空响应。"""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    allowed = _allowed_chat_ids()
    if not allowed or chat_id not in allowed:
        # 非授权用户：静默丢弃，不回执、不报错、不泄露存在性
        return {"ok": True}

    # 严格整串匹配：命令必须与白名单完全一致，不接受任何参数，
    # 杜绝 "/test --repo=..." 这类注入被误判为合法命令。
    task_name = COMMANDS.get(text)
    if task_name is None:
        # 非白名单命令或带参数的命令：静默丢弃
        return {"ok": True}

    job_id = enqueue_fixed_task(task_name, chat_id)
    return {"ok": True, "job_id": job_id}


def drain_queue() -> list[dict[str, Any]]:
    """取出并清空待执行任务（供 worker 拉取）。"""
    items = list(_QUEUE)
    _QUEUE.clear()
    return items
