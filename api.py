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
import paths  # noqa: E402
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


@app.get("/health/model")
async def model_health() -> JSONResponse:
    """模型健康度：Log Loss 趋势、校准曲线、各信心等级命中率。"""
    try:
        data = analytics.model_health(_DB_PATH)
    except Exception as exc:
        log.exception("/health/model 计算失败")
        raise HTTPException(status_code=500, detail=f"计算失败：{exc}") from exc
    return JSONResponse({"status": data["status"], "version": _VERSION, **data})


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
    rows: list[dict[str, Any]] = []
    try:
        for d in day_list:
            for r in repo.matches_with_predictions(d, limit=limit):
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


def main() -> None:  # pragma: no cover
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
