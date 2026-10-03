"""管理员指令：/test /status /stats /storage

权限由 dispatcher 统一拦截（admin_only=True），本模块不再重复判权。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import paths
from backtest import DEFAULT_MIN_HISTORY, run_backtest
from backtest_corpus import load_corpus
from api_client import parse_shots_on_target, stat_types_of
from repository import quota_today
from bot_handler import BotUI, esc
from formatkit import BLANK, SEP, hbar, kv_line, pad_cjk, section, section_join
from migrate_elo import fetch_finished
from tghtml import normalize
from telegram.constants import ChatAction

from scheduler import DAILY_JOB, run_push
from support import describe_error

from . import CommandDispatcher

log = logging.getLogger("bot")


async def test_cmd(update, context) -> None:
    """/test — 立即推送一次预测，走与定时任务完全相同的路径。"""
    app = context.application
    progress = await update.effective_message.reply_text("⏳ 正在获取数据并生成预测…")
    try:
        result = await run_push(app, widen=True)
    except Exception as exc:
        log.exception("/test 失败")
        await progress.edit_text(f"❌ 发送失败：{describe_error(exc)}")
        return
    if result.sent == 0:
        await progress.edit_text(
            "ℹ️ 没有可推送的比赛（近期赛程为空或已全部开赛），未发送任何消息。")
    else:
        extra = f"\n{result.note}" if result.note else ""
        await progress.edit_text(f"✅ 已向 CHAT_ID 发送 {result.sent} 条预测。{extra}")


def _format_quota(api: Any, req: dict) -> str:
    """把额度显示成「真实发起了多少次请求」。

    主源（API-Football）能自己报额度；备用源 football-data.org 免费层没有
    额度查询端点，因此统一改用本地实测计数 —— 两者都显示，不再出现 ? / ?。
    """
    parts: list[str] = []
    current, limit = req.get("current"), req.get("limit_day")
    if current is not None and limit is not None:
        parts.append(f"主源 {current} / {limit}")
    try:
        counts = quota_today()
    except Exception:  # 计数不可用时退回额度端点数据
        counts = {}
    local: list[str] = []
    for src, label in (("api-football", "主源"), ("football-data", "备用源")):
        n = counts.get(src, 0)
        if n:
            local.append(f"{label} {n}")
    if local:
        parts.append("本地实测 " + " · ".join(local))
    if not parts:
        # 主源报不出额度、本地也没有计数（例如刚重启且尚未发请求）
        parts.append("本地实测 0")
    return "｜".join(parts)


async def diag_cmd(update, context) -> None:
    """/diag — 直连主源探活，暴露真实失败原因（套餐 / 赛季 / 配额 / 冷却）。

    常规请求会被冷却逻辑挡住，失败时只提示「未尝试或已恢复」，
    排查时看不到主源到底为什么不可用。本命令绕过调度层直连主源，
    把原始错误原样呈现，用于一次性定位问题。
    """
    app = context.application
    settings = app.bot_data["settings"]
    router = app.bot_data["api"]
    # 路由器负责主备切换；诊断必须直连主源才能拿到真实原因
    primary = getattr(router, "primary", router)

    W = 12
    rows: list[tuple[str, str, str]] = []

    # 冷却状态：冷却期内主源根本不会被调用，所以要先说明
    if callable(getattr(router, "_primary_cooling", None)) and router._primary_cooling():
        left = int(getattr(router, "_primary_down_until", 0) - time.monotonic())
        rows.append(("🧊", "冷却状态", f"冷却中（剩余 {max(left, 0)} 秒，此期间主源不被调用）"))
    else:
        rows.append(("🧊", "冷却状态", "未冷却"))

    # 主源最近一次失败原因（调度层记录的）
    recorded = None
    if callable(getattr(router, "last_error", None)):
        recorded = router.last_error("api-football")
    rows.append(("📡", "主源记录", esc(recorded or "无失败记录")))

    now = datetime.now(settings.timezone)
    d_from = now.date()
    d_to = (now + timedelta(hours=settings.lookahead_hours)).date()

    # 真实请求 1：账号可用赛季（能直接暴露套餐限制）
    seasons_fn = getattr(primary, "get_available_seasons", None)
    if callable(seasons_fn):
        try:
            seasons = await primary.get_available_seasons()
            if seasons:
                mark = "✅" if settings.season in seasons else "⚠️"
                rows.append(("📋", "可用赛季", f"{mark} {'、'.join(str(x) for x in seasons)}"))
            else:
                rows.append(("📋", "可用赛季", "⚠️ 返回空（套餐可能不含赛季权限）"))
        except Exception as exc:  # noqa: BLE001 - 诊断命令要把原因显示出来，不吞异常
            rows.append(("📋", "可用赛季", f"❌ {esc(describe_error(exc))}"))

    # 真实请求 2：按当前窗口拉赛程，参数与每日推送一致
    try:
        fixtures = await primary.get_fixtures(settings.league_id, settings.season, d_from, d_to)
        rows.append(("🧪", "赛程请求", f"✅ {len(fixtures)} 场"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("🧪", "赛程请求", f"❌ {esc(describe_error(exc))}"))

    rows.append(("⚙️", "请求参数",
                 f"联赛 {settings.league_id} · 赛季 {settings.season} · {d_from} ~ {d_to}"))

    body = section_join([
        section("🔬 主源诊断", [kv_line(i, k, v, width=W) for i, k, v in rows]),
    ])
    await update.message.reply_text(normalize(body), parse_mode="HTML")


async def sot_probe_cmd(update, context) -> None:
    """/sotprobe — 探测单场技术统计端点是否可用，并确认射正数字段。

    射正数（一场 4~6 次）样本量远大于进球（1~2 个），赛季初收敛更快，
    离线回测已验证能同时改善 Log Loss 与命中率。但它需要额外请求
    /fixtures/statistics，而该端点在套餐下是否可用、字段叫什么，
    只有真实请求才能确认 —— 沙盒无法出网验证，故先做探测再决定是否启用。
    """
    app = context.application
    settings = app.bot_data["settings"]
    router = app.bot_data["api"]
    service = app.bot_data["service"]
    primary = getattr(router, "primary", router)

    W = 12
    rows: list[tuple[str, str, str]] = []

    targets = service.repo.latest_finished(getattr(getattr(service, "sync", None), "competition", "") or "", limit=1)
    if not targets:
        rows.append(("⚠️", "探测对象", "本地库还没有已完场的比赛，先跑一次同步或 /backfill"))
        body = section_join([section("🔬 场面数据探测",
                                     [kv_line(i, k, v, width=W) for i, k, v in rows])])
        await update.message.reply_text(normalize(body), parse_mode="HTML")
        return

    m = targets[0]
    mid = m["id"]
    rows.append(("🎯", "比赛",
                 f"{esc(m['home_team_name'] or '?')} vs {esc(m['away_team_name'] or '?')}"))
    rows.append(("🕐", "时间/ID", f"{esc(str(m['utc_date'])[:16])} · #{esc(mid)}"))
    rows.append(("⚽", "比分", f"{m['home_score']} - {m['away_score']}"))

    # 真实请求：拿不到就原样显示原因（套餐/未收录/额度），不猜测
    try:
        resp = await primary.get_fixture_statistics(mid)
    except Exception as exc:  # noqa: BLE001 - 探测命令要把原因显示出来
        rows.append(("📡", "统计端点", f"❌ {esc(describe_error(exc))}"))
        rows.append(("🧭", "结论", "该端点不可用，射正数口径暂时无法在生产启用"))
        body = section_join([section("🔬 场面数据探测",
                                     [kv_line(i, k, v, width=W) for i, k, v in rows])])
        await update.message.reply_text(normalize(body), parse_mode="HTML")
        return

    if not resp:
        rows.append(("📡", "统计端点", "⚠️ 返回空（该场可能未收录统计）"))
        service.repo.save_match_stats(mid, None, None)
        rows.append(("💾", "标记", "已记为探测过，后续不再重复请求"))
        body = section_join([section("🔬 场面数据探测",
                                     [kv_line(i, k, v, width=W) for i, k, v in rows])])
        await update.message.reply_text(normalize(body), parse_mode="HTML")
        return

    rows.append(("📡", "统计端点", f"✅ 可用（{len(resp)} 队）"))
    types = stat_types_of(resp)
    if types:
        shown = "、".join(esc(t) for t in types[:12])
        if len(types) > 12:
            shown += f" …等 {len(types)} 项"
        rows.append(("🧩", "可用字段", shown))

    sot = parse_shots_on_target(resp, home_team_id=m["home_team_id"],
                                away_team_id=m["away_team_id"])
    if sot is None:
        rows.append(("🎯", "射正数", "⚠️ 未解析到（字段名不匹配，见上方可用字段）"))
        service.repo.save_match_stats(mid, None, None)
    else:
        rows.append(("🎯", "射正数", f"✅ 主 {sot['home']} · 客 {sot['away']}"))
        ok = service.repo.save_match_stats(mid, sot["home"], sot["away"])
        rows.append(("💾", "写入库", "✅ 已保存" if ok else "❌ 保存失败"))

    body = section_join([section("🔬 场面数据探测",
                                 [kv_line(i, k, v, width=W) for i, k, v in rows])])
    await update.message.reply_text(normalize(body), parse_mode="HTML")


async def _account_status_preferring_primary(api: Any) -> dict:
    """读取账号/额度信息，优先直连主源。

    套餐与额度是**主源账号**的属性，与当前生效的是哪个源无关。
    主源冷却时常规路由会把这个请求发给备用源，于是付费账号被显示成 Free，
    让人误以为订阅失效 —— 因此优先问主源，问不到才回落。
    """
    primary = getattr(api, "primary", None)
    if primary is not None:
        try:
            account = await primary.get_account_status()
        except Exception:  # noqa: BLE001 - 主源读不到就回落到常规路由
            log.debug("主源账号信息读取失败，回落到当前生效数据源")
        else:
            if account:
                return account
    return await api.get_account_status()


async def status_cmd(update, context) -> None:
    """/status — 版本、赛季、下次推送、数据源诊断、持久化状态。"""
    app = context.application
    settings = app.bot_data["settings"]
    api = app.bot_data["api"]
    service = app.bot_data["service"]

    jobs = app.job_queue.get_jobs_by_name(DAILY_JOB) if app.job_queue else []
    # PTB JobQueue 的 job 属性名为 next_run_time（旧版本曾叫 next_t），兼容读取避免 AttributeError
    _next = None
    if jobs:
        _next = getattr(jobs[0], "next_run_time", None) or getattr(jobs[0], "next_t", None)
    next_run = _next.astimezone(settings.timezone).strftime("%m-%d %H:%M") if _next else "未启用"
    version = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "unknown")[:7]
    # 前缀交给标签列（「联赛/赛季」）承担，这里只给值，避免出现
    # 「联赛/赛季 联赛/赛季：39 / 2026」这种重复
    league_ids = tuple(getattr(settings, "league_ids", None) or (settings.league_id,))
    season_line = f"{' / '.join(str(i) for i in league_ids)} / {settings.season}"
    if settings.season != settings.expected_season:
        season_line += (
            f"（⚠️ 按日期应为 {settings.expected_season}，请更新 SEASON 变量；"
            f"无赛程时会自动改用 {settings.expected_season}）")
    if service.season_in_use != settings.season:
        season_line += f"\n实际使用赛季：{service.season_in_use}（已自动降级）"

    # 标签统一补到同一列宽：原来用「：」直接拼，短标签（CHAT_ID）与长标签
    # （备用源 football-data.org）的数值起点差很远，扫读时对不上列。
    W = 12
    runtime_rows = [
        ("🔖", "版本", esc(version)),
        ("🏆", "联赛/赛季", season_line),
        ("⏰", "推送时间",
         f"每天 {settings.push_time:%H:%M}（{esc(settings.timezone.zone)}）"),
        ("📅", "下次推送", esc(next_run)),
        ("🎯", "单场上限",
         f"最多 {settings.max_matches} 场 · 窗口 {settings.lookahead_hours} 小时"),
        ("💬", "CHAT_ID", "已配置" if settings.chat_id else "未配置"),
        ("📦", "缓存预测", f"{service.cached_predictions} 场"),
        ("💾", "存储", esc(paths.summary())),
    ]
    source_rows = [
        ("🔌", "渠道",
         "官方直连 (API-Sports)" if settings.api_provider == "apisports" else "RapidAPI"),
        ("📡", "数据源",
         f"{esc(getattr(api, 'source_label', 'API-Football'))}"
         + ("（备用源生效中）" if getattr(api, "using_fallback", False) else "")),
        ("🥈", "备用源",
         "已配置" if settings.football_data_available else "未配置"),
    ]
    # 备用源实测：真实请求一次，把结果/原因显示出来，便于管理员自查账号与套餐
    if settings.football_data_available and getattr(api, "fallback", None):
        try:
            probe = await api.fallback.probe(settings.league_id)
            if probe.get("ok"):
                source_rows.append(("🔎", "备用源实测",
                                    f"✅ {esc(probe['competition'])} 共 {probe['count']} 场"))
            else:
                source_rows.append((
                    "🔎", "备用源实测",
                    f"❌ {probe.get('count', 0)} 场｜"
                    f"{esc(probe.get('raw') or probe.get('detail') or '无数据')}",
                ))
        except Exception as exc:  # 诊断失败不影响状态页
            source_rows.append(("🔎", "备用源实测", f"⚠️ {esc(describe_error(exc))}"))
    try:
        account = await _account_status_preferring_primary(api)
        sub, req = account.get("subscription") or {}, account.get("requests") or {}
        source_rows.append((
            "📋", "套餐",
            f"{esc(sub.get('plan', '未知'))}（{'有效' if sub.get('active') else '未激活或未知'}）",
        ))
        source_rows.append((
            "📊", "今日请求", _format_quota(api, req),
        ))
        source_rows.append(("✅", "连通性", "数据源连通正常"))
    except Exception as exc:
        # 数据源不可用时仍应显示版本与配置，否则连版本号都看不到
        source_rows.append(("❌", "连通性", esc(describe_error(exc))))

    def rows(entries) -> list[str]:
        return [kv_line(icon, label, value, W) for icon, label, value in entries]

    blocks = [
        ["🤖 <b>运行状态</b>", BLANK],
        section("⚙️", "运行配置", *rows(runtime_rows)),
        section("🛰", "数据源诊断", *rows(source_rows)),
    ]
    text = section_join(blocks)
    await update.effective_message.reply_text(
        normalize(text), parse_mode="HTML", disable_web_page_preview=True
    )


async def stats_cmd(update, context) -> None:
    """/stats — 命中率统计（基于落盘的预测记录）。"""
    service = context.application.bot_data["service"]
    await _typing(update)
    st = service.stats()
    rate = st["rate"]
    W = 12
    # 命中率用等宽条 + 百分比：数字看大小，条看比例，两者缺一都不直观
    rate_value = f"{hbar(rate or 0.0)} {rate:.1%}" if rate is not None else "—"
    overview = [
        ("✅", "已结算", f"{st['total']} 场"),
        ("🎯", "命中", f"{st['hit']} 场"),
        ("📊", "命中率", rate_value),
        ("⏳", "待结算", f"{st['pending']} 场"),
        ("💾", "存储", "✅ 已落盘（重启不丢）" if st["persistent"] else "⚠️ 内存回退（重启会丢）"),
    ]
    blocks = [
        ["📈 <b>预测命中率</b>", BLANK],
        section("🧾", "总览", *[kv_line(i, k, v, W) for i, k, v in overview]),
    ]
    if st["total"] == 0:
        blocks.append(section("🕐", "等待首场结算", "暂无已结算的预测，赛果会在比赛结束后自动同步。"))
    else:
        streak = st["streak"]
        tail = f"🔥 连续命中 {streak} 场" if streak > 0 else (
            f"🧊 连续未中 {-streak} 场" if streak < 0 else "")
        level_rows = []
        if st["by_level"]:
            names = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低", "unknown": "⚪ 未知"}
            for key in ("high", "medium", "low", "unknown"):
                slot = st["by_level"].get(key)
                if not slot:
                    continue
                r = slot["hit"] / slot["total"] if slot["total"] else 0
                # 等级名与计数各自等宽：🟢 高 / ⚪ 未知 宽度不同，不补位会错开
                level_rows.append(
                    f"<code>{pad_cjk(names.get(key, key), 8)}</code>"
                    f"<code>{hbar(r)}</code> <code>{r:>3.0%}</code>"
                    f" <code>{slot['hit']}/{slot['total']}</code>"
                )
        extra = ([tail] if tail else []) + level_rows
        if extra:
            blocks.append(section("🏅", "按信心等级", *extra))
    await update.effective_message.reply_text(
        normalize(section_join(blocks)), parse_mode="HTML", disable_web_page_preview=True
    )


async def storage_cmd(update, context) -> None:
    """/storage — 存储五项自检。

    部署后跑一次，重新部署后再跑一次；第二次若仍能看到第一次的标记文件
    及其时间，即证明 Volume 生效。
    """
    settings = context.application.bot_data["settings"]
    service = context.application.bot_data["service"]
    await _typing(update)

    probe = paths.probe_storage()
    try:
        tables = service.repo.tables()
    except Exception:
        tables = []
    st = service.stats()

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    last_write = "—"
    try:
        db_file = Path(service.repo.db_path)
        if db_file.exists():
            from datetime import datetime, timezone

            mt = datetime.fromtimestamp(db_file.stat().st_mtime, timezone.utc)
            last_write = BotUI.fmt_time(mt, settings.timezone, "%m-%d %H:%M:%S")
    except Exception:
        pass

    age = probe.get("age_seconds")
    if age is None:
        age_text = "—"
    elif age < 60:
        age_text = "刚刚写入（本次启动首次）"
    elif age < 3600:
        age_text = f"{int(age // 60)} 分钟前写入"
    elif age < 86400:
        age_text = f"{age / 3600:.1f} 小时前写入"
    else:
        age_text = f"{age / 86400:.1f} 天前写入"

    mounted = probe["mounted"] and probe["age_seconds"] is not None and probe["age_seconds"] > 60
    W = 12
    checks = [
        (mark(probe["write"]), "写入测试", "通过" if probe["write"] else "失败"),
        (mark(probe["read"]), "读取测试", "通过（内容一致）" if probe["read"] else "失败"),
        # 表名列表很长，塞进对齐行会把整行撑到折行；这里只放张数，
        # 具体表名另起一行缩进展示，既对齐又不破坏版式
        (mark(bool(tables)), "数据表", f"{len(tables)} 张" if tables else "无表"),
        (mark(mounted), "Volume 挂载",
         "已生效（跨部署保留）" if mounted else "未确认——重新部署后再执行一次本命令"),
        ("🕑", "标记文件", esc(age_text)),
        ("🕑", "最后写入", esc(last_write)),
    ]
    # 表名行紧跟「数据表」而不是甩到节末尾：孤零零一行会被读成孤儿内容
    check_lines: list[str] = []
    for icon, label, value in checks:
        check_lines.append(kv_line(icon, label, value, W))
        if label == "数据表" and tables:
            check_lines.append("　" + esc(", ".join(tables)))
    blocks = [
        ["💾 <b>存储状态</b>", BLANK],
        section(
            "📂", "路径",
            kv_line("📁", "数据目录", esc(paths.DATA_DIR), W),
            kv_line("🗄", "数据库", esc(service.repo.db_path), W),
        ),
        section("🧪", "五项自检", *check_lines),
        section(
            "📈", "数据量",
            f"已预测 {st['total'] + st['pending']} 场 · 已结算 {st['total']} 场",
        ),
    ]
    if not probe["write"] or not probe["read"]:
        blocks.append(section(
            "⚠️", "风险",
            "写入/读取失败，数据留在容器临时目录，<b>重新部署会丢失</b>。"))
    elif not mounted:
        blocks.append(section(
            "ℹ️", "如何确认挂载",
            "首次执行属正常；请重新部署后再执行一次，若标记时间仍在即 Volume 生效。",
            "未确认挂载前，预测与命中率数据<b>可能在重新部署后清空</b>。"))
    await update.effective_message.reply_text(
        normalize(section_join(blocks)), parse_mode="HTML", disable_web_page_preview=True
    )


def _season_result_text(season: int, received: int, saved: int,
                        finished: int, message: str = "") -> str:
    """单赛季回填结果文案（/backfill <season> 用）。"""
    if not received:
        return (
            f"⚠️ {season} 赛季没有拉到数据\n\n"
            f"{message or '该赛季可能不在当前套餐范围内。'}"
        )
    return (
        f"✅ {season} 赛季已入库\n\n"
        f"📥 收到 {received} 场\n"
        f"💾 保存 {saved} 场\n"
        f"🏁 已完赛 {finished} 场（本地库累计）"
    )


async def backfill_cmd(update, context) -> None:
    """/backfill — 拉整季赛程入库，用于填充强度榜所需的历史样本。

    只补 **赛程与赛果**（matches 表），**不伪造预测记录**。
    理由：健康度曲线要反映模型「事前」的判断，而回填时用现在的积分榜去
    算过去的比赛会引入前视偏差（look-ahead bias），得出的命中率是虚高的
    假象。宁可曲线空着，也不要一条骗人的曲线。
    """
    await _typing(update)
    service = context.application.bot_data["service"]

    # /backfill            → 当前赛季（原行为，走日期窗口）
    # /backfill 2024       → 指定历史赛季（付费套餐解锁）
    # /backfill all        → 账号可用的全部赛季（含历史，请求量随赛季数增加）
    args = list(getattr(context, "args", None) or [])
    target = args[0].strip().lower() if args else ""

    if target and target != "all":
        try:
            season = int(target)
        except ValueError:
            await update.effective_message.reply_text(
                "❌ 赛季参数无效。用法：\n`/backfill` 当前赛季\n`/backfill 2024` 指定赛季\n`/backfill all` 全部可用赛季"
            )
            return
        progress = await update.effective_message.reply_text(
            f"⏳ 正在回填 {season} 赛季整季赛程，请稍候…"
        )
        try:
            result = await service.sync.sync_season(season)
        except Exception as exc:
            log.exception("/backfill %s 失败", season)
            await progress.edit_text(f"❌ {season} 赛季回填失败：{describe_error(exc)}")
            return
        received = result.get("received", 0)
        saved = result.get("saved", 0)
        finished = service.repo.count_finished_matches(service.sync.competition)
        await progress.edit_text(
            _season_result_text(season, received, saved, finished, result.get("message", ""))
        )
        return

    if target == "all":
        progress = await update.effective_message.reply_text("⏳ 正在查询账号可用赛季…")
        try:
            seasons = await service.api.get_available_seasons()
        except Exception as exc:
            log.exception("/backfill all 查询赛季失败")
            await progress.edit_text(f"❌ 查询可用赛季失败：{describe_error(exc)}")
            return
        if not seasons:
            await progress.edit_text("⚠️ 账号未返回任何可用赛季（可能套餐不支持历史赛季）。")
            return
        done: list[tuple[int, int, int]] = []
        for season in seasons:
            try:
                r = await service.sync.sync_season(season)
                done.append((season, r.get("received", 0), r.get("saved", 0)))
            except Exception as exc:  # noqa: BLE001 - 单个赛季失败不影响其余
                log.warning("赛季 %s 回填失败：%s", season, exc)
                done.append((season, 0, 0))
        finished = service.repo.count_finished_matches(service.sync.competition)
        total_saved = sum(x[2] for x in done)
        lines = "\n".join(
            f"{'✅' if s else '⚠️'} {sn} 赛季：收到 {rc} 保存 {sv}" for sn, rc, sv in done
        )
        await progress.edit_text(
            f"✅ 全赛季回填完成\n\n{lines}\n\n"
            f"💾 累计保存 {total_saved} 场 · 已完赛 {finished} 场"
        )
        return

    progress = await update.effective_message.reply_text("⏳ 正在拉取整季赛程（约 380 场），请稍候…")
    try:
        result = await service.sync.sync_full_season()
    except Exception as exc:
        log.exception("/backfill 失败")
        await progress.edit_text(f"❌ 拉取失败：{describe_error(exc)}")
        return

    received = result.get("received", 0)
    saved = result.get("saved", 0)
    # 已完赛（有比分）的场次才是强度榜的有效样本
    finished = service.repo.count_finished_matches(service.sync.competition)

    W = 12
    need = 60
    rows = [
        ("📥", "收到", f"{received} 场"),
        ("💾", "保存", f"{saved} 场"),
    ]
    if finished is not None:
        rows.append(("✅", "已完赛", f"{finished} 场（强度榜的有效样本）"))
    blocks = [
        ["✅ <b>整季赛程已入库</b>", BLANK],
        section("📦", "同步结果", *[kv_line(i, k, v, W) for i, k, v in rows]),
    ]
    if finished is not None:
        if finished >= need:
            # 进度条按 need 封顶：已达标时不该画一根溢出到 60 格的条
            gauge = f"<code>{hbar(min(finished / need, 1.0))} {finished}/{need}</code>"
            blocks.append(section("📊", "强度榜样本", gauge, "样本充足，强度榜已可正常展示。"))
        else:
            gauge = f"<code>{hbar(finished / need)} {finished}/{need}</code>"
            blocks.append(section(
                "📊", "强度榜样本", gauge,
                f"还差 {need - finished} 场，随着赛季推进会自动补齐。"))
    blocks.append(section(
        "ℹ️", "关于命中率",
        "预测命中率曲线仍需等机器人日常推送积累，本命令不会补。"))
    await progress.edit_text(normalize(section_join(blocks)), parse_mode="HTML")


# 回测可信门槛：预热场次 + 至少 20 场用于评估。
# 低于此数时只报数字、不给结论——小样本上的「准确率」是噪声，不是能力。
BACKTEST_MIN_EVAL = 20
BACKTEST_MIN_TOTAL = DEFAULT_MIN_HISTORY + BACKTEST_MIN_EVAL


# /backtest 可选的变体。默认 "poisson" —— 必须与线上 /predict 实际使用的
# 模型保持一致（service.py 调用 predict_match 时不传 elo_home_advantage，
# 即线上是纯 Maher/Poisson）。Elo / Dixon-Coles 只能显式指定，作为对照。
BACKTEST_VARIANTS = {"poisson": "纯泊松", "elo": "泊松+Elo", "dc": "泊松+DC"}


def _parse_backtest_variant(context) -> tuple[str, list[str]]:
    """解析 /backtest 的变体参数。返回 (变体, 被忽略的无法识别参数)。

    只认 args[0] 是不够的：手机端常把多条命令一起粘贴发送（例如
    「/backtest\\n/backtest elo」），此时 Telegram 会把整条消息当一条命令，
    args 变成 ['/backtest', 'elo']，只看首位就会静默退回默认值，用户以为
    自己指定了变体却拿到了默认结果。因此这里扫描全部参数，跳过以 `/`
    开头的 token，取第一个合法变体。

    无法识别的参数原样返回，由调用方显式提示——静默回退最容易误导。
    """
    args = [a for a in (getattr(context, "args", None) or []) if not str(a).startswith("/")]
    rejected = []
    for raw in args:
        token = (raw or "").strip().lower()
        if token in BACKTEST_VARIANTS:
            return token, rejected
        if token:
            rejected.append(token)
    return "poisson", rejected


async def backtest_cmd(update, context) -> None:
    """/backtest — 用本地库真实赛果做走前回测（零 API 开销）。

    与 /backfill 的分工：/backfill 只补赛程赛果入库；本命令用这些赛果
    **当场重演**模型在赛前的判断，再与真实比分对照。预测只用该场之前的
    历史，不存在前视偏差，因此是可信样本。

    默认评估纯泊松（与线上 /predict 同口径）；Elo / Dixon-Coles 需显式指定：
        /backtest          → 纯泊松（线上口径）
        /backtest elo      → 额外对比 泊松+Elo
        /backtest dc       → 额外对比 泊松+DC

    样本来源：库内已完赛 ＋ 镜像内置历史赛季（data/history/*.csv）。
    两者按「同日同对阵」去重，队 id 以库内官方 id 为准统一，
    保证走前回测能把跨赛季历史接续起来。

    局限（必须如实告知，不夸大）：
    · 内置历史目前只有英超三个赛季；
    · 内置历史的队 id 依赖库内队名反查，反查不到时会退回 slug 兜底，
      该队将视为新队（不影响正确性，只少一点历史）。
    """
    await _typing(update)
    progress = await update.effective_message.reply_text(
        "⏳ 正在用本地赛果回测（不消耗任何额度）…")
    service = context.application.bot_data["service"]
    competition = getattr(getattr(service, "sync", None), "competition", "") or ""

    try:
        matches, corpus_info = await asyncio.to_thread(
            load_corpus, service.repo, competition)
    except Exception as exc:
        log.exception("/backtest 读取本地赛果失败")
        await progress.edit_text(f"❌ 读取失败：{describe_error(exc)}")
        return

    W = 12
    total = len(matches)

    if total < BACKTEST_MIN_TOTAL:
        need = BACKTEST_MIN_TOTAL - total
        blocks = [
            ["🧪 <b>历史回测</b>", BLANK],
            section("⚠️", "样本不足", *[
                kv_line("📦", "本地已完赛", f"{total} 场", W),
                kv_line("🎯", "可信门槛", f"{BACKTEST_MIN_TOTAL} 场", W),
                kv_line("📉", "还差", f"{need} 场", W),
            ]),
            section("🛠", "如何补齐",
                    "执行 /backfill 拉取整季赛程（免费套餐包含当前赛季全部赛果）"),
            section("ℹ️", "为何不硬算",
                    f"不足 {BACKTEST_MIN_EVAL} 场评估样本时的「命中率」是噪声，"
                    "算出来也是假结论，宁可空着。"),
        ]
        await progress.edit_text(
            normalize(section_join(blocks)), parse_mode="HTML",
            disable_web_page_preview=True)
        return

    variant, rejected = _parse_backtest_variant(context)
    try:
        result = await asyncio.to_thread(
            run_backtest, matches, min_history=DEFAULT_MIN_HISTORY,
            variant=variant)
    except Exception as exc:
        log.exception("/backtest 回测失败")
        await progress.edit_text(f"❌ 回测失败：{describe_error(exc)}")
        return

    comp = result["comparison"]
    base, chal = comp["baseline"], comp["challenger"]
    n = chal["n"] or 0
    delta = comp.get("log_loss_delta")

    def _row(tag: str, d: dict) -> str:
        ll, acc = d.get("log_loss"), d.get("accuracy")
        ll_txt = f"{ll:.4f}" if ll is not None else "—"
        acc_txt = f"{acc:>3.0%}" if acc is not None else " —"
        return (f"<code>{pad_cjk(tag, 10)}</code>"
                f"<code>LL {ll_txt}</code> <code>命中 {acc_txt}</code>")

    def comparison_rows(b: dict, c: dict, var: str) -> list[str]:
        """默认（poisson）时基线与挑战者本就是同一模型，只列一行，
        避免输出两行相同数字造成误读；显式变体时才列双路。"""
        if var == "poisson":
            return [_row("纯泊松", b), "（＝线上 /predict 口径）"]
        return [_row("纯泊松", b), _row(BACKTEST_VARIANTS[var], c)]

    verdict = comp.get("verdict") or "无法判断"
    delta_txt = (f"{delta:+.4f}（正数＝挑战者更好）" if delta is not None else "—")

    blocks = [
        ["🧪 <b>历史回测</b>", BLANK],
        section("📦", "样本", *[
            kv_line("🗄", "已完赛", f"{total} 场", W),
            kv_line("🧱", "来源", f"库内 {corpus_info['db']} · 内置历史 "
                                  f"{corpus_info['history']}", W),
            kv_line("🎯", "计入评估", f"{n} 场", W),
            kv_line("🔥", "预热", f"{DEFAULT_MIN_HISTORY} 场（不计入评估）", W),
        ]),
        section("⚖️", "双路对比", *comparison_rows(base, chal, variant)),
        section("📉", "差异", *[
            kv_line("🔻", "Δ Log Loss", delta_txt, W),
            kv_line("🏁", "判定", verdict, W),
            kv_line("🧭", "校准误差", f"{chal.get('ece'):.4f}"
                    if chal.get("ece") is not None else "—", W),
        ]),
    ]

    mono = (result.get("challenger_levels") or {}).get("monotonic")
    if mono is True:
        blocks.append(section("🏅", "信心分级", "单调成立：高信心命中率 ≥ 中 ≥ 低"))
    elif mono is False:
        blocks.append(section("⚠️", "信心分级",
                              "不单调：高信心命中率未高于低信心，分级暂不可信"))

    if rejected:
        blocks.append(section("⚠️", "参数未识别",
                              f"已忽略：{'、'.join(rejected)}，按默认纯泊松执行。"
                              "可用变体：poisson / elo / dc"))

    blocks.append(section("ℹ️", "口径说明",
                          "样本＝库内赛果 ＋ 镜像内置历史赛季；"
                          "预测只使用该场之前的赛果，无前视偏差。"
                          f"当前变体：{BACKTEST_VARIANTS[variant]}"
                          "（默认纯泊松＝线上口径；/backtest elo 可对照 Elo）"))

    await progress.edit_text(
        normalize(section_join(blocks)), parse_mode="HTML",
        disable_web_page_preview=True)


async def _typing(update) -> None:
    """统一先发「正在输入」。发送失败不影响主流程——这只是体验优化。"""
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except Exception:
        pass


def register(dispatcher: CommandDispatcher) -> None:
    dispatcher.register("test", test_cmd, admin_only=True, description="立即推送一次预测")
    dispatcher.register("status", status_cmd, admin_only=True, description="运行状态诊断")
    dispatcher.register("diag", diag_cmd, admin_only=True, description="直连主源探活，显示真实失败原因")
    dispatcher.register("stats", stats_cmd, admin_only=True, description="命中率统计")
    dispatcher.register("storage", storage_cmd, admin_only=True, description="存储自检")
    dispatcher.register("backfill", backfill_cmd, admin_only=True,
                        description="拉取整季赛程，填充强度榜样本")
    dispatcher.register("sotprobe", sot_probe_cmd, admin_only=True,
                        description="探测单场技术统计（射正数）是否可用")
    dispatcher.register("backtest", backtest_cmd, admin_only=True,
                        description="用本地真实赛果回测（不消耗额度）")
