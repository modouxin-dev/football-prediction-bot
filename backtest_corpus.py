"""回测语料：把「库内已完赛」与「镜像内置历史 CSV」合并成一份样本。

为什么需要合并
--------------
/backfill 拉到的主要是当前赛季，样本长期停在几百场，回测结论噪声很大。
镜像里 data/history/*.csv 内置了三个完整赛季的真实赛果（含比分），
把它们并进来，评估样本可从数百场提升到千场量级——这是让回测结论可信的前提。

为什么能合并（关键：队 ID 必须统一）
------------------------------------
matches 表的唯一键是 (competition_code, utc_date, home_team_id, away_team_id)。
若 CSV 用 slug 当 id、库里用官方数字 id，同一支球队会同时存在两个 id，
走前回测会把它们当成两支不同的队，历史完全接不上（等于白喂数据）。

因此先用库内已有的「队名 → id」反查表喂给 CSV 解析器（known=），
让历史比赛复用库里的官方 id；查不到的才退回 slug 兜底。

去重
----
两个来源会覆盖同一场比赛（内置 CSV 含当前赛季）。同一场优先保留库内那行：
它是真实 API 回传的数据，元数据更全；内置 CSV 只作补充。
"""

from __future__ import annotations

import logging

from football_data_uk import (
    available_local_seasons,
    canonical_team_name,
    load_local,
)
from migrate_elo import fetch_finished

log = logging.getLogger(__name__)

DEFAULT_DIV = "E0"
DEFAULT_COMPETITION = "PL"


def known_team_ids(repo) -> dict[str, str]:
    """从库内已存比赛反查「规范队名 → team_id」。

    用 UNION ALL 同时扫主客两列：只对阵过一次的球队也能取到。
    任何异常都退化为「查不到」——反查失败只是少一次 ID 对齐，
    不该让整个回测挂掉。
    """
    out: dict[str, str] = {}
    try:
        conn = repo._connect()
    except Exception:
        log.warning("回测语料：无法连接数据库，跳过队名反查")
        return out
    try:
        rows = conn.execute(
            "SELECT home_team_id AS tid, home_team_name AS name FROM matches "
            "WHERE home_team_id IS NOT NULL AND home_team_name IS NOT NULL "
            "UNION ALL "
            "SELECT away_team_id AS tid, away_team_name AS name FROM matches "
            "WHERE away_team_id IS NOT NULL AND away_team_name IS NOT NULL"
        ).fetchall()
    except Exception:
        log.exception("回测语料：队名反查失败")
        return out
    # 注意：repo._connect() 返回的是**线程内共享连接**（按路径缓存），
    # 这里绝不能 close——关掉会让同线程后续所有数据库操作失败。
    # 与 migrate_elo.fetch_finished 保持一致：只查询，不关闭。

    for r in rows:
        tid, name = r["tid"], r["name"]
        if not tid or not name:
            continue
        key = canonical_team_name(name)
        # 首次出现优先：官方源的主用 id 通常先入库
        if key not in out:
            out[key] = str(tid)
    return out


def fixture_to_match(fx: dict) -> dict | None:
    """内置 CSV 解析出的 fixture → 回测器要的 match 结构。

    无比分（未赛）或缺队 id 的行返回 None，由调用方跳过。
    """
    ft = (fx.get("score") or {}).get("fullTime") or {}
    hs, as_ = ft.get("home"), ft.get("away")
    if hs is None or as_ is None:
        return None
    home = fx.get("homeTeam") or {}
    away = fx.get("awayTeam") or {}
    hid, aid = home.get("id"), away.get("id")
    if hid is None or aid is None:
        return None

    season = None
    start_date = (fx.get("season") or {}).get("startDate") or ""
    if len(start_date) >= 4 and start_date[:4].isdigit():
        season = int(start_date[:4])

    sot = fx.get("shotsOnTarget") or None

    return {
        "fixture_id": fx.get("id"),
        "competition": fx.get("competition") or "",
        "season": season,
        "utc_date": fx.get("utcDate") or "",
        "home_team_id": str(hid),
        "away_team_id": str(aid),
        "home_team_name": home.get("name"),
        "away_team_name": away.get("name"),
        "home_score": int(hs),
        "away_score": int(as_),
        # 场面数据（可选）：football-data.co.uk CSV 自带，API-Football 需额外请求。
        # 缺失时建模层自动退回用进球算强度，行为与改造前一致。
        "home_sot": sot.get("home") if sot else None,
        "away_sot": sot.get("away") if sot else None,
        "source": "history",
    }


def _dedup_key(m: dict) -> tuple:
    """同一场比赛的判定键：同一天 + 同一对阵 + 同一赛事。

    只取日期（不取时刻）：两个来源对同一场的记录时间可能有分钟级差异，
    用完整时间戳会去不掉重。
    """
    return (
        str(m.get("competition") or ""),
        (m.get("utc_date") or "")[:10],
        str(m.get("home_team_id")),
        str(m.get("away_team_id")),
    )


def load_corpus(repo, competition: str = "", *, div: str = DEFAULT_DIV,
                include_history: bool = True
                ) -> tuple[list[dict], dict]:
    """合并语料，返回 (按时间正序的样本, 来源统计)。

    include_history=False 时等价于旧的 fetch_finished（只库内），
    便于对照和故障降级。
    """
    db_rows = fetch_finished(repo, competition)
    for r in db_rows:
        r["source"] = "db"

    merged: dict[tuple, dict] = {_dedup_key(r): r for r in db_rows}
    history_added = 0

    if include_history:
        known = known_team_ids(repo)
        comp = competition or DEFAULT_COMPETITION
        for season in available_local_seasons(div):
            try:
                fixtures = load_local(season, div, competition=comp, known=known)
            except Exception:
                # 单个赛季读坏不应拖垮整体：跳过该赛季，其余照常
                log.exception("回测语料：内置赛季 %s 读取失败，已跳过", season)
                continue
            for fx in fixtures:
                m = fixture_to_match(fx)
                if m is None:
                    continue
                key = _dedup_key(m)
                if key in merged:
                    # 库内已有同一场 → 保留库内那行（真实 API 数据更全）
                    continue
                merged[key] = m
                history_added += 1

    matches = sorted(
        merged.values(),
        key=lambda m: (m.get("utc_date") or "", str(m.get("fixture_id") or "")),
    )
    info = {
        "db": len(db_rows),
        "history": history_added,
        "total": len(matches),
        "seasons": sorted({
            m["season"] for m in matches if m.get("season") is not None
        }),
    }
    return matches, info
