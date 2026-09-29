"""管理员指令：/test /status /stats /storage

权限由 dispatcher 统一拦截（admin_only=True），本模块不再重复判权。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import paths
from bot_handler import BotUI, esc
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
    season_line = f"联赛/赛季：{settings.league_id} / {settings.season}"
    if settings.season != settings.expected_season:
        season_line += (
            f"（⚠️ 按日期应为 {settings.expected_season}，请更新 SEASON 变量；"
            f"无赛程时会自动改用 {settings.expected_season}）")
    if service.season_in_use != settings.season:
        season_line += f"\n实际使用赛季：{service.season_in_use}（已自动降级）"

    lines = [
        "🤖 运行状态",
        f"版本：{version}",
        season_line,
        f"推送：每天 {settings.push_time:%H:%M}（{settings.timezone.zone}），"
        f"每次最多 {settings.max_matches} 场，窗口 {settings.lookahead_hours} 小时",
        f"下次推送：{next_run}",
        f"CHAT_ID：{'已配置' if settings.chat_id else '未配置'}",
        f"内存中的预测：{service.cached_predictions} 场",
        f"存储：{paths.summary()}",
        f"数据源渠道：{'官方直连 (API-Sports)' if settings.api_provider == 'apisports' else 'RapidAPI'}",
        f"数据源：{getattr(api, 'source_label', 'API-Football')}"
        + ("（备用源生效中）" if getattr(api, "using_fallback", False) else ""),
        f"备用源 football-data.org：{'已配置' if settings.football_data_available else '未配置'}",
    ]
    # 备用源实测：真实请求一次，把结果/原因显示出来，便于管理员自查账号与套餐
    if settings.football_data_available and getattr(api, "fallback", None):
        try:
            probe = await api.fallback.probe(settings.league_id)
            if probe.get("ok"):
                lines.append(f"备用源实测：✅ {probe['competition']} 共 {probe['count']} 场")
            else:
                lines.append(
                    f"备用源实测：❌ {probe.get('count', 0)} 场｜"
                    f"{probe.get('raw') or probe.get('detail') or '无数据'}")
        except Exception as exc:  # 诊断失败不影响状态页
            lines.append(f"备用源实测：⚠️ {describe_error(exc)}")
    try:
        account = await api.get_account_status()
        sub, req = account.get("subscription") or {}, account.get("requests") or {}
        lines.append(f"套餐：{sub.get('plan', '未知')}（{'有效' if sub.get('active') else '未激活或未知'}）")
        lines.append(f"今日请求：{req.get('current', '?')} / {req.get('limit_day', '?')}")
        lines.append("数据源连通：✅")
    except Exception as exc:
        # 数据源不可用时仍应显示版本与配置，否则连版本号都看不到
        lines.append(f"数据源连通：❌ {describe_error(exc)}")
    await update.effective_message.reply_text("\n".join(lines))


async def stats_cmd(update, context) -> None:
    """/stats — 命中率统计（基于落盘的预测记录）。"""
    service = context.application.bot_data["service"]
    await _typing(update)
    st = service.stats()
    rate = st["rate"]
    lines = [
        "📈 <b>预测命中率</b>",
        f"已结算：<code>{st['total']}</code> 场 · 命中 <code>{st['hit']}</code> 场"
        + (f" · 命中率 <code>{rate:.1%}</code>" if rate is not None else ""),
        f"待结算：<code>{st['pending']}</code> 场",
        f"存储：{'✅ 已落盘（重启不丢）' if st['persistent'] else '⚠️ 内存回退（重启会丢）'}",
    ]
    if st["total"] == 0:
        lines += ["", "暂无已结算的预测，赛果会在比赛结束后自动同步。"]
    else:
        streak = st["streak"]
        tail = f"连续命中 <code>{streak}</code>" if streak > 0 else (
            f"连续未中 <code>{-streak}</code>" if streak < 0 else "")
        if tail:
            lines.append(tail)
        if st["by_level"]:
            lines += ["", "按信心等级："]
            names = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低", "unknown": "未知"}
            for key in ("high", "medium", "low", "unknown"):
                slot = st["by_level"].get(key)
                if not slot:
                    continue
                r = slot["hit"] / slot["total"] if slot["total"] else 0
                lines.append(f"│ {names.get(key, key)}：<code>{slot['hit']}/{slot['total']}</code>（{r:.0%}）")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
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
    lines = [
        "💾 <b>存储状态</b>",
        f"数据目录：<code>{esc(paths.DATA_DIR)}</code>",
        f"数据库：<code>{esc(service.repo.db_path)}</code>",
        "",
        f"{mark(probe['write'])} 写入测试：{'通过' if probe['write'] else '失败'}",
        f"{mark(probe['read'])} 读取测试：{'通过（内容一致）' if probe['read'] else '失败'}",
        f"{mark(bool(tables))} 数据库：<code>{esc(', '.join(tables) or '无表')}</code>",
        f"{mark(mounted)} Volume 挂载：{'已生效（跨部署保留）' if mounted else '未确认——重新部署后再执行一次本命令'}",
        f"🕑 标记文件：<code>{age_text}</code>",
        f"🕑 最后写入：<code>{esc(last_write)}</code>",
        "",
        f"已预测：<code>{st['total'] + st['pending']}</code> 场 · 已结算 <code>{st['total']}</code> 场",
    ]
    if not probe["write"] or not probe["read"]:
        lines += ["", "⚠️ 写入/读取失败，数据留在容器临时目录，<b>重新部署会丢失</b>。"]
    elif not mounted:
        lines += [
            "",
            "ℹ️ 首次执行属正常；请重新部署后再执行一次，若标记时间仍在即 Volume 生效。",
            "未确认挂载前，预测与命中率数据<b>可能在重新部署后清空</b>。",
        ]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


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
