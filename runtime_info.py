"""进程运行时信息：部署平台、进程启动时刻、已运行时长。

为什么单独成模块而不是各写一份：
    「跑的是哪个 commit / 什么时候起来的」这个问题，Web 看板（HTTP）
    与机器人（Telegram）两侧都要回答。写在两处必然漂移——比如一侧加了
    平台识别、另一侧忘了。更关键的是 Fly.io 那个实例**只跑 main.py、
    不监听端口**（见 fly.toml 的 [processes]），根本没有 HTTP 端点可查，
    只能靠 Telegram 的 /status 看见它。所以两侧必须同源。

为什么需要「启动时刻」而不只是版本号：
    版本号（RAILWAY_GIT_COMMIT_SHA）只能区分「代码是不是新的」，区分不了
    「容器有没有真的重启」。同一个 commit 重新部署、或旧容器没被替换时，
    版本号一模一样，看上去「已更新」，实际跑的还是老进程。启动时刻能
    直接戳穿这种假象。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

# 进程内单调时钟：不受系统时间调整（NTP 校时、手动改表）影响，
# 用来算「已运行多久」比 wall clock 差更可靠。
_STARTED_MONOTONIC = time.monotonic()
# 启动时刻（UTC）：导入本模块即视为进程启动完成后的首个可记录点。
_STARTED_AT = datetime.now(timezone.utc)


def platform() -> str:
    """识别当前部署平台，用于在多平台并存时区分实例来源。

    识别依据是各平台**注入到容器环境**的变量，都是文档化的稳定变量：
      - Railway: RAILWAY_ENVIRONMENT / RAILWAY_PROJECT_ID / RAILWAY_SERVICE_ID
      - Fly.io : FLY_APP_NAME / FLY_MACHINE_ID
      - Render : RENDER / RENDER_SERVICE_NAME

    未命中任何已知平台时返回 "local"，而不是抛错——平台识别只是辅助信息，
    识别不出来不能让 /health 或 /status 整体失败。
    """
    if any(os.getenv(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID",
                                  "RAILWAY_SERVICE_ID")):
        return "railway"
    if any(os.getenv(k) for k in ("FLY_APP_NAME", "FLY_MACHINE_ID")):
        return "fly"
    if any(os.getenv(k) for k in ("RENDER", "RENDER_SERVICE_NAME")):
        return "render"
    return "local"


def started_at() -> datetime:
    """进程启动时刻（UTC，带时区）。"""
    return _STARTED_AT


def uptime_seconds() -> float:
    """已运行秒数（单调时钟差，不受改表影响）。"""
    return max(0.0, time.monotonic() - _STARTED_MONOTONIC)


def uptime_human(seconds: float | None = None) -> str:
    """把秒数写成人类可读的简短形式。

    只保留两级精度：运行时长是用来快速判断「刚重启 / 跑了一阵 / 跑了很久」，
    精确到秒只在刚启动那几分钟有意义，之后是噪音。
    """
    total = int(uptime_seconds() if seconds is None else seconds)
    if total < 60:
        return f"{total}秒"
    minutes, _sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    days, hours = divmod(hours, 24)
    return f"{days}天{hours}小时" if hours else f"{days}天"
