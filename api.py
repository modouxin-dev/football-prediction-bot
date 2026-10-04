#!/usr/bin/env python3
"""Web 看板 API 桥接层 / API bridge for the web dashboard.

只做三件事：健康检查、统计读数、把 service 的能力暴露成 HTTP。
刻意保持极简——它不复制业务逻辑，全部转发给 service / repository。

为什么是可选依赖：
    机器人本体（Telegram 长轮询 + 定时任务）完全不需要 FastAPI。
    把它写进 requirements.txt 会让 Docker 镜像平白增大，
    且给生产进程引入一个不用的攻击面。因此放在 requirements-web.txt，
    只在确实要跑 Web 看板时安装。

启动：
    pip install -r requirements-web.txt
    python -m uvicorn api:app --host 0.0.0.0 --port 8000

    curl http://127.0.0.1:8000/health
    curl http://127.0.0.1:8000/stats
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("api")

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse
except ImportError as exc:  # pragma: no cover - 未装可选依赖时给出明确指引
    raise SystemExit(
        "缺少 Web 依赖。请先执行：pip install -r requirements-web.txt"
    ) from exc

import analytics
import paths
from repository import PredictionRepository

# templates 是零依赖模块（只放静态文案与联赛名表），可以安全地在顶层引入。
# 刻意**不在顶层** import formatkit：它依赖 service，实测导入耗时 1192ms，
# 会直接压垮首请求。改为在 lifespan 预热时绑定一次（见 _team_cn）。
from templates import LEAGUE_NAMES

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
    """看板页面。"""
    if not _INDEX.exists():  # pragma: no cover - 打包遗漏时给出明确提示
        raise HTTPException(status_code=500, detail=f"缺少静态页面：{_INDEX}")
    return FileResponse(_INDEX)


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
async def fixtures(date: str | None = None, limit: int = 100) -> JSONResponse:
    """当日（或指定日期）赛程 + 已生成的预测，按联赛分组。

    与 Telegram 看的是同一张 matches 表，因此机器人刷新后看板同步刷新，
    不存在两套数据。日期格式 YYYY-MM-DD，默认今天（UTC 日期）。

    未生成预测的比赛 `prediction` 为 null —— 表示「还没算」而非「算出来是 0」，
    前端必须区分这两种状态。
    """
    day = (date or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    limit = max(1, min(int(limit), 300))
    repo = _repo()
    try:
        rows = repo.matches_with_predictions(day, limit=limit)
    except Exception as exc:
        log.exception("/fixtures 读取失败")
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}") from exc

    groups: dict[str, list[dict]] = {}
    for r in rows:
        pred = r.get("prediction")
        groups.setdefault(r.get("competition") or "", []).append({
            "fixture_id": r["fixture_id"],
            "kickoff": r["kickoff"],
            "status": r["status"],
            "home": r["home"],
            "away": r["away"],
            "home_cn": _cn(r["home"]),
            "away_cn": _cn(r["away"]),
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "actual_home": r.get("actual_home"),
            "actual_away": r.get("actual_away"),
            "prediction": pred,
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
    return JSONResponse({
        "status": "ok" if total else "empty",
        "version": _VERSION,
        "date": day,
        "total": total,
        "leagues": payload,
    })


@app.get("/commands")
async def commands() -> JSONResponse:
    """暴露指令清单，供 Web 看板自动生成导航（启动时已预热，响应为毫秒级）。"""
    return JSONResponse({"status": "ok", "commands": _commands_payload()})


def main() -> None:  # pragma: no cover
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
