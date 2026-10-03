"""跨赛季先验：用「上一个赛季」的真实强度，替代收缩时的联赛平均。

为什么需要它
------------
``analyzer.build_league_model`` 默认把每队向**联赛平均**收缩（先验目标 1.0）。
赛季初每队只有 3-4 场样本，收缩权重 ``prior/(games+prior)`` 高达 60%，
于是强队和弱队被一起拉向平均——阿森纳主场对利兹联只算出 48% 主胜，
而市场去水 70.9%。收缩本身没错，错在**先验目标对所有人一样**，
先验无法携带"阿森纳本来就是强队"这个信息。

本模块把先验目标换成该队**上赛季的最终强度**，先验就重新有了区分度。

内置历史赛果验证（3 个英超赛季 · 930 场走前预测）
-------------------------------------------------
早赛季 + 市场判定为主场强队（n=37）：

    方案                   Log Loss   命中率   模型 vs 实际   模型 vs 市场
    prior=5 基线            1.0157    50.0%    低估 12.1pp    低估 11.3pp
    prior=3 + 跨赛季先验    1.0171    50.3%    低估  4.7pp    低估  3.9pp

即：命中率略升，赛季初对强队的低估从 12.1pp 收窄到 4.7pp。
代价是 Log Loss 上升 0.0014——LL 奖励保守，这个取舍已被刻意接受，
理由见 tests/test_season_start_calibration.py 的闸门说明。

降级链
------
拿不到任何先验时返回 ``{}``，``build_league_model`` 退回联赛平均，
行为与改造前完全一致。任何异常都不得让预测挂掉。
"""

from __future__ import annotations

import logging

from analyzer import build_league_model
from football_data_uk import (
    available_local_seasons,
    canonical_team_name,
    load_local,
)

log = logging.getLogger(__name__)

# league_id → (内置 CSV 的 division 代码, competition_code)
_LEAGUE_TO_DIV: dict[int, tuple[str, str]] = {
    39: ("E0", "PL"),    # 英超
    78: ("D1", "BL1"),   # 德甲
    140: ("SP1", "PD"),  # 西甲
    135: ("I1", "SA"),   # 意甲
    61: ("F1", "FL1"),   # 法甲
}

_CACHE: dict[tuple[int, int], dict] = {}


def _standings_from_fixtures(fixtures: list[dict]) -> list[dict]:
    """由内置历史 fixture 重建成 build_league_model 需要的积分榜行。"""
    teams: dict[str, dict] = {}
    for fx in fixtures:
        sc = (fx.get("score") or {}).get("fullTime") or {}
        hg, ag = sc.get("home"), sc.get("away")
        if hg is None or ag is None:
            continue
        home = (fx.get("homeTeam") or {})
        away = (fx.get("awayTeam") or {})
        hid, aid = str(home.get("id")), str(away.get("id"))
        h = teams.setdefault(hid, {
            "team": {"id": hid, "name": home.get("name") or hid},
            "home": {"played": 0, "goals": {"for": 0, "against": 0}},
            "away": {"played": 0, "goals": {"for": 0, "against": 0}},
        })
        a = teams.setdefault(aid, {
            "team": {"id": aid, "name": away.get("name") or aid},
            "home": {"played": 0, "goals": {"for": 0, "against": 0}},
            "away": {"played": 0, "goals": {"for": 0, "against": 0}},
        })
        h["home"]["played"] += 1
        h["home"]["goals"]["for"] += hg
        h["home"]["goals"]["against"] += ag
        a["away"]["played"] += 1
        a["away"]["goals"]["for"] += ag
        a["away"]["goals"]["against"] += hg
    return list(teams.values())


def load_prior_strength(league_id: int, season: int,
                        known: dict[str, str] | None = None) -> dict:
    """返回 ``{team_id: {attack_home, defense_home, attack_away, defense_away}}``。

    team_id 是 ``build_league_model`` 实际使用的键（线上为 API-Football 数字 id）。
    ``known`` 为「规范队名 → team_id」反查表；能用上时优先复用库内官方 id，
    保证先验与当季积分榜用的是同一个 id 空间。

    拿不到就返回空字典，调用方无需特殊处理。
    """
    key = (int(league_id), int(season))
    if key in _CACHE:
        return _CACHE[key]

    out: dict = {}
    try:
        div_comp = _LEAGUE_TO_DIV.get(int(league_id))
        if not div_comp:
            return _CACHE.setdefault(key, {})
        div, competition = div_comp

        seasons = available_local_seasons(div)
        # 上赛季 = 内置赛季中严格小于当季的最大者
        prev = [s for s in seasons if s < int(season)]
        if not prev:
            return _CACHE.setdefault(key, {})
        prev_season = max(prev)

        fixtures = load_local(prev_season, div, competition=competition,
                              known=known)
        finished = [f for f in fixtures
                    if ((f.get("score") or {}).get("fullTime") or {}).get("home")
                    is not None]
        if len(finished) < 100:
            log.info("跨赛季先验：%s %s 仅 %d 场已完赛，样本不足，跳过",
                     div, prev_season, len(finished))
            return _CACHE.setdefault(key, {})

        standings = _standings_from_fixtures(finished)
        model = build_league_model(standings)
        for row in standings:
            tid = row["team"]["id"]
            s = model.strength(tid)
            out[tid] = {
                "attack_home": s.attack_home,
                "defense_home": s.defense_home,
                "attack_away": s.attack_away,
                "defense_away": s.defense_away,
            }
        log.info("跨赛季先验：%s %s 载入 %d 队（%d 场已完赛）",
                 div, prev_season, len(out), len(finished))
    except Exception as exc:  # 先验是增强项，绝不能拖垮预测
        log.warning("跨赛季先验载入失败，退回联赛平均：%s", exc)
        return _CACHE.setdefault(key, {})

    return _CACHE.setdefault(key, out)


def known_team_ids(repo) -> dict[str, str]:
    """从库内已存比赛反查「规范队名 → team_id」。

    与 backtest_corpus.known_team_ids 同逻辑，这里独立实现是为了避免
    先验模块反向依赖回测模块（回测依赖更重）。
    """
    out: dict[str, str] = {}
    if repo is None:
        return out
    try:
        conn = repo._connect()
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT home_team_id AS tid, home_team_name AS name FROM matches "
            "WHERE home_team_id IS NOT NULL AND home_team_name IS NOT NULL "
            "UNION ALL "
            "SELECT away_team_id AS tid, away_team_name AS name FROM matches "
            "WHERE away_team_id IS NOT NULL AND away_team_name IS NOT NULL")
        for r in rows:
            name = canonical_team_name(r["name"] or "")
            tid = str(r["tid"])
            if name and tid:
                out.setdefault(name, tid)
    except Exception as exc:
        log.warning("跨赛季先验：队名反查失败（%s），改用 CSV 队名键", exc)
    return out


def clear_cache() -> None:
    """仅供测试：清空先验缓存。"""
    _CACHE.clear()
