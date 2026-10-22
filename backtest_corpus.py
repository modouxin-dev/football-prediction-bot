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
from typing import Sequence

from football_data_uk import (DIV_TO_COMPETITION, available_local_seasons,
                              canonical_team_name, load_local)
from migrate_elo import fetch_finished

log = logging.getLogger(__name__)

DEFAULT_DIV = "E0"
DEFAULT_COMPETITION = "PL"

# 多联赛语料的默认分区集合（football-data.co.uk 的 div 代号）。
#
# ⚠️ 命名坑：西甲是 **SP1**，不是 E1 —— E1 是英冠（ELC）。
# 写错不会报错，只会静默取回另一个联赛的赛果，回测结论随之失真。
# 因此这里**不**自己维护代号表，直接复用 football_data_uk.DIV_TO_COMPETITION。
DEFAULT_DIVS: tuple[str, ...] = ("E0", "SP1", "I1", "D1", "F1")


def div_for_competition(competition: str) -> str:
    """库内 competition_code → football-data.co.uk 分区代号。

    查不到时退回 DEFAULT_DIV，由调用方决定是否降级为「只跑英超」。
    """
    comp = (competition or "").strip().upper()
    for div, code in DIV_TO_COMPETITION.items():
        if code.upper() == comp:
            return div
    return DEFAULT_DIV


def available_divs() -> list[str]:
    """返回镜像里**真的**有内置 CSV 的分区（按 DEFAULT_DIVS 的顺序）。

    只声明支持、但 data/history/ 里没有文件的分区不算数——
    否则回测会显示"支持五联赛"却只跑出英超的样本，属于虚报。
    """
    return [d for d in DEFAULT_DIVS if available_local_seasons(d)]


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
        # 赛前 1X2 赔率（football-data.co.uk 自带，Pinnacle > B365 > 均值）。
        # 回测的市场口径要用它：生产展示口径已是去水市场概率，
        # 回测若只跑泊松就与生产不一致，验证的就不是真实上线行为。
        # 赔率是**赛前**记录的，属于当场可得信息，不构成前视偏差。
        "odds": fx.get("odds") or None,
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
                divs: Sequence[str] | None = None,
                include_history: bool = True
                ) -> tuple[list[dict], dict]:
    """合并语料，返回 (按时间正序的样本, 来源统计)。

    分区（div）选择规则，优先级从高到低：
      1. 显式传 divs → 用传入的分区列表（多联赛回测走这条）；
      2. 传了 div → 只用该分区（兼容旧调用，行为不变）；
      3. 都没传 → 用 DEFAULT_DIVS 里**实际有内置 CSV** 的分区。

    第 3 条刻意做"有数据才算数"的过滤：否则 info 会显示"覆盖五联赛"，
    实际却只有英超有样本——回测结论的可信度就被虚报了。

    include_history=False 时等价于旧的 fetch_finished（只库内），
    便于对照和故障降级。
    """
    if divs is None:
        if div:
            divs = [div]
        else:
            divs = available_divs() or [DEFAULT_DIV]
    divs = list(dict.fromkeys(divs))  # 去重且保序，避免同一分区算两遍

    db_rows = fetch_finished(repo, competition)
    for r in db_rows:
        r["source"] = "db"

    merged: dict[tuple, dict] = {_dedup_key(r): r for r in db_rows}
    history_added = 0
    by_div: dict[str, int] = {}

    if include_history:
        known = known_team_ids(repo)
        for _div in divs:
            # 单分区时尊重调用方传入的 competition；
            # 多分区时必须用各分区自己的代号，否则五联赛的赛果
            # 会被打成同一个 competition，去重键碰撞导致样本丢失。
            if competition and len(divs) == 1:
                comp = competition
            else:
                comp = DIV_TO_COMPETITION.get(_div, DEFAULT_COMPETITION)
            div_count = 0
            for season in available_local_seasons(_div):
                try:
                    fixtures = load_local(season, _div, competition=comp,
                                          known=known)
                except Exception:
                    # 单个赛季读坏不应拖垮整体：跳过该赛季，其余照常
                    log.exception("回测语料：分区 %s 赛季 %s 读取失败，已跳过",
                                  _div, season)
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
                    div_count += 1
            by_div[_div] = div_count

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
        # 多联赛可观测性：光说"支持五联赛"没用，得能看到每个分区
        # 到底贡献了多少场——某个分区为 0 就说明它的 CSV 没进镜像。
        "divs": list(divs),
        "by_div": by_div,
        "competitions": sorted({
            str(m.get("competition") or "") for m in matches
            if m.get("competition")
        }),
    }
    return matches, info
