"""管理员指令：/test /status /stats /storage

权限由 dispatcher 统一拦截（admin_only=True），本模块不再重复判权。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import paths
from bot_handler import BotUI, esc
from formatkit import BLANK, SEP, hbar, kv_line, pad_cjk, section, section_join
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
    season_line = f"{settings.league_id} / {settings.season}"
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
        account = await api.get_account_status()
        sub, req = account.get("subscription") or {}, account.get("requests") or {}
        source_rows.append((
            "📋", "套餐",
            f"{esc(sub.get('plan', '未知'))}（{'有效' if sub.get('active') else '未激活或未知'}）",
        ))
        source_rows.append((
            "📊", "今日请求",
            f"{req.get('current', '?')} / {req.get('limit_day', '?')}",
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


async def backfill_cmd(update, context) -> None:
    """/backfill — 拉整季赛程入库，用于填充强度榜所需的历史样本。

    只补 **赛程与赛果**（matches 表），**不伪造预测记录**。
    理由：健康度曲线要反映模型「事前」的判断，而回填时用现在的积分榜去
    算过去的比赛会引入前视偏差（look-ahead bias），得出的命中率是虚高的
    假象。宁可曲线空着，也不要一条骗人的曲线。
    """
    await _typing(update)
    service = context.application.bot_data["service"]
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


async def _typing(update) -> None:
    """统一先发「正在输入」。发送失败不影响主流程——这只是体验优化。"""
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except Exception:
        pass


def register(dispatcher: CommandDispatcher) -> None:
    dispatcher.register("test", test_cmd, admin_only=True, description="立即推送一次预测")
    dispatcher.register("status", status_cmd, admin_only=True, description="运行状态诊断")
    dispatcher.register("stats", stats_cmd, admin_only=True, description="命中率统计")
    dispatcher.register("storage", storage_cmd, admin_only=True, description="存储自检")
    dispatcher.register("backfill", backfill_cmd, admin_only=True,
                        description="拉取整季赛程，填充强度榜样本")
