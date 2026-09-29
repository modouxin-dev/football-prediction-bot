"""只读分析层 / Read-only analytics for the dashboard.

数据来源只有一个：SQLite（由 service.py 写入、repository.py 管理）。
本模块**不联网、不调用外部 API、不写库**——这是刻意的设计：

1. 看板刷新不应消耗宝贵的 API 免费额度
2. Web 请求必须与 Telegram 命令解耦：数据源挂了看板仍能看历史
3. 只读 SQLite 让响应时间稳定在毫秒级

SSOT 判断：`service.py` 是**写入侧**的唯一真理来源；本模块是**读取侧**
的聚合层，不复制业务规则（结果口径复用 repository._outcome，
强度计算复用 analyzer.build_league_model），因此不存在两套逻辑。
"""
from __future__ import annotations

import logging
import sqlite3

from analyzer import build_league_model
from backtest import (
    calibration_buckets, calibration_error, log_loss as _log_loss,
    outcome_index, rps as _rps,
)
from repository import _outcome

log = logging.getLogger("analytics")

# 少于这么多场已结算预测时，统计不可靠——看板要明确提示，而不是画一条假曲线
MIN_SETTLED_FOR_CURVE = 20
# 少于这么多场历史赛果时，不计算强度榜单（样本不足会导致强度全是噪声）
MIN_MATCHES_FOR_STRENGTH = 60


def _connect(db_path: str) -> sqlite3.Connection:
    """复用 repository 的连接：它会自动建表，空库也能安全查询。

    若直接用裸 sqlite3.connect，库里还没有 matches 表时会抛
    "no such table"，把「还没有数据」误报成「出错了」。
    """
    from repository import PredictionRepository

    return PredictionRepository(db_path)._connect()


# ============================================================================
# 一、模型健康度
# ============================================================================

def settled_predictions(db_path: str, limit: int = 500) -> list[sqlite3.Row]:
    """已结算的预测，按开赛时间正序（趋势图需要时间序）。"""
    try:
        conn = _connect(db_path)
        return list(conn.execute(
            "SELECT fixture_id, home, away, kickoff, result, level_key,"
            "       home_prob, draw_prob, away_prob, best_score,"
            "       actual_home, actual_away, settled_at "
            "FROM predictions "
            "WHERE actual_home IS NOT NULL AND actual_away IS NOT NULL "
            "ORDER BY kickoff LIMIT ?", (limit,),
        ))
    except Exception as exc:
        log.warning("读取已结算预测失败：%s", exc)
        return []


def model_health(db_path: str) -> dict:
    """Log Loss 趋势 + 校准曲线 + RPS + 各信心等级命中率。"""
    rows = settled_predictions(db_path)
    if not rows:
        return {"status": "empty", "settled": 0,
                "message": "尚无已结算的预测。比赛结束后赛果会自动同步，届时这里会出现曲线。"}

    points, cumulative, running = [], [], 0.0
    records = []
    by_level: dict[str, dict] = {}
    hits = 0

    for idx, r in enumerate(rows, start=1):
        probs = (r["home_prob"] or 0.0, r["draw_prob"] or 0.0, r["away_prob"] or 0.0)
        actual_idx = outcome_index(r["actual_home"], r["actual_away"])
        ll = _log_loss(probs, actual_idx)
        running += ll
        cumulative.append({
            "n": idx,
            "log_loss": running / idx,
            "rps": _rps(probs, actual_idx),
        })
        # 单点抖动太大，趋势图用逐点值 + 累积均值双线
        points.append({
            "n": idx,
            "log_loss": ll,
            "kickoff": r["kickoff"],
            "label": f"{r['home']} vs {r['away']}",
        })

        pred_name = r["result"]
        hit = (pred_name == _outcome(r["actual_home"], r["actual_away"]))
        hits += 1 if hit else 0
        best_p = max(probs)
        records.append({"prob": best_p, "hit": bool(hit)})

        key = r["level_key"] or "unknown"
        slot = by_level.setdefault(key, {"total": 0, "hit": 0})
        slot["total"] += 1
        slot["hit"] += 1 if hit else 0

    # RPS 累积均值
    run_rps = 0.0
    for idx, c in enumerate(cumulative, start=1):
        run_rps += c["rps"]
        c["rps"] = run_rps / idx

    buckets = calibration_buckets(records)
    total = len(rows)
    reliable = total >= MIN_SETTLED_FOR_CURVE
    return {
        "status": "ok" if reliable else "insufficient",
        "settled": total,
        "hit": hits,
        "accuracy": hits / total,
        "mean_log_loss": running / total,
        "mean_rps": sum(c["rps"] for c in cumulative) / total,
        "ece": calibration_error(buckets),
        "reliable": reliable,
        "min_samples": MIN_SETTLED_FOR_CURVE,
        "message": None if reliable else (
            f"样本仅 {total} 场，少于 {MIN_SETTLED_FOR_CURVE} 场，曲线仅供观察、暂不足以定论。"),
        "trend": cumulative,          # 累积均值（平滑，用于趋势判断）
        "points": points,             # 逐点值（抖动大，仅作散点参考）
        "calibration": buckets,
        "by_level": {
            k: {"total": v["total"], "hit": v["hit"],
                "rate": (v["hit"] / v["total"]) if v["total"] else None}
            for k, v in by_level.items()
        },
    }


# ============================================================================
# 二、预测审计
# ============================================================================

def prediction_audit(db_path: str, limit: int = 50) -> dict:
    """历史预测 → 实际赛果 → 逐场命中与否（倒序，最新在前）。"""
    rows = settled_predictions(db_path)
    items = []
    for r in reversed(rows[-limit:]):
        pred = r["result"]
        actual = _outcome(r["actual_home"], r["actual_away"])
        items.append({
            "fixture_id": r["fixture_id"],
            "home": r["home"],
            "away": r["away"],
            "kickoff": r["kickoff"],
            "predicted": pred,
            "actual": actual,
            "score": f"{r['actual_home']}-{r['actual_away']}",
            "best_score": r["best_score"],
            "level": r["level_key"] or "unknown",
            "prob": max(r["home_prob"] or 0, r["draw_prob"] or 0, r["away_prob"] or 0),
            "hit": pred == actual,
        })
    return {"status": "ok" if items else "empty", "count": len(items), "items": items}


# ============================================================================
# 三、强度榜单（由本地赛果重建，不联网）
# ============================================================================

def _standings_from_matches(rows: list[sqlite3.Row]) -> list[dict]:
    """把本地 matches 表里的已完场比赛，重建成 build_league_model 需要的积分榜行。

    只统计有比分的比赛；未开赛的跳过。
    """
    teams: dict[str, dict] = {}
    for r in rows:
        hs, as_ = r["home_score"], r["away_score"]
        if hs is None or as_ is None:
            continue
        hid, aid = str(r["home_team_id"]), str(r["away_team_id"])
        h = teams.setdefault(hid, {"team": {"id": hid, "name": r["home_team_name"] or hid},
                                   "home": {"played": 0, "goals": {"for": 0, "against": 0}},
                                   "away": {"played": 0, "goals": {"for": 0, "against": 0}}})
        a = teams.setdefault(aid, {"team": {"id": aid, "name": r["away_team_name"] or aid},
                                   "home": {"played": 0, "goals": {"for": 0, "against": 0}},
                                   "away": {"played": 0, "goals": {"for": 0, "against": 0}}})
        h["home"]["played"] += 1
        h["home"]["goals"]["for"] += hs
        h["home"]["goals"]["against"] += as_
        a["away"]["played"] += 1
        a["away"]["goals"]["for"] += as_
        a["away"]["goals"]["against"] += hs
    return list(teams.values())


def strength_table(db_path: str, competition: str = "") -> dict:
    """攻防强度榜。

    ⚠️ 口径说明：强度来自 **泊松模型的攻防收缩估计**（analyzer.build_league_model），
    它是本项目预测时真正使用的强度。Dixon-Coles 只做低比分概率修正，
    **不改变强度值**——因此这里不存在「DC 版强度」这种东西，
    榜单展示的就是模型在用的那套强度。
    """
    try:
        conn = _connect(db_path)
        if competition:
            rows = list(conn.execute(
                "SELECT home_team_id, away_team_id, home_team_name, away_team_name,"
                "       home_score, away_score FROM matches "
                "WHERE competition_code=? AND home_score IS NOT NULL",
                (competition,)))
        else:
            rows = list(conn.execute(
                "SELECT home_team_id, away_team_id, home_team_name, away_team_name,"
                "       home_score, away_score FROM matches "
                "WHERE home_score IS NOT NULL"))
    except Exception as exc:
        log.warning("读取赛果失败：%s", exc)
        return {"status": "error", "message": f"读取失败：{exc}", "teams": []}

    finished = sum(1 for r in rows if r["home_score"] is not None)
    if finished < MIN_MATCHES_FOR_STRENGTH:
        return {
            "status": "insufficient", "finished": finished,
            "min_samples": MIN_MATCHES_FOR_STRENGTH, "teams": [],
            "message": f"本地仅 {finished} 场已完赛（需 ≥{MIN_MATCHES_FOR_STRENGTH}），"
                       f"样本不足时强度全是噪声，故不展示榜单。",
        }

    standings = _standings_from_matches(rows)
    model = build_league_model(standings)
    teams = []
    for row in standings:
        tid = row["team"]["id"]
        s = model.strength(tid)
        played = row["home"]["played"] + row["away"]["played"]
        gf = row["home"]["goals"]["for"] + row["away"]["goals"]["for"]
        ga = row["home"]["goals"]["against"] + row["away"]["goals"]["against"]
        # 综合强度：进攻取高、防守取低（defense 越大表示失球越多，越差）
        teams.append({
            "team_id": tid,
            "name": row["team"]["name"],
            "played": played,
            "goals_for": gf,
            "goals_against": ga,
            "attack": round((s.attack_home + s.attack_away) / 2, 3),
            "defense": round((s.defense_home + s.defense_away) / 2, 3),
            "overall": round((s.attack_home + s.attack_away) / 2
                             - (s.defense_home + s.defense_away) / 2, 3),
        })
    teams.sort(key=lambda t: t["overall"], reverse=True)
    return {
        "status": "ok",
        "finished": finished,
        "league_avg_home": round(model.avg_home_goals, 3),
        "league_avg_away": round(model.avg_away_goals, 3),
        "teams": teams,
    }


def overview(db_path: str) -> dict:
    """看板首屏汇总：一次查询拿到全部卡片需要的数字。"""
    health = model_health(db_path)
    strength = strength_table(db_path)
    return {
        "health": health,
        "strength": {
            k: strength.get(k) for k in ("status", "finished", "message", "teams")
        },
    }
