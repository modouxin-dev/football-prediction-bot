"""定时任务执行流水：把「job 跑没跑、成功没成功」变成可查的事实。

为什么需要这个模块
------------------
修复前，``scheduler.py`` 里 7 个定时任务的失败处理长这样：

    except Exception as exc:
        log.warning("回测失败（不影响主流程）：%s", exc)

后果是**失败完全不可观测**：

  * 机器人照常推送，用户以为一切正常；
  * 看板显示旧数据，用户以为是最新的；
  * 只有去翻 Railway 日志才知道某个 job 已经连续失败好几天。

「不影响主流程」这个设计本身是对的——一个 job 挂掉不该拖垮机器人。
错的是**只写日志**：日志要主动去翻，而数据可以被动呈现。

本模块把每次 job 的结果落进 ``job_runs`` 表，再由 ``/jobs`` 端点暴露，
让「回测到底跑没跑」变成看板上一行字，而不是一次日志考古。

设计约束（重要）
----------------
**记录失败绝不能反过来影响业务。** 本模块所有函数都吞掉自己的异常——
如果落盘失败会让 job 挂掉，那就把「观测手段」变成了「新的故障源」，
比不观测更糟。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# 状态取值。用字面量而非枚举：这张表会被 SQL 直接查，
# 字符串在日志和看板里都更好读。
OK = "ok"
FAIL = "fail"
SKIP = "skip"


def _conn(db_path: str | None) -> sqlite3.Connection | None:
    """打开任务流水库。返回 None 表示不可用，调用方静默跳过。"""
    if not db_path or db_path == ":memory:":
        # 内存库在进程重启后必然为空，记了也没意义，还会在测试里制造噪声
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        # 必须设 row_factory：不设的话查询返回纯 tuple，dict(r) 会抛
        # "cannot convert dictionary update sequence element"（实测复现）。
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS job_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, "
            "status TEXT NOT NULL, message TEXT, duration_ms INTEGER, "
            "created_at TEXT NOT NULL)"
        )
        return conn
    except Exception as exc:
        log.warning("job_runs 打开失败：%s", exc)
        return None


def record(
    db_path: str | None,
    job: str,
    status: str,
    message: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """记录一次任务执行结果。

    Args:
        db_path: SQLite 路径；None 或 ":memory:" 时静默跳过。
        job: 任务名，与 scheduler.py 中的常量一致（如 BACKTEST_JOB）。
        status: "ok" / "fail" / "skip"。
        message: 失败或跳过的原因；成功时传 None。
        duration_ms: 耗时毫秒，用于发现「跑得越来越慢」这类隐性劣化。

    Returns:
        None。本函数绝不抛异常——观测手段不能成为新的故障源。
    """
    try:
        conn = _conn(db_path)
        if conn is None:
            return
        with conn:
            conn.execute(
                "INSERT INTO job_runs (job, status, message, duration_ms, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    str(job),
                    str(status),
                    (message or None),
                    duration_ms,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        conn.close()
    except Exception as exc:
        # 落盘失败也绝不能让调用方的 job 挂掉
        log.warning("job_runs 写入失败（不影响任务本身）：%s", exc)


def ok(db_path: str | None, job: str, duration_ms: int | None = None) -> None:
    """记录成功。"""
    record(db_path, job, OK, None, duration_ms)


def fail(
    db_path: str | None,
    job: str,
    exc: BaseException | str,
    duration_ms: int | None = None,
) -> None:
    """记录失败。exc 可以是异常对象或字符串。

    只存消息不存堆栈：看板上一行要能读完，堆栈留在日志里。
    """
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    # 超长消息截断：一条 API 报错可能几千字符，会顶掉整个看板表格
    if len(msg) > 200:
        msg = msg[:200] + "…"
    record(db_path, job, FAIL, msg, duration_ms)


def skip(db_path: str | None, job: str, reason: str) -> None:
    """记录跳过（如「无样本」「未启用」）——区别于失败，不是故障。"""
    record(db_path, job, SKIP, reason)


def recent(
    db_path: str | None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """最近的任务执行记录（新→旧）。db_path 无效时返回空列表。"""
    try:
        conn = _conn(db_path)
        if conn is None:
            return []
        rows = list(conn.execute(
            "SELECT id, job, status, message, duration_ms, created_at "
            "FROM job_runs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ))
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("job_runs 读取失败：%s", exc)
        return []


def last_status(
    db_path: str | None,
) -> dict[str, dict[str, Any]]:
    """每个任务最近一次执行结果，按任务名索引。

    返回 ``{job: {status, message, created_at, duration_ms}}``。
    看板用它回答「哪个 job 现在不健康」，而不用翻流水。
    """
    try:
        conn = _conn(db_path)
        if conn is None:
            return {}
        rows = list(conn.execute(
            "SELECT job, status, message, duration_ms, created_at, id "
            "FROM job_runs j WHERE id = ("
            "  SELECT MAX(id) FROM job_runs WHERE job = j.job)"
        ))
        conn.close()
        return {r["job"]: dict(r) for r in rows}
    except Exception as exc:
        log.warning("job_runs 汇总失败：%s", exc)
        return {}
