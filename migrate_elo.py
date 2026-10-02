#!/usr/bin/env python3
"""Elo 冷启动迁移：把库里已有的历史赛果重放一遍，让模型上线就有「经验值」。

用法：
    python migrate_elo.py                 # 回算默认库（paths.DB_PATH）
    python migrate_elo.py --db /data/football.db
    python migrate_elo.py --competition PL
    python migrate_elo.py --dry-run       # 只看会算多少场，不写库

设计要点：
- **按时间正序**重放 —— Elo 依赖顺序，乱序会得到不同结果
- **幂等** —— 已处理过的比赛自动跳过，可重复执行（增量更新）
- 只算有比分的比赛（home_score / away_score 均非空）

注意：免费套餐可能拿不到历史赛季数据（README 常见问题已说明）。
若库里没有历史赛果，本脚本会提示「0 场」，此时机器人会从 1500 分冷启动，
靠 PRIOR_GAMES 收缩保证赛季初不至于失真。
"""
from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate_elo")


def fetch_finished(repo, competition: str) -> list[dict]:
    """取出有比分的已结束比赛，按开赛时间正序。"""
    conn = repo._connect()
    if competition:
        rows = conn.execute(
            "SELECT id, competition_code, season, utc_date, home_team_id, away_team_id, "
            "home_score, away_score FROM matches "
            "WHERE home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND competition_code=? ORDER BY utc_date, id",
            (competition,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, competition_code, season, utc_date, home_team_id, away_team_id, "
            "home_score, away_score FROM matches "
            "WHERE home_score IS NOT NULL AND away_score IS NOT NULL "
            "ORDER BY competition_code, utc_date, id"
        ).fetchall()
    return [
        {
            "fixture_id": r["id"],
            "competition": r["competition_code"],
            "season": r["season"],
            # 回测语料合并需要按时间排序，故一并返回（新增字段，向后兼容）
            "utc_date": r["utc_date"] or "",
            "home_team_id": r["home_team_id"],
            "away_team_id": r["away_team_id"],
            "home_score": r["home_score"],
            "away_score": r["away_score"],
        }
        for r in rows
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Elo 冷启动：重放历史赛果")
    ap.add_argument("--db", default=None, help="SQLite 路径，默认用 paths.DB_PATH")
    ap.add_argument("--competition", default="", help="只回算某个联赛，如 PL；留空则全部")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args(argv)

    import paths
    from elo import EloEngine
    from repository import PredictionRepository

    db = args.db or str(paths.DB_PATH)
    repo = PredictionRepository(db)
    log.info("数据库：%s", db)

    warn = paths.persistence_warning()
    if warn:
        log.warning("⚠️ %s", warn)

    # 分组回算：每个联赛一套独立评分
    if args.competition:
        groups = [args.competition]
    else:
        conn = repo._connect()
        groups = [r[0] for r in conn.execute(
            "SELECT DISTINCT competition_code FROM matches "
            "WHERE home_score IS NOT NULL AND away_score IS NOT NULL "
            "ORDER BY competition_code"
        ).fetchall()]

    if not groups:
        log.warning("库里没有任何带比分的比赛，无法回算。机器人将从 1500 分冷启动。")
        return 0

    total = 0
    for comp in groups:
        engine = EloEngine(repo=repo, competition=comp)
        matches = fetch_finished(repo, comp)
        if not matches:
            continue
        if args.dry_run:
            pending = sum(
                1 for m in matches if not repo.elo_is_processed(m["fixture_id"])
            )
            log.info("[%s] 共 %d 场，待回算 %d 场（dry-run，未写入）",
                     comp, len(matches), pending)
            total += pending
            continue

        done = engine.replay(matches)
        total += done
        ratings = repo.elo_ratings(comp)
        if ratings:
            lo, hi = min(ratings.values()), max(ratings.values())
            log.info("[%s] 回算 %d 场（共 %d 场，其余已处理过）· %d 支球队 · 评分 %.0f~%.0f",
                     comp, done, len(matches), len(ratings), lo, hi)

    if args.dry_run:
        log.info("dry-run 结束：待回算合计 %d 场", total)
    else:
        log.info("回算完成：本轮新增 %d 场。可重复执行，已处理的会自动跳过。", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
