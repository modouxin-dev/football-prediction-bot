#!/usr/bin/env python3
"""Web 看板 API 桥接层 / API bridge for the web dashboard.

只做三件事：健康检查、统计读数、把 service 的能力暴露成 HTTP。
刻意保持极简——它不复制业务逻辑，全部转发给 service / repository。

为什么分成两个 requirements 文件：
    机器人本体（Telegram 长轮询 + 定时任务）完全不需要 FastAPI。
    把它写进 requirements.txt 会让只跑机器人的环境平白增大，
    且引入一个不用的攻击面。因此 FastAPI 单独放在 requirements-web.txt。

    但注意：Dockerfile 两个文件**都会安装**（`pip install -r requirements.txt
    && pip install -r requirements-web.txt`）。因为看板必须与机器人跑在
    同一个容器里 —— Railway 的 Volume 无法跨服务共享，拆成两个服务后
    看板会拿到一个空卷，读不到机器人写的 football.db。所以"不写入
    requirements.txt"是指不混进机器人依赖，不是指不进镜像。

启动：
    pip install -r requirements-web.txt
    python -m uvicorn api:app --host 0.0.0.0 --port 8080

    curl http://127.0.0.1:8080/health
    curl http://127.0.0.1:8080/stats
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("api")

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse
except ImportError as exc:  # pragma: no cover - 未装可选依赖时给出明确指引
    raise SystemExit(
        "缺少 Web 依赖。请先执行：pip install -r requirements-web.txt"
    ) from exc

import analytics  # noqa: E402
from templates import resolve_league  # noqa: E402
import joblog  # noqa: E402
import paths  # noqa: E402
import runtime_info  # noqa: E402
from repository import PredictionRepository  # noqa: E402

# 默认日期必须按项目时区（默认 Asia/Shanghai）算，不能用 UTC。
# 反例：上海 10-05 07:00，UTC 仍是 10-04 23:00 —— 用 UTC 取「今天」
# 会返回昨天的赛程，看板与 Telegram 推送差一整天。
_TZ_NAME = os.getenv("TIMEZONE", "Asia/Shanghai")


def _today() -> str:
    """按项目时区返回今天日期（YYYY-MM-DD）。时区无效时退化为 UTC。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(_TZ_NAME)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _league_flag(code: str | None) -> str:
    """联赛旗帜字符，供看板徽标用。未登记返回 ⚽（不返回空、不猜）。"""
    try:
        return resolve_league(code)[0]
    except Exception:
        return "⚽"


def _league_short(code: str | None) -> str:
    """联赛短名（如「西甲」）。未登记时退回全称。"""
    try:
        name = resolve_league(code)[1]
    except Exception:
        return _league_name(code)
    return name or _league_name(code)


def _now_iso() -> str:
    """当前时刻 ISO8601（UTC，带 Z 语义的 +00:00）。

    实时数据必须带时间戳：没有时间戳的「实时比分」无法判断是否过期，
    网络中断时页面会一直显示旧数字却看起来像在跳动。
    """
    return datetime.now(timezone.utc).isoformat()


# templates 是零依赖模块（只放静态文案与联赛名表），可以安全地在顶层引入。
# 刻意**不在顶层** import formatkit：它依赖 service，实测导入耗时 1192ms，
# 会直接压垮首请求。改为在 lifespan 预热时绑定一次（见 _team_cn）。
from templates import LEAGUE_NAMES  # noqa: E402

# 联赛 ID → 竞赛代码（用于给统计端点一个「当前联赛」默认值）。
# 单独 try 包一层：football_data 依赖 httpx，若将来被拆成可选依赖，
# 看板仍应能启动，只是拿不到默认联赛名，退化为「不过滤」而非崩溃。
try:
    from football_data import LEAGUE_ID_TO_CODE
except Exception:  # pragma: no cover - 防御性
    LEAGUE_ID_TO_CODE = {}


def _default_competition() -> str:
    """当前配置联赛对应的竞赛代码（英超 → PL）。

    本地库可能同时存着其他竞赛的比赛（例如早期同步混入的英冠赛果）。
    统计端点若不按竞赛过滤，强度榜会把它们一起算进来，
    导致 Hull City、Ipswich Town 这类英冠球队出现在英超榜单里。
    """
    try:
        league_id = int(os.getenv("LEAGUE_ID", "39") or 39)
    except ValueError:
        return ""
    return LEAGUE_ID_TO_CODE.get(league_id, "")


# 指令清单在启动时构建一次并缓存。
# 实测：若在每个请求里现算，首次调用要导入 commands → bot_handler → chart →
# matplotlib，耗时约 800ms，远超 200ms 预算。启动时预热后，首请求即为毫秒级。
_COMMAND_CACHE: list[dict] | None = None

# 中文队名转换函数，启动时绑定一次（见 lifespan）。
# 为什么要绕一层：formatkit 依赖 service，实测导入 1192ms。若在请求里现导入，
# 首请求必定超时；若不导入而在本文件重写一份归一化逻辑，又会与 Telegram 侧
# 出现两套规则（去重音、剥 FC 词缀），改一处漏一处。预热绑定两全。
_TEAM_CN = None


def _cn(raw: str | None) -> str:
    """队名转中文短名；未收录或尚未预热时返回原名，绝不猜测、绝不编造。"""
    text = (raw or "").strip()
    if not text:
        return "?"
    if _TEAM_CN is None:
        return text
    try:
        return _TEAM_CN(text)
    except Exception:  # pragma: no cover - 单个队名转换失败不应拖垮整个响应
        return text


def _league_name(code: str | None) -> str:
    """本地库分区代码 → 中文联赛名。

    库里的 competition_code 有两种写法，来源不同：
      - 已收录联赛走 football_data 映射，存字母码（39 → "PL"）
      - 未收录联赛 fallback 成数字字符串（阿甲 128 → "128"）
    两种都要能翻回中文，否则多联赛看板会有半数是裸码。
    """
    text = (code or "").strip()
    if not text:
        return "未分类"
    if text.isdigit():
        return LEAGUE_NAMES.get(int(text)) or f"联赛 {text}"
    for league_id, mapped in LEAGUE_ID_TO_CODE.items():
        if mapped == text:
            return LEAGUE_NAMES.get(league_id) or text
    return text


def _commands_payload() -> list[dict]:
    global _COMMAND_CACHE
    if _COMMAND_CACHE is None:
        from commands import build_dispatcher

        _COMMAND_CACHE = [
            {"name": s.name, "admin_only": s.admin_only, "description": s.description}
            for s in build_dispatcher().specs
        ]
    return _COMMAND_CACHE


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时预热重量级导入，避免把 800ms 的导入开销转嫁给第一个用户。"""
    try:
        _commands_payload()
    except Exception as exc:  # 预热失败不能阻止服务启动
        log.warning("指令清单预热失败：%s", exc)
    global _TEAM_CN
    try:
        from formatkit import team_short_name

        _TEAM_CN = team_short_name
    except Exception as exc:  # 拿不到就退化成英文原名，绝不中断启动
        log.warning("中文队名未启用：%s", exc)
    yield


app = FastAPI(
    title="Football Prediction Bot API",
    description="只读桥接层：把机器人落盘的预测与命中率暴露给 Web 看板。",
    version="1.0.0",
    lifespan=lifespan,
)

_VERSION = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "unknown")[:7]
_DB_PATH = str(paths.DB_PATH)
# 看板页面：单文件、零外部依赖（无 CDN），保证离线/内网环境也能渲染
_STATIC_DIR = Path(__file__).resolve().parent / "web"
_INDEX = _STATIC_DIR / "index.html"


def _repo() -> PredictionRepository:
    return PredictionRepository(_DB_PATH)


def _active_competitions() -> tuple[str, ...] | None:
    """看板要显示的联赛分区代码，来自配置的 LEAGUE_IDS。

    看板与自动预测必须看同一批比赛：预测只跑配置的联赛，看板若把本地库
    里所有比赛都列出来（含历史同步残留的未配置联赛），那些比赛永远不会
    有预测，每条都恒显示「尚未生成预测」。

    返回 None 表示**不过滤**（配置读取失败或映射不出任何代码）。宁可多
    显示，也不能因为映射表缺项把配置内的联赛也过滤掉——那会让看板整页
    空白，比多显示几场无关比赛严重得多。
    """
    try:
        from config import load_settings
        from football_data import LEAGUE_ID_TO_CODE

        settings = load_settings()
        codes = tuple(
            LEAGUE_ID_TO_CODE[int(lid)]
            for lid in settings.league_ids
            if int(lid) in LEAGUE_ID_TO_CODE
        )
        return codes or None
    except Exception as exc:  # 拿不到就不过滤，绝不中断看板
        log.warning("看板联赛过滤未启用：%s", exc)
        return None


@app.get("/")
async def index():
    """看板页面。

    显式禁用缓存：index.html 是单文件内联脚本，改动后若被浏览器或中间
    代理缓存住，用户拿到的仍是旧脚本——页面会一直卡在「正在加载数据…」
    而服务端早已修好，现场表现为「明明部署了却还是打不开」。
    """
    if not _INDEX.exists():  # pragma: no cover - 打包遗漏时给出明确提示
        raise HTTPException(status_code=500, detail=f"缺少静态页面：{_INDEX}")
    return FileResponse(
        _INDEX,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/health")
async def health() -> JSONResponse:
    """健康与持久化状态。

    `persistent=False` 意味着数据目录不在持久卷上——Web 与机器人共用同一个
    数据库，因此这个字段对两者同样重要。
    """
    warn = paths.persistence_warning()
    payload = {
        "status": "ok",
        "version": _VERSION,
        "db_path": _DB_PATH,
        "persistent": not bool(warn),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # 下面三项用于回答「这个实例到底是不是新部署的那一个」：
        # version 只能说明代码版本，区分不了「旧容器还在跑同一个 commit」。
        # 同一份代码部署到多处（Railway + Fly）时，platform + started_at
        # 是唯一能把它们区分开的依据。
        "platform": runtime_info.platform(),
        "started_at": runtime_info.started_at().isoformat(),
        "uptime_seconds": int(runtime_info.uptime_seconds()),
    }
    if warn:
        payload["warning"] = warn
    return JSONResponse(payload)


@app.get("/stats")
async def stats() -> JSONResponse:
    """命中率统计（与 Telegram 的 /stats 读同一份数据）。"""
    repo = _repo()
    try:
        data = repo.stats()
    except Exception as exc:
        log.exception("/stats 读取失败")
        raise HTTPException(status_code=500, detail=f"读取统计失败：{exc}") from exc
    by_level = {
        key: {
            "total": slot["total"],
            "hit": slot["hit"],
            "rate": (slot["hit"] / slot["total"]) if slot["total"] else None,
        }
        for key, slot in (data.get("by_level") or {}).items()
    }
    return JSONResponse({
        "status": "ok",
        "version": _VERSION,
        "settled": data.get("total", 0),
        "hit": data.get("hit", 0),
        "rate": data.get("rate"),
        "pending": data.get("pending", 0),
        "streak": data.get("streak", 0),
        "persistent": data.get("persistent", False),
        "by_level": by_level,
    })


@app.get("/stats/period")
async def stats_period(days: str = "7,30") -> JSONResponse:
    """分时间窗口的胜率 / 平均赔率 / ROI（周 = 7 天，月 = 30 天）。

    为什么三个数字一起给：只看命中率会被高赔冷门误导（30% 命中率
    押 5.0 的冷门是赚的，押 1.2 的热门是亏的）。ROI 把两者合起来，
    是唯一能直接回答「这套推荐到底赚不赚」的指标。

    无赔率的场次（老记录）从 ROI 中剔除并单独报 ``no_odds``，
    不做平均赔率填充——填充会虚报 ROI。
    """
    try:
        wanted = [int(x) for x in str(days).split(",") if str(x).strip().isdigit()]
    except ValueError:
        wanted = [7, 30]
    wanted = [w for w in wanted if 0 < w <= 365] or [7, 30]
    repo = _repo()
    out = {}
    try:
        for d in wanted:
            out[str(d)] = repo.period_stats(d)
    except Exception as exc:
        log.exception("/stats/period 计算失败")
        raise HTTPException(status_code=500, detail=f"计算失败：{exc}") from exc
    return JSONResponse({"status": "ok", "version": _VERSION, "periods": out})


@app.get("/health/model")
async def model_health() -> JSONResponse:
    """模型健康度：Log Loss 趋势、校准曲线、各信心等级命中率。

    附带 stale_pending（超期未结算数）：这些行已开赛但超出回溯窗口，
    永远结算不了。它们被 pending() 的下界挡在结算之外、不再堵塞队列，
    但**数量本身就是故障信号**——要么是赛季切换后失效的旧 ID，
    要么是某几天赛果没拉回来。此前这个数字算得出来却无人展示，
    等于没有。
    """
    try:
        data = analytics.model_health(_DB_PATH)
    except Exception as exc:
        log.exception("/health/model 计算失败")
        raise HTTPException(status_code=500, detail=f"计算失败：{exc}") from exc
    stale = None
    try:
        stale = _repo().count_stale_pending()
    except Exception:  # noqa: BLE001 - 计数是旁路，读不到不能拖垮健康度
        log.debug("读取超期未结算数失败")
    return JSONResponse({
        "status": data["status"], "version": _VERSION,
        "stale_pending": stale, **data,
    })


@app.get("/audit")
async def audit(limit: int = 50) -> JSONResponse:
    """预测审计：历史预测 → 实际赛果 → 命中与否。

    中文字段以 *_cn 形式**追加**而非替换：原名 home/away 仍保留，
    前端已有渲染逻辑不会因此失效。
    """
    limit = max(1, min(int(limit), 200))  # 防止超大 limit 拖慢响应
    try:
        data = analytics.prediction_audit(_DB_PATH, limit=limit)
    except Exception as exc:
        log.exception("/audit 读取失败")
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}") from exc
    for item in data.get("items") or []:
        item["home_cn"] = _cn(item.get("home"))
        item["away_cn"] = _cn(item.get("away"))
    return JSONResponse({"version": _VERSION, **data})


@app.get("/strength")
async def strength(competition: str | None = None) -> JSONResponse:
    """球队强度榜（由本地赛果重建，不联网）。

    默认按当前配置联赛过滤；显式传 ?competition=ELC 可看其他竞赛，
    传 ?competition=（空）才表示不过滤、统计全部竞赛。
    """
    comp = _default_competition() if competition is None else competition
    try:
        data = analytics.strength_table(_DB_PATH, competition=comp)
    except Exception as exc:
        log.exception("/strength 计算失败")
        raise HTTPException(status_code=500, detail=f"计算失败：{exc}") from exc
    for team in data.get("teams") or []:
        team["name_cn"] = _cn(team.get("name"))
    data["competition_cn"] = _league_name(comp)
    return JSONResponse({"version": _VERSION, **data})


@app.get("/fixtures")
async def fixtures(date: str | None = None, days: int = 1, limit: int = 100) -> JSONResponse:
    """赛程 + 已生成的预测，按联赛分组，可一次取多天。

    days=1 今天，3/7 表示含今天在内的未来几天。之所以要支持多天：联赛并非
    每天都有比赛，只看「今天」时常整页空白，容易被误认为看板坏了。
    日期格式 YYYY-MM-DD，默认今天（按 TIMEZONE 时区，默认 Asia/Shanghai）。

    每支球队额外给出**近期战绩与实力分**：
      · 近期战绩只统计区间**起始日之前**已完赛的比赛，因此区间内尚未开踢的
        场次不会被算进任何一队的状态（无前视偏差）。
      · 实力分/排名来自 elo_ratings，与 Telegram 实力榜同源。
      · 两者都可能为 null（库未回填 / Elo 未计算）。此时前端显示「暂无」，
        绝不能把「没有数据」显示成 0 分，否则看起来像一支极弱的队。

    未生成预测的比赛 `prediction` 为 null —— 表示「还没算」而非「算出来是 0」，
    前端必须区分这两种状态。
    """
    day = (date or "").strip() or _today()
    days = max(1, min(int(days), 14))
    limit = max(1, min(int(limit), 300))
    try:
        start = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD") from None
    day_list = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    repo = _repo()
    comps = _active_competitions()
    rows: list[dict[str, Any]] = []
    try:
        for d in day_list:
            for r in repo.matches_with_predictions(d, limit=limit, competitions=comps):
                rows.append({**r, "date": d})
    except Exception as exc:
        log.exception("/fixtures 读取失败")
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}") from exc

    # 每个联赛只查一次 Elo / 战绩，避免逐场比赛打库。
    meta: dict[str, dict[str, Any]] = {}
    for comp in sorted({r.get("competition") or "" for r in rows}):
        if not comp:
            continue
        ratings = {str(k): float(v) for k, v in repo.elo_ratings(comp).items()}
        rank = {
            str(tid): i
            for i, (tid, _) in enumerate(
                sorted(ratings.items(), key=lambda kv: kv[1], reverse=True), 1
            )
        }
        meta[comp] = {"ratings": ratings, "rank": rank,
                      "form": repo.recent_form(comp, day_list[0])}

    def side(comp: str, team_id: object) -> dict[str, Any]:
        m = meta.get(comp, {})
        tid = str(team_id) if team_id else ""
        info = m.get("form", {}).get(tid) or {}
        rating = m.get("ratings", {}).get(tid)
        return {
            "form": info.get("form"),
            "w": info.get("w"), "d": info.get("d"), "l": info.get("l"),
            "gf": info.get("gf"), "ga": info.get("ga"),
            "rating": round(float(rating), 1) if rating is not None else None,
            "rank": m.get("rank", {}).get(tid),
            "rank_total": len(m.get("ratings", {})),
        }

    groups: dict[str, list[dict]] = {}
    for r in rows:
        comp = r.get("competition") or ""
        groups.setdefault(comp, []).append({
            "fixture_id": r["fixture_id"],
            "date": r["date"],
            "kickoff": r["kickoff"],
            "matchday": r.get("matchday"),
            "status": r["status"],
            "home": r["home"],
            "away": r["away"],
            "home_cn": _cn(r["home"]),
            "away_cn": _cn(r["away"]),
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "actual_home": r.get("actual_home"),
            "actual_away": r.get("actual_away"),
            "prediction": r.get("prediction"),
            "home_team": side(comp, r.get("home_id")),
            "away_team": side(comp, r.get("away_id")),
        })
    payload = [
        {
            "competition": code,
            "competition_cn": _league_name(code),
            # 徽标用「旗帜 + 短名」而非外链图片：库里没有存 logo URL，
            # 外链要额外请求且可能失效；旗帜是纯字符，零成本、零失效。
            "competition_flag": _league_flag(code),
            "competition_short": _league_short(code),
            "count": len(items),
            "matches": items,
        }
        for code, items in sorted(groups.items())
    ]
    total = sum(g["count"] for g in payload)
    message = ""
    if not total:
        try:
            empty_db = repo.matches_count() == 0
        except Exception:  # pragma: no cover - 库不可用时按「未回填」提示
            empty_db = False
        message = (
            "库里还没有任何赛程：首次自动回填在机器人启动约 10 分钟后开始，请稍后刷新。"
            if empty_db else
            f"{day_list[0]} ～ {day_list[-1]} 区间内没有比赛，可把范围放宽到 7 天试试。"
        )
    return JSONResponse({
        "status": "ok" if total else "empty",
        "version": _VERSION,
        "date": day,
        "days": days,
        "date_from": day_list[0],
        "date_to": day_list[-1],
        "total": total,
        "leagues": payload,
        "message": message,
    })


@app.get("/jobs")
async def jobs(limit: int = 20) -> JSONResponse:
    """定时任务执行状态：每个 job 最近一次是成功还是失败。

    为什么需要这个端点：此前 7 个 job 的失败只写日志，机器人照常推送、
    看板显示旧数据，用户以为一切正常。落进 job_runs 表后，
    「回测到底跑没跑」变成看板上一行字，而不是一次日志考古。

    status 语义：
        ok   —— 最近一次执行成功
        fail —— 最近一次失败（message 为原因摘要）
        skip —— 主动跳过（无样本 / 未启用），**不是故障**
        unknown —— 从未记录（尚未到首次执行时间，或 job 未注册）
    """
    limit = max(1, min(int(limit), 100))
    try:
        last = joblog.last_status(_DB_PATH)
        recent = joblog.recent(_DB_PATH, limit=limit)
    except Exception as exc:
        log.exception("/jobs 读取失败")
        raise HTTPException(status_code=500, detail=f"读取任务状态失败：{exc}") from exc
    return JSONResponse({
        "version": _VERSION,
        "status": "ok" if last else "empty",
        "count": len(last),
        "last": last,
        "recent": recent,
        "message": (
            "" if last else
            "尚无任务记录。定时任务按各自间隔启动，首次生效在启动后数分钟。"
        ),
    })


@app.get("/quota")
async def quota() -> JSONResponse:
    """当日额度消耗：主源自报 + 本地按任务明细。

    为什么两者都要：
        主源自报的「剩余次数」是权威值，但它只在**发过请求之后**才有读数，
        且答不了「是谁在用」。本地按任务计数正好补上这一半——
        额度打光时要能一眼看出该停哪个任务，而不是靠猜。

    quota_left 为 None 表示尚未观测到（进程刚重启 / 还没发过请求），
    此时**不猜、不填 0**：填 0 会立刻触发熔断，把整个系统停掉。
    """
    try:
        from repository import quota_tasks_today, quota_today
        by_source = quota_today()
        by_task = quota_tasks_today()
    except Exception as exc:
        log.exception("/quota 读取失败")
        raise HTTPException(status_code=500, detail=f"读取额度失败：{exc}") from exc

    left = None
    remaining_raw = None
    try:
        # 看板进程有自己的 client（--mode web 时机器人侧连接并未建立），
        # 因此这里读的是**本进程**观测到的剩余次数。它只在自己发过请求后
        # 才有值；机器人进程的读数写不进来（两个进程不共享内存）。
        client = _live_client()
        if client and client is not True and client is not False:
            primary = getattr(client, "primary", None) or client
            left = getattr(primary, "quota_left", None)
            remaining_raw = getattr(primary, "quota_remaining", None)
    except Exception:  # noqa: BLE001 - 额度是旁路信息，读不到不影响其它字段
        left = None

    total = sum(int(v) for v in by_source.values())
    ranked = sorted(by_task.items(), key=lambda kv: kv[1], reverse=True)
    return JSONResponse({
        "version": _VERSION,
        "status": "ok",
        "day": _now_iso()[:10],
        "total": total,
        "by_source": by_source,
        "by_task": dict(ranked),
        "top_task": (ranked[0][0] if ranked else None),
        "quota_left": left,
        "quota_remaining": remaining_raw,
        "note": (
            "quota_left 为主源上报的剩余次数，只有本进程自己发过请求才有读数；"
            "为 null 表示尚未观测到（不代表余额未知或已耗尽）。"
            "by_source / by_task 来自共享数据库，是机器人与看板的合并计数。"
        ),
    })


@app.get("/backtest")
async def backtest(limit: int = 20, competition: str | None = None) -> JSONResponse:
    """历史回测结论（新→旧），用于观察指标随样本积累的变化。

    数据来自 backtest_runs 表——由 backtest_job 每日跑完 Walk-Forward
    回测后落盘。表为空时返回 status=empty，**不**编造任何数字：
    看板必须能区分「还没跑过」和「跑出来是 0」。

    Walk-Forward 的含义：逐场只用该场**之前**的数据做预测，因此不存在
    前视偏差；这里的准确率与 Telegram 推送口径同源。
    """
    comp = _default_competition() if competition is None else competition
    limit = max(1, min(int(limit), 100))
    repo = _repo()
    try:
        items = repo.backtest_history(comp, limit=limit)
    except Exception as exc:
        log.exception("/backtest 读取失败")
        raise HTTPException(status_code=500, detail=f"读取回测结论失败：{exc}") from exc
    return JSONResponse({
        "version": _VERSION,
        "status": "ok" if items else "empty",
        "competition": comp,
        "competition_cn": _league_name(comp),
        "count": len(items),
        "items": items,
        "message": (
            "" if items else
            "尚无回测记录。backtest_job 每日自动跑一次并落盘，"
            "首次生效约在启动 30 分钟后。"
        ),
    })


@app.get("/commands")
async def commands() -> JSONResponse:
    """暴露指令清单，供 Web 看板自动生成导航（启动时已预热，响应为毫秒级）。"""
    return JSONResponse({"status": "ok", "commands": _commands_payload()})


# ── 实时赛况 ---------------------------------------------------------------
# 进行中比赛的数据不在本地库里（本地库由 sync_job 每 6 小时刷新，
# 拿它当实时源等于把最迟 6 小时前的比分说成「现在」），必须直连主源。

# 单场实时最多并发查询多少场。20 场同时进行时若全部展开详情，
# 单轮就是 20×3=60 次请求，几分钟就能烧掉一天额度。
LIVE_MAX_MATCHES = 8
_LIVE_CLIENT: Any = None


def _live_client() -> Any:
    """懒加载主源客户端（单例）。

    为什么不在 lifespan 里建：看板可能单独以 --mode web 启动，
    那时机器人侧的连接根本没建立；而 httpx.AsyncClient 每次请求
    新建会在 TIME_WAIT 上堆积。单例 + 懒加载两头都兼顾。
    """
    global _LIVE_CLIENT
    if _LIVE_CLIENT is not None:
        return _LIVE_CLIENT
    try:
        from config import load_settings
        from data_source import DataSourceRouter

        settings = load_settings()
        if not getattr(settings, "api_key", None):
            _LIVE_CLIENT = False  # 未配置：标记为不可用，避免每次重试
            return _LIVE_CLIENT
        from api_client import FootballAPI

        # 看板进程同样会发真实请求（实时赛况），此前未接 quota_sink，
        # 这部分消耗完全不进本地账本 —— 这正是「API 侧 3360 / 本地实测 280」
        # 对不上的机制之一。与 main.py 的机器人侧保持一致地记账。
        try:
            from repository import bump_quota as _dash_bump_quota
            _LIVE_CLIENT = DataSourceRouter(
                FootballAPI(settings.api_key, provider=settings.api_provider),
                quota_sink=_dash_bump_quota)
        except Exception:  # 计数不可用时看板功能不受影响
            _LIVE_CLIENT = DataSourceRouter(
                FootballAPI(settings.api_key, provider=settings.api_provider))
    except Exception as exc:  # 客户端不可用绝不能让看板整页 500
        log.warning("实时客户端不可用：%s", exc)
        _LIVE_CLIENT = False
    return _LIVE_CLIENT


# 进行中状态的 short 枚举（API-Football）
# 1H/2H = 上下半场，HT = 中场，ET/BT/P = 加时/加时中场/点球，LIVE = 通用进行中
LIVE_STATUS = {"1H", "2H", "HT", "ET", "BT", "P", "LIVE"}
LIVE_CN = {"1H": "上半场", "2H": "下半场", "HT": "中场休息",
           "ET": "加时", "BT": "加时中场", "P": "点球大战", "LIVE": "进行中"}


def _pick_live(raw: list[dict]) -> list[dict]:
    """筛出真正进行中的比赛，按开赛时间正序。"""
    out = []
    for fx in raw or []:
        short = ((fx.get("fixture") or {}).get("status") or {}).get("short")
        if short in LIVE_STATUS:
            out.append((str((fx.get("fixture") or {}).get("date") or ""), fx))
    out.sort(key=lambda item: item[0])
    return [fx for _, fx in out]


def _norm_lineups(raw: list[dict]) -> list[dict]:
    """阵容规范化成前端直接可渲染的结构。

    原始响应的键是驼峰 `startXI`，球员信息又埋在 `player` 子对象里；
    直接把原始结构丢给前端，渲染层就要懂 API-Football 的字段命名，
    等于把外部 API 的形状泄漏进了展示层。这里一次性摊平。

    只取首发（startXI）：替补名单对「现在场上是谁」没有意义，
    且一场 20+ 人全塞进响应，多场叠加后体积会明显变大。
    """
    out: list[dict] = []
    for item in raw or []:
        team = item.get("team") or {}
        xi = []
        for entry in item.get("startXI") or item.get("start_xi") or []:
            player = entry.get("player") or entry
            name = player.get("name")
            if name:
                xi.append({"name": name, "pos": player.get("pos")})
        out.append({
            "team": team.get("name"),
            "team_cn": _cn(team.get("name")),
            "formation": item.get("formation"),
            "start_xi": xi,
        })
    return out


def _norm_injuries(raw: list[dict]) -> list[dict]:
    """伤停按队分组。

    原始响应是「一名球员一条」，同一个队会在列表里重复出现多次；
    直接渲染会变成一长串看不出归属的名单。这里聚合成
    「一队一条、队下挂球员」。
    """
    grouped: dict[str, dict] = {}
    for item in raw or []:
        team = item.get("team") or {}
        name = team.get("name") or "?"
        slot = grouped.setdefault(
            name, {"team": name, "team_cn": _cn(name), "players": []})
        player = item.get("player") or {}
        pname = player.get("name")
        if pname:
            slot["players"].append({
                "name": pname,
                "reason": player.get("reason") or player.get("type"),
            })
    return list(grouped.values())


def _norm_odds(raw: Any, fid: str | None = None) -> dict | None:
    """赔率取各公司中位数，并给出市场隐含概率。

    只报单一公司的报价会被异常值带偏；中位数抗离群。
    隐含概率必须去水（overround），否则三个概率相加恒 > 1，
    用户会算出「主+平+客 = 105%」这种看不懂的数。
    """
    if not raw:
        return None
    try:
        from analyzer import collect_1x2_odds, consensus_odds, implied_probabilities

        rows = collect_1x2_odds(raw)
        cons = consensus_odds(rows)
    except Exception:  # 赔率只是附加信息，解析失败不应影响整场展示
        return None

    # 顺手把亚洲盘的真实选项落盘（**零额外请求**）。
    #
    # 为什么在这里做：亚盘解析必须基于实测的 value 写法，而 raw 就是
    # /odds 的完整响应，已经在内存里了——再单独调一次就是纯浪费额度。
    #
    # 为什么需要它：凭记忆猜 bet id / 选项写法会导致解析永远取不到值，
    # 而取不到值是**静默**的（不报错、不告警，只是推荐里没有亚盘）。
    # 落盘后管理员打开 /oddsprobe 端点即可核对真实写法。
    #
    # 失败一律静默：这是诊断设施，绝不能拖累赔率展示主流程。
    try:
        import oddsprobe_log
        from analyzer import ASIAN_HANDICAP_BET_ID
        bets = []
        for item in raw or []:
            for book in (item.get("bookmakers") or [])[:3]:   # 3 家足够看清写法
                for bet in book.get("bets") or []:
                    if bet.get("id") == ASIAN_HANDICAP_BET_ID:
                        bets.append(bet)
                        break
            if bets:
                break
        if bets:
            oddsprobe_log.record_bets(fid or "?", bets[:3])
    except Exception:
        pass
    if not cons:
        return None
    probs = implied_probabilities(cons) or {}
    return {
        "home": cons.get("home"), "draw": cons.get("draw"), "away": cons.get("away"),
        "n_bookmakers": cons.get("n"),
        "implied": {k: round(v, 4) for k, v in probs.items()},
    }


def _live_shape(fx: dict, detail: dict) -> dict:
    """把单场比赛整理成前端直接可渲染的结构。

    缺字段一律给 None 而不是 0：把「没拿到」显示成 0 会被读成
    「拿到了，值是 0」（比分 0-0 与未知是两回事）。
    """
    info = fx.get("fixture") or {}
    goals = fx.get("goals") or {}
    teams = fx.get("teams") or {}
    league = fx.get("league") or {}
    status = info.get("status") or {}
    return {
        "fixture_id": info.get("id"),
        "league": league.get("name"),
        # 用数字 id 查中文名：_league_name 对数字串查 LEAGUE_NAMES，
        # 而 league.code 是 API-Football 自己的码，与本地映射表未必同源。
        "league_cn": _league_name(str(league.get("id") or "")),
        "league_flag": _league_flag(str(league.get("id") or "")),
        "elapsed": status.get("elapsed"),
        "status_short": status.get("short"),
        "status_cn": LIVE_CN.get(status.get("short"), status.get("short") or ""),
        "home": (teams.get("home") or {}).get("name"),
        "away": (teams.get("away") or {}).get("name"),
        "home_cn": _cn((teams.get("home") or {}).get("name")),
        "away_cn": _cn((teams.get("away") or {}).get("name")),
        "home_score": goals.get("home"),
        "away_score": goals.get("away"),
        "lineups": _norm_lineups(detail.get("lineups") or []),
        "injuries": _norm_injuries(detail.get("injuries") or []),
        "odds": _norm_odds(detail.get("odds")),
        "errors": detail.get("errors") or {},
    }


async def _cached_fetch(key: str, kind: str, fetch_factory, *, allow_stale: bool = True,
                        should_cache=None):
    """先查缓存，未命中才打外网，外网失败时退回过期缓存。

    为什么必须拦截在端点之前：
      看板是多人共用的，十个人先后点同一场比赛就发十次一模一样的请求，
      而这些数据几分钟内根本不会变。额度是按天结算的硬上限，打光后整套
      系统停摆——不该让「刷新页面」这种只读动作具备耗尽额度的能力。

    返回 (value, fetched_at, stale, error)：
      value      数据；None 表示这次确实没拿到
      fetched_at **实际拉取时刻**（不是当前时刻）——缓存命中时返回旧时间戳，
                 这样前端能如实显示「截至 10:32」，而不是假装刚取到
      stale      是否使用了过期缓存（True 时必须提示用户数据可能滞后）
      error      拉取失败原因；缓存命中时为 None

    fetch_factory 是**工厂而非协程**：缓存命中时根本不该创建协程，
    否则每次都会留下一个 never-awaited 的协程对象（运行时告警 + 资源泄漏）。

    allow_stale=False 用于不允许糊弄的场合（如比分），此时过期即视为无缓存。

    should_cache(value) → bool，返回 False 时不写入缓存。
    用于「空结论」——见 /live：没比赛这个事实随时可能变，缓存它会让
    「比赛刚开始」最多延迟一个 TTL 才显示，而省下的只是每人一次请求，
    明显不划算。
    """
    repo = _repo()
    try:
        payload, fetched, _ = repo.cache_read(key, allow_stale=False)
    except Exception as exc:
        log.warning("读缓存异常（key=%s）：%s", key, exc)
        payload, fetched = None, None
    if payload is not None:
        return payload, fetched, False, None

    try:
        value = await fetch_factory()
        written_at = None
        keep = should_cache(value) if should_cache else True
        if keep:
            try:
                # 以库里落下的那一版时间戳为准，不在这里再取一次 now()——
                # 两次取值会差几微秒，缓存命中时返回的时间戳就对不上库里的记录。
                written_at = repo.cache_write(key, value, kind)
            except Exception as exc:
                log.warning("写缓存异常（key=%s）：%s", key, exc)
        return value, written_at or _now_iso(), False, None
    except Exception as exc:
        if not allow_stale:
            return None, None, False, str(exc)
        try:
            payload, fetched, _ = repo.cache_read(key, allow_stale=True)
        except Exception as exc2:
            log.warning("读过期缓存异常（key=%s）：%s", key, exc2)
            payload, fetched = None, None
        if payload is not None:
            return payload, fetched, True, str(exc)
        return None, None, False, str(exc)


@app.get("/prediction/{fixture_id}")
async def prediction_detail(fixture_id: str) -> JSONResponse:
    """单场预测的完整详情：方向 / 比分 / 大小球 / 信心等级 / 阵容 / 赔率。

    看板主列表只放「时间 + 队伍 + 预测按钮」，详情按需加载：
    一场比赛的阵容与赔率各要一次外部请求，若在主列表里就把全部比赛
    的详情拉齐，一次刷新就能烧掉几十次额度。

    ⚠️ 阵容与赔率是**实时**从主源取的，其余字段来自落盘快照。
    这两类数据的时效性不同，响应里用 `sources` 分别标注，
    避免把「开赛前算出的概率」和「刚刚取到的赔率」混为一谈。

    未生成预测的比赛返回 404：这与「已生成但数据不全」是两回事，
    前端据此区分「还没算」和「算出来了但缺项」。
    """
    repo = _repo()
    try:
        row = repo.get_prediction_detail(fixture_id)
    except Exception as exc:
        log.exception("/prediction 读取失败")
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}") from exc
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="该场比赛尚无预测。预测由 auto_predict 任务自动生成。")

    inputs = row.get("inputs") or {}
    probs = {
        "home": row.get("home_prob"),
        "draw": row.get("draw_prob"),
        "away": row.get("away_prob"),
    }
    # 方向：与落盘的 result 保持一致，不在这里重算——重算可能因
    # 温度校准开关变化而与当初推送给用户的结论不一致。
    top_scores = inputs.get("top_scores") or []

    # 阵容/赔率：走缓存，未过期直接复用（多人点同一场只发一次请求）
    lineups: list[dict] = []
    odds: dict | None = None
    detail_errors: dict[str, str] = {}
    detail_fetched: dict[str, str] = {}
    client = _live_client()
    if client:
        try:
            fid = int(str(fixture_id))
        except (TypeError, ValueError):
            detail_errors["id"] = "比赛 ID 非整数，无法向主源查询阵容与赔率"
            fid = None  # type: ignore[assignment]
        if fid is not None:
            import asyncio

            raw_lineups, lu_at, lu_stale, lu_err = await _cached_fetch(
                f"lineups:{fid}", "lineups", lambda: client.get_lineups(fid))
            raw_odds, od_at, od_stale, od_err = await _cached_fetch(
                f"odds:{fid}", "odds", lambda: client.get_odds(fid))

            if lu_err and raw_lineups is None:
                detail_errors["lineups"] = lu_err
            if od_err and raw_odds is None:
                detail_errors["odds"] = od_err
            lineups = _norm_lineups(raw_lineups or [])
            odds = _norm_odds(raw_odds, fid)
            # 告诉前端这份数据是何时真拉取的，以及是否用了过期缓存
            if lu_at:
                detail_fetched["lineups"] = lu_at
            if od_at:
                detail_fetched["odds"] = od_at
            if lu_stale:
                detail_fetched["lineups_stale"] = "true"
            if od_stale:
                detail_fetched["odds_stale"] = "true"
    else:
        detail_errors["client"] = "实时客户端不可用，阵容与赔率未取"

    return JSONResponse({
        "version": _VERSION,
        "fixture_id": fixture_id,
        "league": row.get("league"),
        "league_cn": _league_name(str(row.get("league") or "")),
        "home": row.get("home"),
        "away": row.get("away"),
        "home_cn": _cn(row.get("home")),
        "away_cn": _cn(row.get("away")),
        "kickoff": row.get("kickoff"),
        "result": row.get("result"),
        # 顶层同时给一份平铺概率：前端既可以用 probs 整体传，
        # 也可以直接读 home_prob 逐个渲染，不必再解一层。
        "home_prob": row.get("home_prob"),
        "draw_prob": row.get("draw_prob"),
        "away_prob": row.get("away_prob"),
        "probs": probs,
        "best_score": row.get("best_score"),
        "level_key": row.get("level_key"),
        "predicted_at": row.get("created_at"),
        # 以下为落盘派生量（预测时算好，非实时）
        "lambda_home": inputs.get("lambda_home"),
        "lambda_away": inputs.get("lambda_away"),
        "over_2_5": inputs.get("over_2_5"),
        "btts": inputs.get("btts"),
        "top_scores": top_scores,
        "data_completeness": inputs.get("data_completeness"),
        "odds_error": inputs.get("odds_error"),
        # 决策链证据：预测时的阶段式概率与输入特征（落盘快照，非实时）。
        # 老预测没有这个键（本次上线前的），前端按缺失处理，不要渲染成空表。
        "trace": inputs.get("trace"),
        # 以下为实时取回（可能来自缓存，fetched_at 为实际拉取时刻）
        "lineups": lineups,
        "odds": odds,
        "fetched_at": detail_fetched,
        "errors": detail_errors,
    })


@app.get("/teammiss")
async def teammiss(limit: int = 300) -> JSONResponse:
    """导出「未命中中文名、只能显示英文原名」的真实队名。

    ⚠️ 为什么要暴露成网页端点：
    队名字典不可能一次收录所有联赛的所有球队（项目已启用几十个联赛）。
    靠人肉猜会漏，靠重启等也不会自愈。这里把**埋点实测到的原名**
    原样吐出来，管理员打开 URL 复制一份即可批量补录——
    补的是真实出现过的字符串，不按规律推演。

    与 Telegram 的 /teammiss 同源，都是 formatkit.unmatched_team_names()。
    """
    limit = max(1, min(int(limit), 1000))
    try:
        import formatkit
        names = formatkit.unmatched_team_names(limit=limit)
    except Exception as exc:
        log.warning("读取未命中队名失败：%s", exc)
        names = []
    return JSONResponse({
        "version": _VERSION,
        "count": len(names),
        "names": names,
        "hint": "把这些原名连同中文译名一起补进 templates.TEAM_NAMES 即可。",
    })


@app.get("/oddsprobe")
async def odds_probe(limit: int = 200) -> JSONResponse:
    """导出最近一次 /oddsprobe 探测到的**完整盘口结构**（含全部选项）。

    ⚠️ 为什么要走网页端点而不是 Telegram：
    一场比赛有几十个盘口，全部列出远超 Telegram 单条 4096 上限
    （实测 BadRequest: Message is too long，异常发生在最后一步，
    用户侧只看到「机器人没回复」）。JSON 端点没有这个限制。

    管理员打开本 URL 即可复制盘口 id / 名称 / 选项写法，
    据此实现亚盘解析——不凭记忆猜 bet id。

    同时返回最近几次探测的成败记录 ``runs``：
    用于区分「命令没跑」「跑了但没取到盘口」「取到了但消息发不出去」。
    """
    limit = max(1, min(int(limit), 1000))
    try:
        import oddsprobe_log
        bets = oddsprobe_log.latest_bets(limit=limit)
        runs = oddsprobe_log.latest_runs(limit=10)
        fid = oddsprobe_log.latest_fixture_id()
    except Exception as exc:
        log.warning("读取盘口探测结果失败：%s", exc)
        bets, runs, fid = [], [], None
    return JSONResponse({
        "version": _VERSION,
        "fixture_id": fid,
        "count": len(bets),
        "bets": bets,
        "runs": runs,
        "hint": "bet id 与选项写法以这里为准；亚盘通常名为 Asian Handicap。",
    })


@app.get("/live")
async def live(detail: int = 1) -> JSONResponse:
    """正在进行的比赛：实时比分 + 阵容 + 伤停 + 赔率。

    `detail=0` 只取赛况列表（1 次请求），`detail=1`（默认）再逐场并发取
    阵容/伤停/赔率。

    ⚠️ 依赖付费能力 `fixtures?live=all`。取不到时**明确告知原因**，
    绝不能显示成「当前没有进行中的比赛」——那会让人一直等下去。

    结果按 60 秒 TTL 落库缓存：看板多人共用，先后打开同一页面不该
    各打一次外网。缓存命中时 `updated_at` 返回**实际拉取时刻**，
    由前端如实显示「截至 10:32」，而不是假装刚取到。
    """
    client = _live_client()
    if not client:
        return JSONResponse({
            "version": _VERSION, "status": "unavailable", "count": 0,
            "items": [], "updated_at": _now_iso(),
            "message": "实时赛况不可用：未配置 API Key 或客户端初始化失败。",
        })

    async def _build() -> dict[str, Any]:
        """真正打外网并组装响应体。只在缓存未命中时执行。"""
        raw = await client.get_live_fixtures()
        live_rows = _pick_live(raw)
        err = getattr(getattr(client, "primary", None), "last_live_error", None)

        if not live_rows:
            # 空列表必须解释清楚，否则用户无从判断是该等还是该换套餐
            if err:
                # 两种失败必须分开说，处置完全不同：
                #   额度用尽 → 等次日重置（或查是否有任务在空转烧额度）
                #   套餐不含 → 必须升级订阅，等也没用
                # 实测生产返回的原文是 "You have reached the request limit
                # for the day"，把这种也说成「套餐不含 live」会误导升级。
                low = str(err).lower()
                if "request limit" in low or "reached the" in low or "too many" in low:
                    msg = ("实时端点不可用：当日请求额度已用尽。"
                           "这不是套餐不支持——额度次日重置，"
                           "届时无需任何改动即会自动恢复。")
                    status = "quota_exhausted"
                elif ("not subscribed" in low or "permission" in low
                      or "do not have access" in low):
                    msg = ("实时端点不可用：当前订阅不含 live 能力，"
                           "需在 API-Football 控制台确认套餐。")
                    status = "unsupported"
                else:
                    msg = ("实时端点不可用：原因未归类。"
                           "若长期如此，请在控制台确认套餐与当日用量。")
                    status = "unsupported"
            else:
                msg = "此刻没有正在进行的比赛。比赛开始后本板块会自动出现。"
                status = "empty"
            # raw_count 是唯一能区分「真没比赛」与「静默失败」的证据：
            # live=all 在套餐不含该能力时可能返回 HTTP 200 + 空列表，
            # 不抛错 → last_live_error 为 None → 看起来和「此刻没比赛」一模一样。
            # 前者要换套餐，后者只需等待，处置完全不同。
            return {"status": status, "count": 0, "items": [],
                    "updated_at": _now_iso(), "message": msg,
                    "error_raw": str(err) if err else None,
                    "raw_count": len(raw or []),
                    "diag": ("live=all 返回 %d 条原始记录（未筛出进行中）。"
                             "若长期为 0 且从无比赛出现，多为订阅不含 live 能力"
                             "（该情况返回空列表而非报错）。" % len(raw or []))}

        # ── 只保留「本模型推送过」的比赛 ────────────────────────────
        # `live=all` 返回的是全网进行中的比赛，绝大多数与本项目无关
        # （未配置的联赛、或已配置但没生成预测的场次）。
        # 看板的价值是跟踪**自己推送的**命中情况，不是转播全部比赛；
        # 展示无关比赛还会让人误以为模型预测了它们。
        wanted: list[str] = []
        by_fid: dict[str, dict] = {}
        for fx in live_rows:
            fid = (fx.get("fixture") or {}).get("id")
            if fid is None:
                continue
            key = str(fid)
            wanted.append(key)
            by_fid.setdefault(key, fx)

        try:
            owned = _repo().predicted_fixture_ids(wanted)
        except Exception as exc:
            # 库不可用时宁可**不展示**也不误展示：把没预测过的比赛
            # 显示成「已推送」比少显示几场严重得多。
            log.warning("判断实时比赛归属失败：%s", exc)
            owned = set()

        matched = [by_fid[k] for k in wanted if k in owned]
        filtered_out = len(live_rows) - len(matched)

        if not matched:
            # 有进行中的比赛、但一场都不是我们推送过的——
            # 必须说清是「没推送过」而不是「没有比赛」，
            # 否则用户会一直等一个永远不会出现的板块。
            return {
                "status": "not_predicted", "count": 0, "items": [],
                "updated_at": _now_iso(),
                "live_total": len(live_rows),
                "message": (f"当前有 {len(live_rows)} 场比赛正在进行，"
                            "但都不是本模型推送过的预测场次。"
                            "本板块只跟踪已推送/已生成预测的比赛。"),
            }

        picked = matched[:LIVE_MAX_MATCHES]
        truncated = max(0, len(matched) - len(picked))

        items: list[dict] = []
        if detail:
            import asyncio

            async def _one(fx: dict) -> dict:
                fid = (fx.get("fixture") or {}).get("id")
                det: dict[str, Any] = {"errors": {}}
                calls = {
                    "lineups": lambda: client.get_lineups(fid),
                    "injuries": lambda: client.get_injuries(fid),
                    "odds": lambda: client.get_odds(fid),
                }
                results = await asyncio.gather(
                    *[c() for c in calls.values()], return_exceptions=True)
                for key, value in zip(calls, results):
                    if isinstance(value, BaseException):
                        # 单个可选字段失败不影响整场展示，但原因要留痕
                        det["errors"][key] = str(value)
                        det[key] = [] if key != "odds" else None
                    else:
                        det[key] = value
                return _live_shape(fx, det)

            items = list(await asyncio.gather(*[_one(fx) for fx in picked]))
        else:
            items = [_live_shape(fx, {}) for fx in picked]

        msg = (f"另有 {truncated} 场已推送的比赛未展示（单轮上限 "
               f"{LIVE_MAX_MATCHES} 场，避免额度过快耗尽）。"
               if truncated else "")
        if filtered_out and not msg:
            msg = (f"全网另有 {filtered_out} 场进行中，"
                   "未生成预测故不展示。")
        return {
            "status": "ok", "count": len(items), "items": items,
            "updated_at": _now_iso(), "truncated": truncated,
            "live_total": len(live_rows), "filtered_out": filtered_out,
            "message": msg,
        }

    body, fetched_at, stale, err = await _cached_fetch(
        f"live:list:{1 if detail else 0}", "live", _build,
        # 只缓存「有比赛」的结果。empty / unsupported 随时可能变，
        # 缓存它们会让刚开踢的比赛迟迟不出现，也会把「套餐不支持」
        # 这个需要及时发现的状态盖住。
        should_cache=lambda v: v.get("status") == "ok")

    if body is None:
        return JSONResponse({
            "version": _VERSION, "status": "error", "count": 0,
            "items": [], "updated_at": _now_iso(),
            "message": f"实时赛况拉取失败：{err}",
        })

    body = dict(body)
    body["version"] = _VERSION
    # 用实际拉取时刻，不用当前时刻——缓存命中时两者不同，
    # 显示当前时刻等于把旧数据伪装成刚取到的。
    body["updated_at"] = fetched_at or body.get("updated_at")
    if stale:
        body["stale"] = True
        body["message"] = (body.get("message") or "")
        body["message"] += f"（最后一次成功拉取的快照，本次实时拉取失败：{err}）"
    return JSONResponse(body)


def main() -> None:  # pragma: no cover
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
