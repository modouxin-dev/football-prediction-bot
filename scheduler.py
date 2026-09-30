"""定时任务与主动推送 / Scheduler & push.

为什么独立成模块：
    推送、结算、赛程同步是「后台任务」，与 Telegram 消息处理正交。
    放在同一个文件里会让入口既管命令又管定时，任何一处改动都可能牵连另一处。

⚠️ 关于调度器选型（重要）：
    本项目**不引入 apscheduler**。python-telegram-bot 自带 `JobQueue`，
    已经用 `run_daily` / `run_repeating` 实现了定时推送、赛程同步、赛果结算。
    若再引入 apscheduler，会出现两个互不知晓的调度器同时运行，
    最直接后果是**同一场比赛被推送两次**，且两者都持有独立的事件循环，
    排查困难。这里做的是「把已有 JobQueue 逻辑抽出来」，而非「换一个调度器」。

推送通道（NotificationManager）：
    把「谁负责发」封装成一个对象，让定时任务不依赖 Application 的具体形态，
    从而可以被 Web / 测试 / 未来的其他通道复用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram.ext import ContextTypes

from bot_handler import BotUI
from config import Settings
from service import PredictionService
from tghtml import normalize
from support import describe_error, notify_admins

log = logging.getLogger("bot")
ui = BotUI()

# /test 放宽窗口：近期无比赛（如国际比赛日）时放宽到 14 天，方便看到示例消息。
# 必须与 main.py 迁移前的取值一致，否则 /test 的行为会静默改变。
WIDE_HOURS = 24 * 14

DAILY_JOB = "daily_push"
SETTLE_JOB = "settle_results"
SYNC_JOB = "sync_matches"


@dataclass
class PushResult:
    sent: int
    note: str | None = None


class NotificationManager:
    """主动推送通道。

    当前实现走 Telegram Bot API；将来接 Web 推送、邮件或队列时，
    只需替换这里的 send，定时任务与命令层代码不用改。
    """

    def __init__(self, app) -> None:
        self.app = app

    @property
    def settings(self) -> Settings:
        return self.app.bot_data["settings"]

    async def send_prediction(self, prediction) -> None:
        settings = self.settings
        await self.app.bot.send_message(
            chat_id=settings.chat_target,
            # 推送是唯一「无人值守」的出口：一条畸形标签会让整条推送 400 失败
            # 且没人会发现，因此这里必须过规范化层。
            text=normalize(ui.format_prediction(prediction, settings.timezone)),
            parse_mode="HTML",
            reply_markup=ui.get_main_keyboard(prediction.fixture_id, "home"),
        )

    async def notify_admins(self, text: str) -> None:
        await notify_admins(self.app, text)


async def run_push(app, *, widen: bool = False) -> PushResult:
    """生成并推送预测。/test 与每日定时任务走的是同一条路径。"""
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    if settings.chat_target is None:
        raise RuntimeError("未设置 CHAT_ID，无法推送")

    predictions = await service.build_predictions()
    note = service.last_note
    if not predictions and widen:
        predictions = await service.build_predictions(lookahead_hours=WIDE_HOURS)
        if predictions:
            note = (f"未来 {settings.lookahead_hours} 小时内没有未开赛的比赛，"
                    f"已放宽到 {WIDE_HOURS // 24} 天内用于测试。")
        elif service.last_note:
            note = service.last_note  # 降级/空结果的真实原因，必须让用户看到

    sender = NotificationManager(app)
    for p in predictions:
        await sender.send_prediction(p)
    return PushResult(len(predictions), note)


async def daily_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    """每日定时推送：失败要让管理员知道，绝不能静默。"""
    app = context.application
    try:
        result = await run_push(app)
        log.info("每日推送完成，共 %d 场", result.sent)
    except Exception as exc:  # 定时任务里的任何失败都要让管理员知道
        log.exception("每日推送失败")
        await notify_admins(app, f"❌ 每日推送失败：{describe_error(exc)}")


async def settle_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """定时结算：把已完场比赛的真实比分回写，供命中率统计。"""
    app = context.application
    service: PredictionService = app.bot_data["service"]
    try:
        done = await service.sync_results()
        if done:
            log.info("定时结算完成：%d 场", done)
    except Exception as exc:  # 结算失败绝不能影响主流程
        log.warning("定时结算失败：%s", exc)


async def sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """定时同步：把赛程拉到本地 SQLite（查询优先读本地库，省 API 额度）。"""
    app = context.application
    service: PredictionService = app.bot_data["service"]
    try:
        up = await service.sync.sync_upcoming()
        recent = await service.sync.sync_recent()
        log.info(
            "赛程同步完成：未来 %d 场 / 最近 %d 场，本地库共 %d 场",
            up.get("saved", 0), recent.get("saved", 0), service.repo.matches_count(),
        )
    except Exception as exc:  # 同步失败不能影响主流程
        log.warning("赛程同步失败：%s", exc)


def setup_scheduler(app, settings: Settings) -> int:
    """注册全部定时任务。返回注册的任务数。

    注意：这里用的是 PTB 自带的 JobQueue，与 run_polling 共享同一个事件循环，
    不需要也不应该再起第二个调度器。
    """
    from sync import SYNC_INTERVAL_HOURS

    if app.job_queue is None:
        log.warning("JobQueue 未启用（缺少 PTB 的 job-queue 扩展），定时任务不会运行")
        return 0

    # 赛程同步：定时把未来赛程与最近赛果拉到本地库，
    # 让 Telegram 命令只读 SQLite，不依赖外部接口
    app.job_queue.run_repeating(
        sync_job, interval=SYNC_INTERVAL_HOURS * 3600, first=10, name=SYNC_JOB,
    )
    registered = 1
    if settings.chat_id:
        app.job_queue.run_daily(daily_push, time=settings.push_time, name=DAILY_JOB)
        # 每 6 小时同步一次赛果，保证命中率统计能及时更新
        app.job_queue.run_repeating(
            settle_job, interval=6 * 3600, first=300, name=SETTLE_JOB,
        )
        registered += 2
    else:
        log.warning("未设置 CHAT_ID：定时推送已停用（仍可使用命令）")
    return registered
