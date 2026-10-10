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

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot_handler import BotUI
from keyboards import nav_row
from backtester_fixed import BacktesterFixed
from config import Settings
from service import PredictionService
from tghtml import normalize
from support import describe_error, notify_admins
from views.digest import DigestView

log = logging.getLogger("bot")
ui = BotUI()

# /test 放宽窗口：近期无比赛（如国际比赛日）时放宽到 14 天，方便看到示例消息。
# 必须与 main.py 迁移前的取值一致，否则 /test 的行为会静默改变。
WIDE_HOURS = 24 * 14

DAILY_JOB = "daily_push"
SETTLE_JOB = "settle_results"
SYNC_JOB = "sync_matches"
BACKTEST_JOB = "backtest_run"
HISTORY_JOB = "sync_history"

# 历史赛季回填与回测都是重活：前者要拉整季（数百场，吃 API 额度），
# 后者要逐场滚动预测（1100 场约数十秒）。默认每天各一次即可，
# 太频繁只会白烧额度，且结论不会变得更有用。
HISTORY_INTERVAL_HOURS = 24
BACKTEST_INTERVAL_HOURS = 24


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

    async def send_digest(self, predictions, *, note: str | None = None) -> int:
        """推送「按联赛分组的单条汇总」，返回实际发出的消息条数。

        五大联赛周末一天能有 30+ 场，逐场推送 = 30 条消息、30 次通知，会把
        聊天列表整个刷掉。汇总压成 1 条（超长时按联赛块自动分页，绝不从一场
        中间切断），细节留给用户点进单场看。

        无比赛时**不推送**：国际比赛日天天发「暂无比赛」是纯噪音，
        静默比打扰好。手动触发（按钮 / /test）的反馈由调用方负责。
        """
        if not predictions:
            return 0

        settings = self.settings
        pages = DigestView.format_daily_digest_pages(
            predictions, settings, settings.timezone, note=note,
        )
        # 汇总只给结论，细节在单场页；挂导航键让用户一键跳进去
        markup = InlineKeyboardMarkup([nav_row()])
        sent = 0
        for page in pages:
            await self.app.bot.send_message(
                chat_id=settings.chat_target,
                text=normalize(page),
                parse_mode="HTML",
                reply_markup=markup,
            )
            sent += 1
        return sent

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
    await sender.send_digest(predictions, note=note)
    # PushResult.sent 语义保持「推送了多少场比赛」；分页只是传输细节，
    # 不该让调用方（/test 回显、日志）看到消息条数和场数混在一起。
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


async def history_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """回填历史赛季（付费套餐解锁），让库里真正积累起赛果。

    只补赛程与赛果，不伪造预测记录——回填时用现在的积分榜去算过去的比赛
    会引入前视偏差，得出的命中率是假象。
    """
    app = context.application
    service: PredictionService = app.bot_data["service"]
    try:
        r = await service.sync.sync_history()
        log.info("历史赛季回填完成：赛季 %d 个，收到 %d 保存 %d",
                 r.get("seasons", 0), r.get("received", 0), r.get("saved", 0))
    except Exception as exc:  # 回填失败不能影响主流程
        log.warning("历史赛季回填失败：%s", exc)


async def backtest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """跑一次全量回测并把结论落盘。

    回测是"模型自我体检"：用滚动前进的方式对历史比赛逐场预测，
    再把准确率 / 对数损失 / 校准度存进 SQLite。
    结果必须落盘——只放内存的话，容器一重启就消失，
    也就无从回答"模型是在变好还是在变差"。
    """
    from datetime import datetime, timezone

    app = context.application
    service: PredictionService = app.bot_data["service"]
    try:
        bt = BacktesterFixed(
            league_id=service.settings.league_id,
            db_path=getattr(service.settings, "db_path", None),
        )
        result = await bt.backtest()
        run_id = service.repo.save_backtest_run(
            competition=bt.competition_code,
            variant=bt.variant,
            min_history=bt.min_history,
            result=result,
        )
        log.info(
            "回测完成并落盘(id=%s)：%d 场 准确率 %.2f%% log_loss %.4f "
            "（语料 db=%s csv=%s）",
            run_id, result["total_predictions"], result["accuracy"] * 100,
            result["log_loss"],
            (result.get("corpus") or {}).get("db"),
            (result.get("corpus") or {}).get("history"),
        )
        app.bot_data.setdefault("last_backtest_at",
                                datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as exc:  # 回测失败绝不能影响推送主流程
        log.warning("回测失败（不影响主流程）：%s", exc)


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
    # 历史回填与回测**不依赖 CHAT_ID**：它们是数据积累与模型体检，
    # 属于"程序自己该做的事"，不该因为没配推送群就整个停掉。
    # （此前的 settle_job 就被误放进 chat_id 分支，导致没配群号时
    #  赛果一条都不结算——那个问题单独修，这里先保证不再重演。）
    app.job_queue.run_repeating(
        history_job, interval=HISTORY_INTERVAL_HOURS * 3600, first=600,
        name=HISTORY_JOB,
    )
    app.job_queue.run_repeating(
        backtest_job, interval=BACKTEST_INTERVAL_HOURS * 3600, first=1800,
        name=BACKTEST_JOB,
    )
    registered = 3
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
