"""泊松模型与赔率工具（纯计算，不做网络请求，便于测试）。

模型（Maher 泊松模型）：
    λ_主 = 主队主场进攻强度 × 客队客场防守强度 × 联赛主队场均进球
    λ_客 = 客队客场进攻强度 × 主队主场防守强度 × 联赛客队场均进球
强度 = 球队场均进球（失球）÷ 联赛平均，并向 1.0 收缩，避免赛季初样本太少时出现极端值。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

MAX_GOALS = 10  # 比分矩阵覆盖 0~10 球，再整体归一化
DEFAULT_AVG_HOME_GOALS = 1.5
DEFAULT_AVG_AWAY_GOALS = 1.2
PRIOR_GAMES = 5  # 收缩强度：相当于给每支球队补 5 场"联赛平均水平"的先验比赛
OUTCOMES = ("home", "draw", "away")


def poisson_pmf(lmbda: float, k: int) -> float:
    return math.exp(-lmbda) * (lmbda**k) / math.factorial(k)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class TeamStrength:
    attack_home: float = 1.0
    defense_home: float = 1.0
    attack_away: float = 1.0
    defense_away: float = 1.0
    games_home: int = 0
    games_away: int = 0


@dataclass(frozen=True)
class LeagueModel:
    avg_home_goals: float = DEFAULT_AVG_HOME_GOALS
    avg_away_goals: float = DEFAULT_AVG_AWAY_GOALS
    teams: dict[int, TeamStrength] = field(default_factory=dict)

    def strength(self, team_id: int) -> TeamStrength:
        return self.teams.get(team_id, TeamStrength())


def build_league_model(rows: Iterable[dict], prior_games: int = PRIOR_GAMES) -> LeagueModel:
    """由积分榜行（API-Football /standings）计算联赛均值与每队主客场攻防强度。"""
    rows = list(rows)
    home_games = home_for = away_games = away_for = 0.0
    for row in rows:
        home, away = row.get("home") or {}, row.get("away") or {}
        home_games += _num(home.get("played"))
        home_for += _num((home.get("goals") or {}).get("for"))
        away_games += _num(away.get("played"))
        away_for += _num((away.get("goals") or {}).get("for"))

    avg_home = home_for / home_games if home_games else DEFAULT_AVG_HOME_GOALS
    avg_away = away_for / away_games if away_games else DEFAULT_AVG_AWAY_GOALS
    avg_home, avg_away = max(avg_home, 0.1), max(avg_away, 0.1)

    def shrink(goals: float, games: float, league_avg: float) -> float:
        """向联赛平均收缩后的场均进球（失球）÷ 联赛平均。"""
        return (goals + prior_games * league_avg) / (games + prior_games) / league_avg

    teams: dict[int, TeamStrength] = {}
    for row in rows:
        team_id = (row.get("team") or {}).get("id")
        if team_id is None:
            continue
        home, away = row.get("home") or {}, row.get("away") or {}
        hg, ag = _num(home.get("played")), _num(away.get("played"))
        h_goals, a_goals = home.get("goals") or {}, away.get("goals") or {}
        teams[team_id] = TeamStrength(
            attack_home=shrink(_num(h_goals.get("for")), hg, avg_home),
            defense_home=shrink(_num(h_goals.get("against")), hg, avg_away),
            attack_away=shrink(_num(a_goals.get("for")), ag, avg_away),
            defense_away=shrink(_num(a_goals.get("against")), ag, avg_home),
            games_home=int(hg),
            games_away=int(ag),
        )
    return LeagueModel(avg_home, avg_away, teams)


class MatchAnalyzer:
    @staticmethod
    def poisson_prob(lmbda: float, x: int) -> float:
        """泊松分布概率质量函数。"""
        return poisson_pmf(lmbda, x)

    def predict_match(self, model: LeagueModel, home_id: int, away_id: int) -> dict:
        h, a = model.strength(home_id), model.strength(away_id)
        return self.calculate_prediction(
            {"attack": h.attack_home, "defense": h.defense_home},
            {"attack": a.attack_away, "defense": a.defense_away},
            league_avg_home=model.avg_home_goals,
            league_avg_away=model.avg_away_goals,
        )

    def calculate_prediction(
        self,
        home_stats: dict,
        away_stats: dict,
        league_avg_home: float = DEFAULT_AVG_HOME_GOALS,
        league_avg_away: float = DEFAULT_AVG_AWAY_GOALS,
        max_goals: int = MAX_GOALS,
    ) -> dict:
        """
        home_stats: 主队主场 {'attack': 进攻强度, 'defense': 防守强度}
        away_stats: 客队客场 {'attack': ..., 'defense': ...}
        （强度 1.0 = 联赛平均；防守强度 <1 表示失球比平均少，防守更好）
        """
        lambda_home = home_stats["attack"] * away_stats["defense"] * league_avg_home
        lambda_away = away_stats["attack"] * home_stats["defense"] * league_avg_away
        lambda_home = min(max(lambda_home, 0.05), 6.0)
        lambda_away = min(max(lambda_away, 0.05), 6.0)

        n = max_goals + 1
        p_home = [poisson_pmf(lambda_home, k) for k in range(n)]
        p_away = [poisson_pmf(lambda_away, k) for k in range(n)]
        matrix = [[ph * pa for pa in p_away] for ph in p_home]
        total = sum(sum(row) for row in matrix)
        matrix = [[p / total for p in row] for row in matrix]  # 截断尾部后归一化，三项概率之和恒为 1

        win = sum(matrix[h][a] for h in range(n) for a in range(h))
        draw = sum(matrix[i][i] for i in range(n))
        loss = sum(matrix[h][a] for h in range(n) for a in range(h + 1, n))
        scores = sorted(
            ((f"{h}-{a}", matrix[h][a]) for h in range(n) for a in range(n)),
            key=lambda item: item[1],
            reverse=True,
        )
        over_2_5 = sum(matrix[h][a] for h in range(n) for a in range(n) if h + a >= 3)
        btts = sum(matrix[h][a] for h in range(1, n) for a in range(1, n))

        return {
            "win_prob": win,
            "draw_prob": draw,
            "loss_prob": loss,
            "best_score": scores[0][0],
            "top_scores": scores[:5],
            "over_2_5": over_2_5,
            "btts": btts,
            "lambda_home": lambda_home,
            "lambda_away": lambda_away,
        }

    @staticmethod
    def analyze_value(model_prob: float, odds: float) -> float:
        """价值偏差 = 模型概率 − 赔率隐含概率(1/赔率)。为正当且仅当期望收益为正。"""
        if not odds or odds <= 1.0:
            return 0.0
        return model_prob - 1 / odds

    def evaluate_outcomes(self, analysis: dict, odds: dict | None) -> dict:
        """对主/平/客三个结果分别计算价值偏差与期望收益。"""
        if not odds:
            return {}
        probs = {"home": analysis["win_prob"], "draw": analysis["draw_prob"], "away": analysis["loss_prob"]}
        result = {}
        for key in OUTCOMES:
            price = odds.get(key)
            if not price or price <= 1.0:
                continue
            result[key] = {
                "prob": probs[key],
                "odds": price,
                "edge": self.analyze_value(probs[key], price),
                "ev": probs[key] * price - 1,
            }
        return result


# ---- 赔率工具 -------------------------------------------------------------
_LABELS = {"home": "home", "draw": "draw", "away": "away", "1": "home", "x": "draw", "2": "away"}


def collect_1x2_odds(odds_response: Iterable[dict] | None) -> list[dict]:
    """从 /odds 响应提取各博彩公司的胜平负(Match Winner)赔率。"""
    rows: list[dict] = []
    for item in odds_response or []:
        for bookmaker in item.get("bookmakers") or []:
            for bet in bookmaker.get("bets") or []:
                if bet.get("id") != 1 and str(bet.get("name", "")).strip().lower() != "match winner":
                    continue
                prices: dict[str, float] = {}
                for value in bet.get("values") or []:
                    key = _LABELS.get(str(value.get("value", "")).strip().lower())
                    odd = _num(value.get("odd"))
                    if key and odd > 1.0:
                        prices[key] = odd
                if len(prices) == 3:
                    rows.append({"bookmaker": bookmaker.get("name") or "?", **prices})
                break
    return rows


def consensus_odds(rows: list[dict]) -> dict | None:
    """各博彩公司赔率的中位数（比平均值更抗异常报价）。"""
    if not rows:
        return None
    result: dict[str, Any] = {k: statistics.median(r[k] for r in rows) for k in OUTCOMES}
    result["n"] = len(rows)
    return result


def overround(odds: dict) -> float:
    """庄家抽水：隐含概率之和 − 1。"""
    return sum(1 / odds[k] for k in OUTCOMES) - 1


# ---- 模型信心等级 -------------------------------------------------------------
# 说明：这里评的是「模型对自己结论的把握程度」，不是实际命中率。
# 概率高 ≠ 一定赢，因此命名为「模型信心等级」而非「准确率等级」。
LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"
LEVEL_LOW = "low"

LEVEL_META = {
    LEVEL_HIGH: {"name": "高", "emoji": "🟢"},
    LEVEL_MEDIUM: {"name": "中", "emoji": "🟡"},
    LEVEL_LOW: {"name": "低", "emoji": "🔴"},
}

# 判定阈值：最高概率 + 领先第二名的差距，两个条件同时满足才算该等级
HIGH_MIN_PROB = 0.65
HIGH_MIN_GAP = 0.15
MEDIUM_MIN_PROB = 0.50
MEDIUM_MIN_GAP = 0.08

PROB_KEYS = ("home_win", "draw", "away_win")


def validate_probabilities(probabilities: dict) -> None:
    """校验概率字段：必须齐全、落在 0~1、总和约等于 1。"""
    for key in PROB_KEYS:
        if key not in probabilities:
            raise ValueError(f"缺少概率字段：{key}")
        value = float(probabilities[key])
        if not 0 <= value <= 1:
            raise ValueError(f"概率超出范围：{key}={value}")

    total = sum(float(probabilities[key]) for key in PROB_KEYS)
    if abs(total - 1.0) > 0.02:
        raise ValueError(f"概率总和异常：{total:.4f}")


def calculate_prediction_level(probabilities: dict) -> dict:
    """根据三项概率计算模型信心等级（🟢 高 / 🟡 中 / 🔴 低）。

    等级只由概率计算，不允许外部手工指定。
    """
    validate_probabilities(probabilities)

    values = {
        "主胜": float(probabilities["home_win"]),
        "平局": float(probabilities["draw"]),
        "客胜": float(probabilities["away_win"]),
    }
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=True)
    best_name, best_probability = ordered[0]
    second_probability = ordered[1][1]
    gap = best_probability - second_probability

    if best_probability >= HIGH_MIN_PROB and gap >= HIGH_MIN_GAP:
        key = LEVEL_HIGH
    elif best_probability >= MEDIUM_MIN_PROB and gap >= MEDIUM_MIN_GAP:
        key = LEVEL_MEDIUM
    else:
        key = LEVEL_LOW

    meta = LEVEL_META[key]
    return {
        "name": meta["name"],
        "emoji": meta["emoji"],
        "key": key,
        "result": best_name,
        "probability": best_probability,
        "gap": gap,
    }
