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

import joblog
from backtester_with_db import BacktesterWithDB
from bot_handler import BotUI
from config import Settings
from keyboards import Keyboards
from service import PredictionService
from support import describe_error, notify_admins
from tghtml import normalize
from views.digest import TELEGRAM_MAX_CHARS, DigestView

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
CACHE_JOB = "cache_purge"

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

# 哪些任务吃外网额度。额度吃紧时要按这张表**从后往前**停：
# 停掉回测/历史回填，系统照样能推送与结算；反过来则整个数据闭环断掉。
# 顺序即优先级，不要随意调换。
QUOTA_CONSUMERS: tuple[str, ...] = (
    SETTLE_JOB,        # 结算：命中率样本的地基，最后才停
    AUTO_PREDICT_JOB,  # 无人值守预测：看板与推送的来源
    SYNC_JOB,          # 赛程同步：没有赛程就没有一切
    PREMATCH_JOB,      # 赛前刷新：锦上添花
    HISTORY_JOB,       # 历史回填：数百场，额度大户
    BACKTEST_JOB,      # 回测：可用本地库跑，最先停
)


# 分级熔断阈值：剩余次数低于该值时，**从该档起往后**的任务整批跳过。
#
# 为什么用绝对剩余次数而不是百分比：主源的每日上限未必报得出来，
# 没有分母时百分比无从计算。而「还剩多少次」是每次响应都会带的硬数字，
# 拿它分档在任何套餐下都成立。
#
# 为什么按档位而不是一刀切：额度吃紧时仍有必须保住的链路——
# 结算（命中率样本）、预测（推送来源）、同步（赛程）。
# 一刀切熔断会把这些一起停掉，系统看起来在省额度，实际是数据闭环断了。
QUOTA_GATE: tuple[tuple[str, int], ...] = (
    (BACKTEST_JOB, 800),    # 回测可用本地库跑，先停它
    (HISTORY_JOB, 400),     # 整季回填数百场，额度大户
    (PREMATCH_JOB, 150),    # 赛前刷新是锦上添花
)

# 额度**未知**时一律放行。理由同 api_client.quota_exhausted：
# 拿不到读数就停所有任务，比额度打光本身更糟——那等于因为量具坏了
# 就把生产线关掉。
QUOTA_GATE_ENABLED = True


def quota_allows(task: str, quota_left) -> tuple[bool, str | None]:
    """判断任务在当前额度下是否该继续跑。

    返回 (是否放行, 跳过原因)。原因要能直接给用户看——
    「额度不足跳过」这种说法等于没说，必须带上剩余次数与阈值。
    """
    if not QUOTA_GATE_ENABLED:
        return True, None
    # 未知额度不放行判断：不猜，也不因此停摆
    if quota_left is None:
        return True, None
    try:
        left = int(quota_left)
    except (TypeError, ValueError):
        return True, None
    for name, floor in QUOTA_GATE:
        if task == name and left < floor:
            return False, (
                f"剩余额度 {left} 次，低于该任务的保留线 {floor} 次，本次跳过。"
                f"额度每日重置后自动恢复。")
    return True, None


def _db_path_of(app):
    """取当前数据库路径（供 joblog 落库）；拿不到返回 None。"""
    service = (app.bot_data or {}).get("service")
    if service is None:
        return None
    return getattr(getattr(service, "settings", None), "db_path", None)


def _current_quota_left(app) -> int | None:
    """从已装配的 API 客户端上读剩余额度；拿不到返回 None。

    只读**已观测到的**数字，绝不为此额外发一次请求——
    查额度的请求本身也要消耗额度。
    """
    api = (app.bot_data or {}).get("api")
    if api is None:
        return None
    # DataSourceRouter 代理到主源
    primary = getattr(api, "primary", None) or api
    getter = getattr(primary, "quota_left", None)
    try:
        return getter() if callable(getter) else getattr(primary, "quota_left", None)
    except Exception:  # noqa: BLE001
        return None


def with_quota_scope(name: str, fn):
    """把任务执行期间的 API 消耗记到该任务名下。

    不做这层标注，额度表只知道「两个源各用了多少」，
    答不了「是谁在用」——额度打光时只能靠猜该停哪个任务。

    包装在注册处统一做，而不是改每个 job 函数体：
    任务有 9 个，逐个改必然漏，漏掉的那个恰好成为盲区。
    """
    async def wrapped(context):
        from repository import quota_scope
        app = context.application
        left = _current_quota_left(app)
        allowed, reason = quota_allows(name, left)
        if not allowed:
            log.warning("任务 %s 跳过：%s", name, reason)
            # 跳过必须落库，否则看板「任务状态」里它凭空消失——
            # 用户会以为任务没跑，而不是「有意省额度」。
            joblog.skip(_db_path_of(app), name, reason)
            return None
        with quota_scope(name):
            return await fn(context)
    # 保留被包装函数的名字，PTB 与日志里才看得出是哪个 job
    wrapped.__name__ = getattr(fn, "__name__", name)
    wrapped.__wrapped__ = fn
    return wrapped

# 自动预测的向前窗口（天）。
# 必须与看板可切换的范围对齐：看板 days 支持 1/3/7，窗口只有 2 天时
# 切到「3 天」第三天必然整片未预测。取 3 天覆盖看板默认档位，
# 再远则赛程会变（改期、阵容未定、赔率未开），提前算好的概率是噪音。
AUTO_PREDICT_DAYS = 3

# 复盘的两个窗口（天），各司其职、不可合并。
#
# 归因窗口（REVIEW_ATTRIBUTION_DAYS）：分组统计、Brier 分解、标定判定用它。
#   原实现只有一个 7 天窗口，实测线上 7 天只结算约 40 场，而复盘要按
#   联赛 / 方向 / 信心档 / 口径 四个维度分组——40 场切成 6 个联赛后每组
#   不足 MIN_SAMPLE(8)，全部标 thin，"样本不足"天天报。
#   关键在于它**永远自愈不了**：窗口写死 7 天，样本上限就被钉死，
#   攒再多历史数据也进不来。放到 30 天（约 160 场）分组才有统计意义。
#   时效性损失由下面的近期窗口补回。
REVIEW_ATTRIBUTION_DAYS = 30
#
# 近期窗口（REVIEW_RECENT_DAYS）：只报一个数字，不参与任何归因。
#   它必然样本不足（7 天约 40 场），用来归因就是噪音，
#   但用来回答"最近手感如何"足够，且是归因窗口给不了的时效信息。
REVIEW_RECENT_DAYS = 7
#
# 兼容旧名：仍指向近期窗口，避免既有引用报错。
REVIEW_WINDOW_DAYS = REVIEW_RECENT_DAYS

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

    ⚠️ 收件人（chat_id）可在调用时显式指定：**不给就退回
    settings.chat_target**（定时任务的行为，不受影响）。
    手动入口（/test、按钮）必须显式传入发起会话的 id —— 否则在私聊里
    触发的测试推送会飞到群里，本人反而一条都看不到。
    """

    def __init__(self, app) -> None:
        self.app = app

    def _target(self, chat_id: object = None) -> object:
        """解析收件人：显式优先，缺省退回配置的 CHAT_ID。"""
        if chat_id is not None:
            return chat_id
        return self.settings.chat_target

    @property
    def settings(self) -> Settings:
        return self.app.bot_data["settings"]

    async def send_prediction(self, prediction, chat_id: object = None) -> None:
        settings = self.settings
        await self.app.bot.send_message(
            chat_id=self._target(chat_id),
            # 推送是唯一「无人值守」的出口：一条畸形标签会让整条推送 400 失败
            # 且没人会发现，因此这里必须过规范化层。
            text=normalize(ui.format_prediction(prediction, settings.timezone)),
            parse_mode="HTML",
            reply_markup=ui.get_main_keyboard(prediction.fixture_id, "home"),
        )

    async def send_digest(self, predictions, *, note: str | None = None,
                          chat_id: object = None) -> int:
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
        tz = settings.timezone
        # 首屏只推焦点战，完整列表收进「📋 完整赛程」按钮。此前把当天全部
        # 场次平铺进推送，30 场时要滑很久，重点反而被淹没了。
        text = DigestView.format_daily_overview(predictions, settings, tz, note=note)
        # 焦点战按钮直接带队名：序号按钮（「预测 3」）在翻页/筛选后会重排，
        # 点进去落到另一场比赛是这类列表最常见的误操作。
        markup = Keyboards.digest_keyboard(DigestView.focus_entries(predictions))

        # 总览结构上是恒定短小的（3 场 + 一行概览），但队名极端长时仍可能
        # 顶到 4096。这是无人值守的定时推送，400 失败没人会发现，所以超限
        # 时退回分页版，宁可多几条也不能整条发不出去。
        if len(text) > TELEGRAM_MAX_CHARS:
            pages = DigestView.format_daily_digest_pages(predictions, settings, tz, note=note)
            sent = 0
            for page in pages:
                await self.app.bot.send_message(
                    chat_id=self._target(chat_id),
                    text=normalize(page),
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                sent += 1
            return sent

        await self.app.bot.send_message(
            chat_id=self._target(chat_id),
            text=normalize(text),
            parse_mode="HTML",
            reply_markup=markup,
        )
        return 1

    async def notify_admins(self, text: str) -> None:
        await notify_admins(self.app, text)


async def run_push(app, *, widen: bool = False, chat_id: object = None) -> PushResult:
    """生成并推送预测。/test 与每日定时任务走的是同一条路径。

    ⚠️ chat_id：显式传入时发到该会话（手动触发必须这么做 —— 私聊里敲
        /test 却发到群，本人一条都看不到）；不给则退回 settings.chat_target
       （定时任务的行为，保持不变）。
    """
    settings: Settings = app.bot_data["settings"]
    service: PredictionService = app.bot_data["service"]
    if chat_id is None and settings.chat_target is None:
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

    # 推荐闸门：先按「把握度 + 赔率区间」筛掉不该推的，再取最有把握的 N 场。
    #
    # 顺序不能反：闸门是**绝对值门槛**（这场够不够格），topn 是**相对排序**
    # （够格的里面挑最好的几个）。若先 topn 再闸门，会在「今天本来就只有
    # 3 场且都不够格」时把这 3 场推上去——闸门就形同虚设。
    gated = service.gate_predictions(predictions)
    gate_note = None
    if service.last_gate and service.last_gate.get("in"):
        g = service.last_gate
        blocked = g["in"] - g["pass"]
        if blocked > 0:
            gate_note = (
                f"🚦 推荐闸门：{g['in']} 场候选中 {blocked} 场未通过"
                f"（无亚盘 {g['no_handicap']} · 把握度<{settings.rec_min_confidence:.0%} "
                f"{g['low_confidence']} · 赔率过低 {g['odds_low']} · 赔率过高 {g['odds_high']}），"
                f"推送 {g['pass']} 场。"
            )
    if gate_note:
        note = f"{note}\n{gate_note}" if note else gate_note

    # 只推最有把握的 N 场：场次铺满会把重点淹没，低把握场次还会拉低整体命中率。
    # 砍掉的场数必须写进 note，否则用户无从判断是「今天只有 3 场」还是「被砍到 3 场」。
    picked = service.select_top_predictions(gated)
    topn_note = service.last_note if service.last_note != note else None
    if topn_note:
        note = f"{note}\n{topn_note}" if note else topn_note
    predictions = picked

    sender = NotificationManager(app)
    await sender.send_digest(predictions, note=note, chat_id=chat_id)
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
    db_path = getattr(service.settings, "db_path", None)
    try:
        done = await service.sync_results()
        if done:
            log.info("定时结算完成：%d 场", done)
        joblog.ok(db_path, SETTLE_JOB)
    except Exception as exc:  # 结算失败绝不能影响主流程
        log.warning("定时结算失败：%s", exc)
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), SETTLE_JOB, exc)


async def sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """定时同步：把赛程拉到本地 SQLite（查询优先读本地库，省 API 额度）。"""
    app = context.application
    service: PredictionService = app.bot_data["service"]
    db_path = getattr(service.settings, "db_path", None)
    try:
        up = await service.sync.sync_upcoming()
        recent = await service.sync.sync_recent()
        log.info(
            "赛程同步完成：未来 %d 场 / 最近 %d 场，本地库共 %d 场",
            up.get("saved", 0), recent.get("saved", 0), service.repo.matches_count(),
        )
        joblog.ok(db_path, SYNC_JOB)
    except Exception as exc:  # 同步失败不能影响主流程
        log.warning("赛程同步失败：%s", exc)
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), SYNC_JOB, exc)


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
    db_path = getattr(service.settings, "db_path", None)
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
        joblog.ok(db_path, AUTO_PREDICT_JOB)
        )
        for err in r["errors"]:
            log.warning("自动预测：%s", err)
    except Exception as exc:  # 自动预测失败绝不能影响推送主流程
        log.warning("自动预测失败（不影响主流程）：%s", exc)
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), AUTO_PREDICT_JOB, exc)


PREMATCH_PUSH_NOTE = (
    "🔄 赛前更新：已并入最新伤停与首发阵容，与早间推送相比概率可能变化。"
)


async def prematch_push(app, entries) -> int:
    """赛前二次推送：把刚用实时数据重算过的比赛推一次，且**每场只推一次**。

    为什么必须推，只落库不够：
        早间 08:00 推送的概率里没有当天的伤停与首发。赛前刷新把新概率
        写进库，但用户看到的仍是早上那条 —— 赛前信号接了等于没接。

    去重靠 prematch_push 表（同一场终身一行）：赛前刷新每 15 分钟一轮，
        不去重就是每 15 分钟刷一次屏。

    ⚠️ 推送时机：拿到阵容立刻推；始终拿不到（部分赛事官方不提供阵容）
        则在 PREMATCH_PUSH_FORCE_MINUTES 兜底推一次，免得这场永远等不到更新。
        窗口外（距开赛 > PREMATCH_PUSH_LEAD_MINUTES）不推 —— 那属于早间推送的职责。
    """
    settings: Settings = app.bot_data["settings"]
    if not getattr(settings, "prematch_push_enabled", False):
        return 0
    service: PredictionService = app.bot_data["service"]
    lead = max(int(getattr(settings, "prematch_push_lead_minutes", 60) or 0), 0) * 60
    force = max(int(getattr(settings, "prematch_push_force_minutes", 20) or 0), 0) * 60
    repo = service.repo
    picked: list = []
    for entry in entries or []:
        fid = entry.get("fixture_id")
        if fid is None or repo.prematch_pushed(fid):
            continue
        remaining = entry.get("lead_seconds")
        if remaining is None or remaining > lead:
            continue
        # 还没拿到阵容就再等一轮；临近开赛仍拿不到则兜底推，不再无限等
        if not entry.get("lineup") and remaining > force:
            continue
        if entry.get("prediction") is None:
            continue
        picked.append(entry)
    if not picked:
        return 0
    sender = NotificationManager(app)
    sent = await sender.send_digest(
        [e["prediction"] for e in picked], note=PREMATCH_PUSH_NOTE)
    # 只有真的发出去了才标记：标记早于发送会让一次失败变成永久不再推送
    repo.mark_prematch_pushed([e["fixture_id"] for e in picked])
    return sent


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
        try:
            pushed = await prematch_push(app, r.get("entries", []))
            if pushed:
                log.info("赛前更新已推送 %d 条", pushed)
        except Exception as exc:  # 推送失败绝不能影响刷新与主流程
            log.warning("赛前推送失败（不影响主流程）：%s", exc)
            joblog.fail(getattr(service.settings, "db_path", None), PREMATCH_JOB, exc)
    except Exception as exc:  # 刷新失败绝不能影响推送主流程
        log.warning("赛前刷新失败（不影响主流程）：%s", exc)
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), PREMATCH_JOB, exc)


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
                          since_days=REVIEW_ATTRIBUTION_DAYS,
                          recent_days=REVIEW_RECENT_DAYS)
        text = render_review(rv)
    except Exception as exc:
        log.warning("复盘生成失败：%s", exc)
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), REVIEW_JOB, exc)
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
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), REVIEW_JOB, exc)


async def history_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """回填历史赛季（付费套餐解锁），让库里真正积累起赛果。

    只补赛程与赛果，不伪造预测记录——回填时用现在的积分榜去算过去的比赛
    会引入前视偏差，得出的命中率是假象。
    """
    app = context.application
    service: PredictionService = app.bot_data["service"]
    db_path = getattr(service.settings, "db_path", None)
    try:
        r = await service.sync.sync_history()
        log.info("历史赛季回填完成：赛季 %d 个，收到 %d 保存 %d",
                 r.get("seasons", 0), r.get("received", 0), r.get("saved", 0))
        joblog.ok(db_path, HISTORY_JOB)
    except Exception as exc:  # 回填失败不能影响主流程
        log.warning("历史赛季回填失败：%s", exc)
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), HISTORY_JOB, exc)


async def cache_purge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """清掉过期很久的外部数据缓存。

    为什么必须有个任务来扫：external_cache 每取一次外部数据就多一行，
    键是「类型+比赛ID」，一场比赛赛前赛后能被取很多次。只增不删的话
    表会一直涨，而它里面装的都是几十分钟后就没用的临时数据。

    为什么不是过期即删（见 cache_purge_expired 的 24 小时宽限）：
    过期缓存还要留着当外网失败时的兜底，立刻删等于把兜底也扔了。
    """
    app = context.application
    service: PredictionService = app.bot_data["service"]
    db_path = getattr(service.settings, "db_path", None)
    try:
        n = service.repo.cache_purge_expired()
        log.info("外部缓存清理完成：删除 %d 条过期记录", n)
        joblog.ok(db_path, CACHE_JOB)
    except Exception as exc:
        log.warning("外部缓存清理失败：%s", exc)
        joblog.fail(db_path, CACHE_JOB, exc)


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
    db_path = getattr(service.settings, "db_path", None)
    try:
        # 口径必须显式对齐线上：线上推送是 w·市场+(1−w)·模型 的融合口径，
        # 不传 market_weight 就退化成纯泊松，回测分数代表不了线上真实表现。
        # 这里用 settings 的真实生效值，而不是写死 0.75 —— 改了配置必须跟着变。
        # 统一走兼容层 BacktesterWithDB：数据源与评估仍委托底层真实回测引擎
        # （见 backtester_with_db 模块头），但对外只保留这一个入口，
        # 避免"两个回测器各有各的默认值"。
        # market_weight / min_history 必须继续显式传——口径说明见 BacktesterWithDB。
        bt = BacktesterWithDB(
            league_id=service.settings.league_id,
            db_path=getattr(service.settings, "db_path", None),
            market_weight=getattr(service.settings, "market_weight", None),
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
        joblog.ok(db_path, BACKTEST_JOB)
    except Exception as exc:  # 回测失败绝不能影响推送主流程
        log.warning("回测失败（不影响主流程）：%s", exc)
        # 失败落库：只写日志的话，用户要翻 Railway 日志才知道它挂了。
        joblog.fail(getattr(service.settings, "db_path", None), BACKTEST_JOB, exc)


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
        with_quota_scope(SYNC_JOB, sync_job), interval=SYNC_INTERVAL_HOURS * 3600, first=10, name=SYNC_JOB,
    )
    # 历史回填与回测**不依赖 CHAT_ID**：它们是数据积累与模型体检，
    # 属于"程序自己该做的事"，不该因为没配推送群就整个停掉。
    # （此前的 settle_job 就被误放进 chat_id 分支，导致没配群号时
    #  赛果一条都不结算——那个问题单独修，这里先保证不再重演。）
    app.job_queue.run_repeating(
        with_quota_scope(HISTORY_JOB, history_job), interval=HISTORY_INTERVAL_HOURS * 3600, first=600,
        name=HISTORY_JOB,
    )
    app.job_queue.run_repeating(
        with_quota_scope(BACKTEST_JOB, backtest_job), interval=BACKTEST_INTERVAL_HOURS * 3600, first=1800,
        name=BACKTEST_JOB,
    )
    # 无人值守预测：让「有赛程」和「有预测」是同一批比赛，
    # 并为复盘/回测积累真实的赛前预测流水（不依赖用户手动点按钮）。
    app.job_queue.run_repeating(
        with_quota_scope(AUTO_PREDICT_JOB, auto_predict_job), interval=AUTO_PREDICT_INTERVAL_HOURS * 3600, first=120,
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
            with_quota_scope(PREMATCH_JOB, prematch_job),
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
        with_quota_scope(SETTLE_JOB, settle_job), interval=6 * 3600, first=300, name=SETTLE_JOB,
    )
    # 缓存清理：每天扫一次足够，过期缓存本来还要留 24 小时当兜底。
    app.job_queue.run_repeating(
        with_quota_scope(CACHE_JOB, cache_purge_job), interval=24 * 3600, first=3600, name=CACHE_JOB,
    )
    # 6 = 同步 / 历史回填 / 回测 / 自动预测 / 结算 / 缓存清理；
    # 赛前刷新按开关另计。
    registered = 7 if prematch_registered else 6
    if settings.chat_id:
        app.job_queue.run_daily(with_quota_scope(DAILY_JOB, daily_push), time=settings.push_time, name=DAILY_JOB)
        # 每日复盘：把「准在哪、不准在哪」讲清楚，是进步型机器人的反馈回路
        app.job_queue.run_daily(
            with_quota_scope(REVIEW_JOB, review_job), time=settings.review_time, name=REVIEW_JOB,
        )
        registered += 2
    else:
        log.warning("未设置 CHAT_ID：定时推送与复盘已停用（仍可使用命令）")
    return registered
