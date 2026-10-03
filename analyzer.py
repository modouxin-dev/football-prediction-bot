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
PRIOR_GAMES = 3  # 收缩强度：相当于给每支球队补 3 场"先验水平"的比赛
# 取值依据：3 个英超赛季 930 场走前扫描（见 season_prior 模块文档）。
# prior=5 命中 50.0% / 赛季初低估强队 12.1pp；prior=3 命中 50.3% / 低估 9.5pp。
# 再往下降（2/1）命中率不再上升、Log Loss 明显变差，故取 3。
OUTCOMES = ("home", "draw", "away")

# Elo 融合系数的安全边界。即便上游算错，λ 最多偏移 25%，不会把模型带崩。
# 与 elo.ELO_CLAMP 保持一致，此处再夹一次防止调用方传入越界值。
ELO_FACTOR_MIN = 0.8
ELO_FACTOR_MAX = 1.25

# ── DSA（Dynamic Strength Adjustment）动态强度调节 ────────────────────────
# 阶梯式时间衰减权重：越近的比赛权重越高，但仍保留全部历史作为"底色"，
# 避免只砍最近 N 场导致样本量骤减、方差放大。
#   index 0~4   → 核心状态区（捕捉瞬间爆发或崩盘）
#   index 5~14  → 趋势稳定区（基准权重）
#   index 15+   → 基础实力区（保留底层能力）
DSA_W_RECENT = 1.15     # 最近 5 场
DSA_W_MID = 1.0         # 第 6~15 场
DSA_W_TAIL = 0.6        # 第 16 场及更远
# ⚠️ 权重经回测下调，非原始提案值。
# 提案的 2.0/1.0/0.5 在 4 个场景（2 seed × drift 0/1）**全部使 Log Loss 变差**
# （平均 +0.00523）——权重差异过大会放大方差，代价超过"贴合近期状态"的收益。
# 扫描后 1.15/1.0/0.6 是唯一平均为负的组合（平均 -0.00043）。
DSA_RECENT_N = 5        # 前 5 场用 W_RECENT
DSA_MID_N = 15          # 第 6~15 场用 W_MID（即 index < 15）

# 安全阀：λ 相对联赛平均的允许波动区间。防止极端样本（如单场 8-0）
# 把强度瞬间带偏。注意本文件的强度是"÷ 联赛平均"后的相对值，
# 故赛季平均值 λ_season ≡ 1.0，区间即 [0.7, 1.3]。
DSA_CLAMP_LO = 0.7
DSA_CLAMP_HI = 1.3


def poisson_pmf(lmbda: float, k: int) -> float:
    return math.exp(-lmbda) * (lmbda**k) / math.factorial(k)


# ============================================================================
# Dixon-Coles 低比分修正
# ============================================================================
# 标准泊松假设主客队进球相互独立，这会系统性低估 0-0 与 1-1、高估 1-0 与 0-1。
# Dixon & Coles (1997) 引入修正项 τ 与参数 ρ，只调整这四个格子的概率，其余不变。
#
# 严格按原文定义（λ = 主队期望进球，μ = 客队期望进球）：
#     τ(0,0) = 1 − λμρ
#     τ(0,1) = 1 + λρ
#     τ(1,0) = 1 + μρ
#     τ(1,1) = 1 − ρ
#     其余   = 1
# ρ 通常为负（约 −0.1），此时 0-0 与 1-1 被抬高、1-0 与 0-1 被压低。

DEFAULT_RHO = 0.0        # 默认关闭（等价于纯泊松）；经回测验证后再决定是否启用
RHO_SEARCH_MIN = -0.30   # ρ 拟合搜索下界
RHO_SEARCH_MAX = 0.30    # ρ 拟合搜索上界
RHO_MIN_SAMPLES = 20     # 少于这么多场样本时不拟合，直接退回 0.0


def dc_tau(x: int, y: int, lambda_home: float, lambda_away: float, rho: float) -> float:
    """Dixon-Coles 修正项 τ(x, y)。严格遵循 Dixon & Coles (1997) 原文。"""
    if rho == 0.0:
        return 1.0
    if x == 0 and y == 0:
        return 1.0 - lambda_home * lambda_away * rho
    if x == 0 and y == 1:
        return 1.0 + lambda_home * rho
    if x == 1 and y == 0:
        return 1.0 + lambda_away * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def dc_rho_bounds(lambda_home: float, lambda_away: float) -> tuple[float, float]:
    """保证全部 τ ≥ 0 的 ρ 可行区间。

    由四个约束联立求解：
        1 − λμρ ≥ 0  →  ρ ≤ 1/(λμ)
        1 + λρ  ≥ 0  →  ρ ≥ −1/λ
        1 + μρ  ≥ 0  →  ρ ≥ −1/μ
        1 − ρ   ≥ 0  →  ρ ≤ 1
    """
    if lambda_home <= 0 or lambda_away <= 0:
        return (0.0, 0.0)
    lo = max(-1.0 / lambda_home, -1.0 / lambda_away)
    hi = min(1.0 / (lambda_home * lambda_away), 1.0)
    if lo > hi:
        return (0.0, 0.0)
    return (lo, hi)


def clamp_rho(rho: float | None, lambda_home: float, lambda_away: float) -> float:
    """把 ρ 夹进可行区间，防止 τ 为负产生负概率；非法值（NaN/inf）退回 0。"""
    if rho is None:
        return 0.0
    try:
        value = float(rho)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    lo, hi = dc_rho_bounds(lambda_home, lambda_away)
    return min(max(value, lo), hi)


def dc_score_matrix(lambda_home: float, lambda_away: float,
                    max_goals: int = MAX_GOALS, rho: float = 0.0) -> list[list[float]]:
    """经 Dixon-Coles 修正并归一化的比分概率矩阵。

    rho=0 时与纯泊松逐元素等价（τ 恒为 1），故向后兼容。
    """
    n = max_goals + 1
    # 夹紧 λ，防止极端数据导致 exp 下溢/上溢
    lh = min(max(float(lambda_home), 1e-9), 20.0)
    la = min(max(float(lambda_away), 1e-9), 20.0)
    r = clamp_rho(rho, lh, la)

    p_home = [poisson_pmf(lh, k) for k in range(n)]
    p_away = [poisson_pmf(la, k) for k in range(n)]
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            val = p_home[i] * p_away[j] * dc_tau(i, j, lh, la, r)
            if not math.isfinite(val) or val <= 0.0:
                val = 0.0     # ρ 已夹紧，理论上不会为负；仍兜底防 NaN
            matrix[i][j] = val

    total = sum(sum(row) for row in matrix)
    if not math.isfinite(total) or total <= 0:
        # 退化路径：退回纯泊松，宁可不准也不能崩
        matrix = [[ph * pa for pa in p_away] for ph in p_home]
        total = sum(sum(row) for row in matrix)
        if not math.isfinite(total) or total <= 0:
            return [[1.0 / (n * n)] * n for _ in range(n)]
    return [[v / total for v in row] for row in matrix]


def _dc_log_likelihood(rho: float, observations: list[tuple],
                       max_goals: int = MAX_GOALS) -> float:
    """给定 ρ 时，全部观测赛果的对数似然（越大越好）。"""
    total = 0.0
    for lh, la, hs, as_ in observations:
        if hs is None or as_ is None or hs > max_goals or as_ > max_goals:
            continue
        p = dc_score_matrix(lh, la, max_goals=max_goals, rho=rho)[hs][as_]
        if p <= 0.0:
            return -1e18     # 不可能事件
        total += math.log(p)
    return total


def fit_rho(observations: list[tuple], max_goals: int = MAX_GOALS,
            lo: float = RHO_SEARCH_MIN, hi: float = RHO_SEARCH_MAX,
            iterations: int = 40) -> float:
    """用黄金分割搜索拟合 ρ（最大化对数似然）—— 适应不同联赛的进球分布。

    observations: [(lambda_home, lambda_away, home_score, away_score), ...]
    样本不足或全部不可用时返回 0.0（退回纯泊松），绝不猜测。
    """
    obs = [
        o for o in observations
        if o[2] is not None and o[3] is not None
        and o[2] <= max_goals and o[3] <= max_goals
    ]
    if len(obs) < RHO_MIN_SAMPLES:
        return 0.0

    # 黄金分割（最大化）：单峰假设下稳定收敛，无需导数
    inv_phi = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    c = b - inv_phi * (b - a)
    d = a + inv_phi * (b - a)
    fc = _dc_log_likelihood(c, obs, max_goals)
    fd = _dc_log_likelihood(d, obs, max_goals)
    for _ in range(iterations):
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - inv_phi * (b - a)
            fc = _dc_log_likelihood(c, obs, max_goals)
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = _dc_log_likelihood(d, obs, max_goals)
        if abs(b - a) < 1e-6:
            break
    best = c if fc > fd else d
    if not math.isfinite(best):
        return 0.0
    return round(best, 6)


def dsa_weight(index: int) -> float:
    """按"由近到远"的场次序号返回阶梯权重。

    index=0 表示最近一场。所有权重恒为 1.0 时，DSA 退化为简单算术平均
    （见 ``build_league_model`` 的等价性保证）。
    """
    if index < 0:
        return DSA_W_TAIL
    if index < DSA_RECENT_N:
        return DSA_W_RECENT
    if index < DSA_MID_N:
        return DSA_W_MID
    return DSA_W_TAIL


def _dsa_aggregate(pairs: Iterable[tuple[str, float]]) -> tuple[float, float]:
    """把 (日期, 进球数) 序列聚合成加权后的 (总进球, 总权重)。

    返回 ``(weighted_goals, total_weight)``；除以即可得到加权场均。
    序列为空时返回 (0.0, 0.0)，由调用方决定如何兜底。

    排序规则：日期降序（最近在前）；日期缺失或相同时，保持输入顺序，
    保证结果可复现（不引入随机性）。
    """
    items = list(pairs)
    if not items:
        return 0.0, 0.0
    # 稳定排序：仅按日期降序，同日期不打乱原顺序
    ordered = sorted(enumerate(items), key=lambda e: (e[1][0], -e[0]), reverse=True)
    w_goals = 0.0
    w_total = 0.0
    for rank, (_orig_idx, (_date, goals)) in enumerate(ordered):
        w = dsa_weight(rank)
        w_goals += float(goals) * w
        w_total += w
    return w_goals, w_total


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


def build_league_model(rows: Iterable[dict], prior_games: int = PRIOR_GAMES,
                       match_logs: dict | None = None,
                       use_dsa_clamp: bool = False,
                       prior_strength: dict | None = None) -> LeagueModel:
    """由积分榜行（API-Football /standings）计算联赛均值与每队主客场攻防强度。

    ⛔ DSA 状态：未启用 / 未验证有效 / 实测为负
    -----------------------------------------
    ``match_logs`` 相关的加权逻辑是 opt-in 的**死代码路径**：线上
    ``service.py`` 与 ``analytics.py`` 的调用点均不传 ``match_logs``，
    实际走原逻辑，两者行为完全一致。

    20 场景合成回测（10 seed × drift 0/1）：平均 Δ Log Loss
    **+0.002747（变差）**，改善场景 **1/20**。详见 ARCHITECTURE.md
    「四·补」章节。**请勿启用**，等真实样本 ≥60 场已完赛再回来验证。

    参数
    ----
    rows
        积分榜聚合行，每行含 ``team.id`` 与 ``home/away`` 的 ``played``、
        ``goals.for``、``goals.against``。
    match_logs
        **可选**的逐场比赛日志，用于启用 DSA 动态强度调节::

            {team_id: {
                "home_for":     [(date, 进球数), ...],   # 主场进球
                "home_against": [(date, 失球数), ...],
                "away_for":     [(date, 进球数), ...],
                "away_against": [(date, 失球数), ...],
            }}

        传入后，对应球队改用**加权平均**；未提供日志的球队自动回退到
        聚合行（原逻辑），因此本参数对调用方完全可选、不破坏既有接口。
    use_dsa_clamp
        是否启用 ±30% 安全阀。默认 **关闭**——见下方"安全阀须知"。

    等价性保证
    ----------
    当所有权重恒为 1.0（或未提供 ``match_logs``）时，加权场均退化为
    ``ΣG/ΣN``，与改造前逐字节一致。安全阀关闭时该等价性严格成立。

    ⚠️ 安全阀须知
    -------------
    本文件的强度是"÷ 联赛平均"后的**相对值**，弱队进攻 / 强队防守天然
    会低于 0.7（例如 19 场 0 进球的球队约为 0.21）。开启 ±30% 会把这些
    真实存在的极端值强行拉回 0.7，**改变现有模型行为**，且会使上述
    等价性失效。故默认关闭，需真实回测数据支撑后再决定是否开启。
    """
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

    def shrink(goals: float, games: float, league_avg: float,
               prior_target: float = 1.0) -> float:
        """向先验目标收缩后的场均进球（失球）÷ 联赛平均。

        ``prior_target=1.0``（默认）即收缩到**联赛平均**，与改造前逐字节一致。
        传入该队自己的历史强度时，收缩到它的历史水平——赛季初样本极少时，
        这一步决定模型能否区分强弱（详见 ``season_prior`` 模块）。
        """
        return ((goals + prior_games * prior_target * league_avg)
                / (games + prior_games) / league_avg)

    def _ptarget(src: dict, key: str) -> float:
        """取先验目标值；缺失 / 非有限 / 非正时退回 1.0（联赛平均）。"""
        try:
            v = float(src.get(key, 1.0))
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(v) or v <= 0:
            return 1.0
        return min(max(v, 0.2), 3.0)

    def _clamp(value: float) -> float:
        if not use_dsa_clamp:
            return value
        return min(max(value, DSA_CLAMP_LO), DSA_CLAMP_HI)

    def _agg(logs: dict, key: str, fallback_goals: float,
             fallback_games: float) -> tuple[float, float]:
        """取该队某项数据的 (加权总进球, 加权总场次)。

        有逐场日志 → DSA 加权；否则回退到积分榜聚合值（权重等价于全 1.0）。
        """
        pairs = logs.get(key)
        if pairs:
            w_goals, w_games = _dsa_aggregate(pairs)
            if w_games > 0:
                return w_goals, w_games
        return fallback_goals, fallback_games

    teams: dict[int, TeamStrength] = {}
    for row in rows:
        team_id = (row.get("team") or {}).get("id")
        if team_id is None:
            continue
        home, away = row.get("home") or {}, row.get("away") or {}
        hg, ag = _num(home.get("played")), _num(away.get("played"))
        h_goals, a_goals = home.get("goals") or {}, away.get("goals") or {}
        logs = (match_logs or {}).get(team_id) or {}

        h_for_g, h_for_n = _agg(logs, "home_for",
                                _num(h_goals.get("for")), hg)
        h_aga_g, h_aga_n = _agg(logs, "home_against",
                                _num(h_goals.get("against")), hg)
        a_for_g, a_for_n = _agg(logs, "away_for",
                                _num(a_goals.get("for")), ag)
        a_aga_g, a_aga_n = _agg(logs, "away_against",
                                _num(a_goals.get("against")), ag)

        prior = (prior_strength or {}).get(team_id) or {}
        teams[team_id] = TeamStrength(
            attack_home=_clamp(shrink(h_for_g, h_for_n, avg_home,
                                      _ptarget(prior, "attack_home"))),
            defense_home=_clamp(shrink(h_aga_g, h_aga_n, avg_away,
                                       _ptarget(prior, "defense_home"))),
            attack_away=_clamp(shrink(a_for_g, a_for_n, avg_away,
                                      _ptarget(prior, "attack_away"))),
            defense_away=_clamp(shrink(a_aga_g, a_aga_n, avg_home,
                                       _ptarget(prior, "defense_away"))),
            games_home=int(hg),
            games_away=int(ag),
        )
    return LeagueModel(avg_home, avg_away, teams)


class MatchAnalyzer:
    @staticmethod
    def poisson_prob(lmbda: float, x: int) -> float:
        """泊松分布概率质量函数。"""
        return poisson_pmf(lmbda, x)

    def predict_match(self, model: LeagueModel, home_id: int, away_id: int,
                      elo_factor: float | None = None,
                      rho: float = DEFAULT_RHO) -> dict:
        h, a = model.strength(home_id), model.strength(away_id)
        return self.calculate_prediction(
            {"attack": h.attack_home, "defense": h.defense_home},
            {"attack": a.attack_away, "defense": a.defense_away},
            league_avg_home=model.avg_home_goals,
            league_avg_away=model.avg_away_goals,
            elo_factor=elo_factor,
            rho=rho,
        )

    def calculate_prediction(
        self,
        home_stats: dict,
        away_stats: dict,
        league_avg_home: float = DEFAULT_AVG_HOME_GOALS,
        league_avg_away: float = DEFAULT_AVG_AWAY_GOALS,
        max_goals: int = MAX_GOALS,
        elo_factor: float | None = None,
        rho: float = DEFAULT_RHO,
    ) -> dict:
        """
        home_stats: 主队主场 {'attack': 进攻强度, 'defense': 防守强度}
        away_stats: 客队客场 {'attack': ..., 'defense': ...}
        （强度 1.0 = 联赛平均；防守强度 <1 表示失球比平均少，防守更好）

        elo_factor: Elo 融合系数（由 elo.elo_multiplier 计算，>1 表示主队更强）。
            采用「份额归一」：先算主队进球占比 s，用系数把占比平移为 s'，
            再按原总进球数还原。因此 λ主+λ客 **精确守恒**，
            大小球（over/under）判断完全不受 Elo 影响，只在两队间重新分配。
            传 None 时保持纯泊松行为（向后兼容，已有测试不受影响）。
        """
        lambda_home = home_stats["attack"] * away_stats["defense"] * league_avg_home
        lambda_away = away_stats["attack"] * home_stats["defense"] * league_avg_away
        if elo_factor is not None and elo_factor > 0:
            f = min(max(float(elo_factor), ELO_FACTOR_MIN), ELO_FACTOR_MAX)
            total = lambda_home + lambda_away
            if total > 0:
                share = lambda_home / total
                # logit 平移：s' = s·f / (s·f + (1−s))，保证 s'∈(0,1) 且 f=1 时不变
                tilted = share * f
                denom = tilted + (1.0 - share)
                if denom > 0:
                    share_new = tilted / denom
                    lambda_home = total * share_new
                    lambda_away = total * (1.0 - share_new)
        lambda_home = min(max(lambda_home, 0.05), 6.0)
        lambda_away = min(max(lambda_away, 0.05), 6.0)

        n = max_goals + 1
        rho = clamp_rho(rho, lambda_home, lambda_away)
        # rho=0 时与纯泊松逐元素等价，行为完全不变
        matrix = dc_score_matrix(lambda_home, lambda_away,
                                 max_goals=max_goals, rho=rho)

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
            "rho": rho,
            "dixon_coles": abs(rho) > 1e-12,
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


def implied_probabilities(odds: dict | None) -> dict[str, float]:
    """去水后的市场隐含概率（multiplicative normalization）。

    博彩赔率含抽水，Σ(1/赔率) 恒 > 1（英超实测均值 1.055）。直接拿 1/赔率
    当概率会把市场概率系统性算高，与模型概率比较时市场「看起来更自信」，
    于是模型与市场的差距被虚报得更大。

    归一化（除以三项之和）后三项和恰为 1，才是可与模型概率直接相减的口径。

    与 analyze_value / edge 的关系（两个口径不能混用）：
        · edge = 模型概率 − 1/赔率   → 含抽水，等价于 EV = p×赔率 − 1，
          回答「这一注划不划算」，是 Value Bet 的正确判据，保持不变。
        · 本函数                     → 去水，回答「模型相对市场有没有信息优势」。
    前者是下注口径，后者是对比口径，两者差值恒为抽水量。

    无效输入（None / 非数字 / ≤1.0）整盘作废返回 {}：部分计算会得到一个
    看起来合理但方向错误的数，宁可让调用方显示「暂无」，也不能给错的对比。
    """
    if not odds:
        return {}
    raw: dict[str, float] = {}
    for key in OUTCOMES:
        try:
            price = float(odds.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {}
        if not price or price <= 1.0 or price != price:  # NaN 自检
            return {}
        raw[key] = 1.0 / price
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {key: value / total for key, value in raw.items()}


def market_gap(model_probs: dict, odds: dict | None) -> dict[str, float]:
    """模型概率 − 去水市场概率（百分点差值，正＝模型更看好）。

    去水失败时返回 {}，由调用方按「暂无可比数据」处理。
    """
    market = implied_probabilities(odds)
    if not market:
        return {}
    return {
        key: model_probs.get(key, 0.0) - market[key]
        for key in OUTCOMES
        if key in market
    }


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
