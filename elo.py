"""Elo 评分 / Elo ratings.

设计原则：
1. **纯计算与状态分离** —— 本模块上半部分全是纯函数（不碰数据库），
   可以 100% 单元测试；下半部分的 EloEngine 才负责读写 repository。
2. **兼容性** —— 只使用 Python 3.10 已支持的语法（PEP 604 的 `X | Y` 注解）。
   不使用 `typing.Self`（3.11+）等新版特性，保证 CI 的 3.10/3.12 矩阵都能跑。

足球 Elo 与棋类不同，多了两件事：
- **主场优势**：主队按惯例先加约 65 分再算期望胜率。
- **净胜球权重**：赢 5 球与赢 1 球不该拿到同样的分，用乘数放大差距。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---- 默认参数 ---------------------------------------------------------------

DEFAULT_RATING = 1500.0        # 新队基准分
ELO_SCALE = 400.0              # 每差 400 分，强队期望胜率约 91%
HOME_ADVANTAGE = 65.0          # 折算成 Elo 分的主场优势
K_BASE = 20.0                  # 常规 K 值
K_NEW_TEAM = 40.0              # 样本不足时的 K 值（评分收敛更快）
NEW_TEAM_MATCHES = 10          # 少于这么多场算「新队」
MAX_RATING = 2400.0
MIN_RATING = 1200.0

# Elo 差 → λ 系数的强度。0.6 是刻意保守：Elo 万一算错，影响也可控。
ELO_BLEND = 0.6
ELO_CLAMP = (0.8, 1.25)        # λ 系数的上下界，防止 Elo 把模型带崩


# ============================================================================
# 一、纯函数（无副作用，可完整单测）
# ============================================================================

def expected_score(rating_a: float, rating_b: float, *, scale: float = ELO_SCALE) -> float:
    """A 对 B 的期望得分（1=胜 0.5=平 0=负）。"""
    return 1.0 / (1.0 + 10.0 ** (-(rating_a - rating_b) / scale))


def outcome_from_score(home_score: int, away_score: int) -> float:
    """比分 → 主队实际得分。"""
    if home_score > away_score:
        return 1.0
    if home_score < away_score:
        return 0.0
    return 0.5


def k_factor_for(matches_played: int, *, base: float = K_BASE,
                 new_team: float = K_NEW_TEAM, threshold: int = NEW_TEAM_MATCHES) -> float:
    """动态 K：样本少的队用大 K，让评分快速收敛到真实水平。"""
    return new_team if int(matches_played) < threshold else base


def goal_diff_multiplier(home_score: int, away_score: int) -> float:
    """净胜球乘数（World Football Elo Ratings 的常用换算表）。

    赢 1 球乘 1.0，赢 2 球乘 1.5，分差越大权重越高；平局与 1 球小胜为 1.0。
    """
    diff = abs(int(home_score) - int(away_score))
    if diff <= 1:
        return 1.0
    if diff == 2:
        return 1.5
    # 3 球及以上：1.75 起，每多 1 球 +0.125（收敛，避免单场暴涨）
    return 1.75 + (diff - 3) * 0.125


def calculate_elo(rating_a: float, rating_b: float, result: float, k_factor: float,
                  *, home_advantage: float = 0.0, multiplier: float = 1.0,
                  scale: float = ELO_SCALE) -> tuple[float, float]:
    """单场比赛后的 Elo 更新 —— 纯函数，不碰数据库。

    Args:
        rating_a / rating_b: 赛前评分（a 视为主队）
        result: a 的实际得分（1.0 胜 / 0.5 平 / 0.0 负）
        k_factor: 本场使用的 K 值
        home_advantage: 加在 a 身上的主场加分，用于算期望胜率
        multiplier: 净胜球乘数
        scale: Elo 尺度，默认 400

    Returns:
        (new_rating_a, new_rating_b)。两人变化量互为相反数，零和。
    """
    exp_a = expected_score(rating_a + home_advantage, rating_b, scale=scale)
    delta = k_factor * multiplier * (result - exp_a)
    return rating_a + delta, rating_b - delta


def elo_multiplier(home_rating: float, away_rating: float,
                   *, home_advantage: float = HOME_ADVANTAGE,
                   blend: float = ELO_BLEND, scale: float = ELO_SCALE,
                   clamp: tuple[float, float] = ELO_CLAMP) -> float:
    """把 Elo 差转成融合系数 —— 这是 Elo 真正影响预测的入口。

    返回 >1 表示主队更强，<1 表示客队更强。
    刻意夹在 clamp 区间内：Elo 算错时影响也有限，不会带崩模型。

    该系数由 analyzer 以「份额归一」方式使用（不是简单相乘），
    因此 λ主+λ客 精确守恒，不影响大小球判断。
    """
    exp = expected_score(home_rating + home_advantage, away_rating, scale=scale)
    factor = 1.0 + (exp - 0.5) * blend
    low, high = clamp
    return min(max(factor, low), high)


# ============================================================================
# 二、有状态引擎（读写 repository）
# ============================================================================

@dataclass
class EloEngine:
    """Elo 闭环引擎：读评分 → 算更新 → 原子写回 → 打幂等标记。

    幂等性靠 `elo_processed` 的**集合成员检查**，不是 ID 大小比较：
    fixture_id 不是单调时间戳（补赛、延期会让小 ID 晚于大 ID 结束），
    用 `<=` 比较会静默跳过有效比赛。
    """

    repo: object
    competition: str = ""
    home_advantage: float = HOME_ADVANTAGE
    base_rating: float = DEFAULT_RATING
    k_base: float = K_BASE
    k_new_team: float = K_NEW_TEAM
    # 本次运行内的变更条数，便于日志与测试断言
    applied: int = field(default=0, repr=False)

    def _ratings(self) -> dict[str, float]:
        return self.repo.elo_ratings(self.competition)

    def rating_of(self, team_id, ratings: dict[str, float] | None = None) -> float:
        """取某队评分；没有记录则返回基准分（新队）。"""
        table = ratings if ratings is not None else self._ratings()
        return float(table.get(str(team_id), self.base_rating))

    # ---- 单场更新 -----------------------------------------------------------
    def apply_match(self, fixture_id, home_team_id, away_team_id,
                    home_score: int, away_score: int, *, season: int | None = None,
                    dry_run: bool = False) -> dict:
        """把一场已结束的比赛计入 Elo。

        返回本次变更明细；若该场已处理过则返回 {'applied': False}。
        """
        fx = str(fixture_id)
        if not dry_run and self.repo.elo_is_processed(fx):
            return {"applied": False, "reason": "已处理过（幂等跳过）", "fixture_id": fx}

        ratings = self._ratings()
        home_id, away_id = str(home_team_id), str(away_team_id)
        r_home = float(ratings.get(home_id, self.base_rating))
        r_away = float(ratings.get(away_id, self.base_rating))

        result = outcome_from_score(int(home_score), int(away_score))
        mult = goal_diff_multiplier(int(home_score), int(away_score))
        # 两队各自的 K 值：新队收敛更快
        k_home = k_factor_for(self._matches_of(home_id), base=self.k_base,
                              new_team=self.k_new_team)
        k_away = k_factor_for(self._matches_of(away_id), base=self.k_base,
                              new_team=self.k_new_team)
        # 单场只能有一个 K：取两者均值（两队样本量不同时的折中）
        k = (k_home + k_away) / 2.0

        new_home, new_away = calculate_elo(
            r_home, r_away, result, k,
            home_advantage=self.home_advantage, multiplier=mult,
        )
        new_home = min(max(new_home, MIN_RATING), MAX_RATING)
        new_away = min(max(new_away, MIN_RATING), MAX_RATING)

        detail = {
            "applied": True, "fixture_id": fx,
            "home": {"team": home_id, "before": r_home, "after": new_home,
                     "delta": new_home - r_home},
            "away": {"team": away_id, "before": r_away, "after": new_away,
                     "delta": new_away - r_away},
            "k": k, "multiplier": mult, "result": result,
        }
        if dry_run:
            return detail

        ratings[home_id] = new_home
        ratings[away_id] = new_away
        # 评分、变更留痕、幂等标记三者必须一致：任一环节失败都要整体回滚，
        # 否则会出现「评分已加但没打标记」→ 重启后重复计算、评分虚高。
        try:
            with self.repo.atomic() as conn:
                now = _now_iso()
                for tid, rating in ((home_id, new_home), (away_id, new_away)):
                    conn.execute(
                        """INSERT INTO elo_ratings
                           (team_id, competition, rating, matches, last_match, updated_at)
                           VALUES (?,?,?,?,?,?)
                           ON CONFLICT(competition, team_id) DO UPDATE SET
                             rating=excluded.rating,
                             matches=excluded.matches,
                             last_match=excluded.last_match,
                             updated_at=excluded.updated_at""",
                        (tid, self.competition, float(rating),
                         self._matches_of(tid), fx, now),
                    )
                for tid, before, after in ((home_id, r_home, new_home),
                                           (away_id, r_away, new_away)):
                    conn.execute(
                        """INSERT INTO elo_log
                           (fixture_id, team_id, competition, before, after, delta, processed_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (fx, tid, self.competition, before, after, after - before, now),
                    )
                conn.execute(
                    """INSERT INTO elo_processed
                       (fixture_id, competition, season, home_team_id, away_team_id,
                        home_score, away_score, processed_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(fixture_id) DO UPDATE SET
                         home_score=excluded.home_score,
                         away_score=excluded.away_score,
                         processed_at=excluded.processed_at""",
                    (fx, self.competition, season, home_id, away_id,
                     int(home_score), int(away_score), now),
                )
        except Exception as exc:
            log.warning("Elo 原子写入失败（fixture=%s）：%s", fx, exc)
            detail["applied"] = False
            detail["reason"] = f"写入失败已回滚：{exc}"
            return detail
        self.applied += 1
        return detail

    def _matches_of(self, team_id: str) -> int:
        """该队已计入的场次数，用于决定 K 值。用 elo_log 统计，避免再加一张表。"""
        try:
            conn = self.repo._connect()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM elo_log WHERE team_id=? AND competition=?",
                (str(team_id), self.competition),
            ).fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return 0  # 取不到就当新队，用大 K 快速收敛

    # ---- 批量回算（冷启动 / 迁移） ------------------------------------------
    def replay(self, matches: list[dict]) -> int:
        """按顺序重放一批历史赛果，让模型上线第一秒就有「经验值」。

        matches 元素需含 fixture_id / home_team_id / away_team_id /
        home_score / away_score，且**必须按时间正序**传入。
        返回实际计入的场次数。
        """
        done = 0
        for m in matches:
            try:
                hs, as_ = m.get("home_score"), m.get("away_score")
                if hs is None or as_ is None:
                    continue
                res = self.apply_match(
                    m.get("fixture_id") or m.get("id"),
                    m.get("home_team_id"), m.get("away_team_id"),
                    int(hs), int(as_), season=m.get("season"),
                )
                if res.get("applied"):
                    done += 1
            except Exception as exc:
                # 单场失败不能中断整批回算
                log.warning("Elo 回算失败（fixture=%s）：%s", m.get("fixture_id"), exc)
        return done
