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

from backtester_fixed import BacktesterFixed
from bot_handler import BotUI
from config import Settings
from keyboards import nav_row
from service import PredictionService
from support import describe_error, notify_admins
from tghtml import normalize
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
AUTO_PREDICT_JOB = "auto_predict"
PREMATCH_JOB = "prematch_refresh"
REVIEW_JOB = "daily_review"

# 历史赛季回填与回测都是重活：前者要拉整季（数百场，吃 API 额度），
# 后者要逐场滚动预测（1100 场约数十秒）。默认每天各一次即可，
# 太频繁只会白烧额度，且结论不会变得更有用。
HISTORY_INTERVAL_HOURS = 24
BACKTEST_INTERVAL_HOURS = 24

# ── 自动预测与复盘 ────────────────────────────────────────────────────────
# 欧冠（2）默认纳入无人值守预测：它是关注度最高的赛事之一，
# 但用户手动点「预测」时只覆盖当前选中的单一联赛，欧冠因此长期没有预测记录，
# 看板上整片显示「未预测」。主联赛列表由 settings.league_ids 提供，
# 这里补的是可能不在 LEAGUE_IDS 里、但仍需无人值守兜底的额外联赛。
AUTO_PREDICT_EXTRA_LEAGUES: tuple[int, ...] = (2,)  # 2 = 欧洲冠军联赛

# 自动预测的向前窗口（天）。
# 必须与看板可切换的范围对齐：看板 days 支持 1/3/7，窗口只有 2 天时
# 切到「3 天」第三天必然整片未预测。取 3 天覆盖看板默认档位，
# 再远则赛程会变（改期、阵容未定、赔率未开），提前算好的概率是噪音。
AUTO_PREDICT_DAYS = 3

# 复盘回看窗口（天）。7 天能在样本量与时效性之间取得平衡：
# 太短（1 天）样本个位数，归因全是噪音；太长（30 天）会掩盖近期劣化。
REVIEW_WINDOW_DAYS = 7

# 自动预测执行间隔（小时）。6 小时一轮既能在赛前刷新概率，
# 又不至于频繁重算——同一场重复预测只会用更新的赔率覆盖旧值，
# 更密的频率换不来更准的赛前结论，只烧额度。
AUTO_PREDICT_INTERVAL_HOURS = 6


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


async def auto_predict_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """无人值守批量预测：把未来赛程的预测算好并落盘。

    为什么必须独立成任务：
        `build_predictions` 只在用户点按钮时跑，且只算当前选中的**一个**联赛。
        结果是看板上「欧冠 9 场」全部显示未预测——机器人压根没为欧冠算过。
        赛程页展示库里所有联赛，预测却只覆盖单联赛，两边永远对不上。

    落盘的意义（这才是「进步型机器人」的地基）：
        只有预测进了 predictions 表，赛果结算才会回写真实比分，
        复盘才有样本可归因，回测才能回答「模型在变好还是变差」。
        不落盘的话，每一次重启都从零开始，永远学不到任何东西。

    ⚠️ 幂等：predictions 以 fixture_id 为主键、ON CONFLICT DO UPDATE，
        重复执行只刷新概率，不产生重复行。
    """
    app = context.application
    service: PredictionService = app.bot_data["service"]
    try:
        # 必须用「全部启用联赛」，不能用主联赛。
        # 赛程同步（sync.py）遍历 settings.league_ids 拉取 10 个联赛 × 30 天，
        # 而这里若只用 settings.league_id，看板展示的比赛里
        # 除英超/欧冠外的 8 个联赛永远显示「尚未生成预测」。
        # 有赛程就必须有预测，两边得是同一批比赛。
        enabled = getattr(service.settings, "league_ids", None) or (
            int(service.settings.league_id),)
        ids = tuple(dict.fromkeys(
            tuple(int(x) for x in enabled) + tuple(AUTO_PREDICT_EXTRA_LEAGUES)
        ))
        r = await service.auto_predict_store(ids, days=AUTO_PREDICT_DAYS)
        log.info(
            "自动预测落盘：%d 场（联赛 %s，窗口 %d 天）",
            r["saved"], list(r["per_league"]), r["window_days"],
        )
        for err in r["errors"]:
            log.warning("自动预测：%s", err)
    except Exception as exc:  # 自动预测失败绝不能影响推送主流程
        log.warning("自动预测失败（不影响主流程）：%s", exc)


async def prematch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """赛前刷新：对即将开赛的比赛重算预测，让首发阵容信号真正生效。

    为什么必须单独一个任务，而不是把 auto_predict_job 调密：
        首发阵容按官方说明要开赛前 20–40 分钟才公布。自动预测 6 小时一轮，
        两者节奏对不上时，某场比赛在上一轮「还没公布」、下一轮「已经开赛」，
        阵容信号在线上一次都不会触发。把这个窗口单独拎出来高频跑，
        才拿得到阵容。

    ⚠️ 只对进入窗口的比赛发请求（见 service.prematch_refresh），
        且拿到阵容的当轮之后就跳过，因此额度开销是常数级而非全量。
    """
    app = context.application
    service: PredictionService = app.bot_data["service"]
    try:
        r = await service.prematch_refresh()
        if r.get("checked"):
            log.info(
                "赛前刷新完成：窗口内 %d 场，重算 %d 场，已取过阵容跳过 %d 场",
                r["checked"], r["refreshed"], r["skipped"],
            )
        for err in r.get("errors", []):
            log.warning("赛前刷新：%s", err)
    except Exception as exc:  # 刷新失败绝不能影响推送主流程
        log.warning("赛前刷新失败（不影响主流程）：%s", exc)


async def review_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """每日复盘：把「昨天推的准不准、准在哪」讲清楚并推送。

    与 backtest_job 的分工：
        回测 → 模型在**内置历史语料**上的表现（回答「方法对不对」）
        复盘 → 模型在**线上真实预测**上的表现（回答「今天该不该信它」）
    两者口径不同，不能互相替代。

    ⚠️ 只读不改：本任务不调整任何模型参数。小样本下自动调参极易过拟合，
        先把归因结论呈现出来，是否据此调参由人决定。
    """
    from review import daily_review, render_review

    app = context.application
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    try:
        rv = daily_review(getattr(service.settings, "db_path", None),
                          since_days=REVIEW_WINDOW_DAYS)
        text = render_review(rv)
    except Exception as exc:
        log.warning("复盘生成失败：%s", exc)
        return
    if rv.get("status") != "ok":
        # 无样本时静默：每天发「暂无数据」是纯噪音，看板里能看到
        log.info("复盘跳过：暂无已结算样本")
        return
    try:
        await app.bot.send_message(
            chat_id=settings.chat_target, text=normalize(text), parse_mode="HTML",
        )
        log.info("复盘推送完成：%d 场，命中率 %.2f%%",
                 rv["settled"], rv["accuracy"] * 100)
    except Exception as exc:
        log.warning("复盘推送失败：%s", exc)


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
    # 无人值守预测：让「有赛程」和「有预测」是同一批比赛，
    # 并为复盘/回测积累真实的赛前预测流水（不依赖用户手动点按钮）。
    app.job_queue.run_repeating(
        auto_predict_job, interval=AUTO_PREDICT_INTERVAL_HOURS * 3600, first=120,
        name=AUTO_PREDICT_JOB,
    )
    # 赛前刷新：只跑「即将开赛」的少数比赛，拿到首发阵容后重算概率。
    # 不注册的条件说在前面——赛前信号被整体关掉，或间隔填 0 明确表示不要，
    # 这时跑它只会白烧额度。它也不依赖 CHAT_ID：刷新是为了数据闭环，
    # 不是推送。
    prematch_registered = False
    if getattr(settings, "prematch_enabled", False) and \
            getattr(settings, "prematch_refresh_minutes", 0) > 0:
        app.job_queue.run_repeating(
            prematch_job,
            interval=int(settings.prematch_refresh_minutes) * 60,
            first=max(int(settings.prematch_refresh_minutes) * 60, 60),
            name=PREMATCH_JOB,
        )
        prematch_registered = True
    else:
        log.info("赛前刷新未注册：PREMATCH_ENABLED 关闭或 PREMATCH_REFRESH_MINUTES=0")

    # 赛果结算移出 chat_id 分支（此前误放进来，导致没配群号时一条都不结算）。
    # 结算是数据闭环的地基：不结算就永远没有命中率样本，复盘与校准都是空的。
    app.job_queue.run_repeating(
        settle_job, interval=6 * 3600, first=300, name=SETTLE_JOB,
    )
    # 5 = 同步 / 历史回填 / 回测 / 自动预测 / 结算；赛前刷新按开关另计。
    registered = 6 if prematch_registered else 5
    if settings.chat_id:
        app.job_queue.run_daily(daily_push, time=settings.push_time, name=DAILY_JOB)
        # 每日复盘：把「准在哪、不准在哪」讲清楚，是进步型机器人的反馈回路
        app.job_queue.run_daily(
            review_job, time=settings.review_time, name=REVIEW_JOB,
        )
        registered += 2
    else:
        log.warning("未设置 CHAT_ID：定时推送与复盘已停用（仍可使用命令）")
    return registered
