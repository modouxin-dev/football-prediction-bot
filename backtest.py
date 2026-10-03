"""回测与校准 / Backtest & calibration.

为什么要回测：
    命中率只能告诉你「蒙对了多少」，不能告诉你「该不该信这个模型」。
    本模块回答三个问题：
      1. Elo 是否真的提升了预测质量？（对照纯泊松）
      2. 信心等级是否有效？（高信心的命中率必须明显高于低信心）
      3. 概率是否校准？（说 70% 的场次是否真有 70% 赢）

核心纪律 —— **滚动前进（walk-forward），严禁前视偏差**：
    预测第 i 场时，只允许使用第 i 场**之前**的数据。
    用「全部历史」算出的 Elo 去评价「历史上的比赛」是作弊，
    会得到虚高的漂亮数字，上线后立刻失效。

指标选择：
    准确率太粗（1-0 和 1.01-0.99 的预测被同等对待），因此以
    **对数损失**和 **RPS** 为主判据，准确率仅作参考。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from analyzer import (
    DEFAULT_AVG_AWAY_GOALS, DEFAULT_AVG_HOME_GOALS, DEFAULT_RHO,
    MatchAnalyzer, build_league_model,
    calculate_prediction_level, fit_rho,
)
from elo import DEFAULT_RATING, elo_multiplier

log = logging.getLogger(__name__)

OUTCOME_HOME, OUTCOME_DRAW, OUTCOME_AWAY = 0, 1, 2
OUTCOME_NAMES = ("home", "draw", "away")

EPS = 1e-12          # 防止 log(0)
DEFAULT_MIN_HISTORY = 30   # 少于这么多场历史时不计入评估（样本太少的预测没意义）


# ============================================================================
# 一、纯函数指标
# ============================================================================

def outcome_index(home_score: int, away_score: int) -> int:
    """比分 → 结果索引（0 主胜 / 1 平 / 2 客胜）。"""
    if home_score > away_score:
        return OUTCOME_HOME
    if home_score < away_score:
        return OUTCOME_AWAY
    return OUTCOME_DRAW


def log_loss(probs: tuple[float, float, float], actual: int,
             *, eps: float = EPS) -> float:
    """对数损失：越低越好。对过度自信的错误预测惩罚极重。

    这是比准确率敏感得多的指标——把 30% 说成 90% 会被狠狠扣分。
    """
    p = min(max(float(probs[actual]), eps), 1.0)
    return -math.log(p)


def rps(probs: tuple[float, float, float], actual: int) -> float:
    """排序概率分数（Ranked Probability Score），越低越好。

    胜平负是有序的（平局介于主胜与客胜之间），RPS 会把「预测主胜但打平」
    判为比「预测主胜但客胜」更轻的错误——这是准确率做不到的。
    """
    total = 0.0
    cum_pred = cum_actual = 0.0
    for i in range(3):
        cum_pred += probs[i]
        cum_actual += 1.0 if i == actual else 0.0
        total += (cum_pred - cum_actual) ** 2
    return total / 2.0


def brier(probs: tuple[float, float, float], actual: int) -> float:
    """Brier 分数：越低越好。"""
    return sum((probs[i] - (1.0 if i == actual else 0.0)) ** 2 for i in range(3))


def accuracy(predicted: int, actual: int) -> int:
    return 1 if predicted == actual else 0


def favorite_index(probs: tuple[float, float, float]) -> int:
    return max(range(3), key=lambda i: probs[i])


# ---- 校准 -------------------------------------------------------------------

CALIBRATION_EDGES = (0.0, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 1.01)


def calibration_buckets(records: list[dict],
                        edges: tuple[float, ...] = CALIBRATION_EDGES) -> list[dict]:
    """校准曲线：把预测按概率分桶，比较「模型说的」与「实际发生的」。

    完美校准时，每个桶的 predicted 与 observed 应当接近。
    """
    buckets = [
        {"lo": edges[i], "hi": edges[i + 1], "n": 0, "predicted": 0.0, "observed": 0.0}
        for i in range(len(edges) - 1)
    ]
    for rec in records:
        p = float(rec["prob"])
        for b in buckets:
            if b["lo"] <= p < b["hi"]:
                b["n"] += 1
                b["predicted"] += p
                b["observed"] += 1.0 if rec["hit"] else 0.0
                break
    out = []
    for b in buckets:
        if b["n"] == 0:
            continue
        out.append({
            "range": f"{b['lo']:.2f}-{min(b['hi'], 1.0):.2f}",
            "n": b["n"],
            "predicted": b["predicted"] / b["n"],
            "observed": b["observed"] / b["n"],
        })
    return out


def calibration_error(buckets: list[dict]) -> float:
    """期望校准误差（ECE）：各桶 |predicted − observed| 的样本加权平均。越低越好。"""
    total = sum(b["n"] for b in buckets)
    if not total:
        return 0.0
    return sum(b["n"] * abs(b["predicted"] - b["observed"]) for b in buckets) / total


# ============================================================================
# 二、滚动回测引擎
# ============================================================================

@dataclass
class BacktestReport:
    """一路模型的回测结果。"""
    label: str = ""
    n: int = 0
    hits: int = 0
    log_loss_sum: float = 0.0
    rps_sum: float = 0.0
    brier_sum: float = 0.0
    by_level: dict[str, dict] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)

    @property
    def accuracy_rate(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def log_loss(self) -> float | None:
        """每场平均对数损失（越低越好）。"""
        return self.log_loss_sum / self.n if self.n else None

    @property
    def rps_score(self) -> float | None:
        return self.rps_sum / self.n if self.n else None

    @property
    def brier_score(self) -> float | None:
        return self.brier_sum / self.n if self.n else None

    @property
    def calibration(self) -> list[dict]:
        return calibration_buckets(self.records)

    @property
    def ece(self) -> float:
        return calibration_error(self.calibration)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "n": self.n,
            "accuracy": self.accuracy_rate,
            "log_loss": self.log_loss,
            "rps": self.rps_score,
            "brier": self.brier_score,
            "ece": self.ece,
            "by_level": {
                k: {"n": v["n"], "hit": v["hit"],
                    "rate": (v["hit"] / v["n"]) if v["n"] else None}
                for k, v in self.by_level.items()
            },
            "calibration": self.calibration,
        }


def _standings_row_from_history(stats: dict, use_sot: bool = False) -> dict:
    """把累积的主客场比赛统计，转成 build_league_model 需要的积分榜行格式。

    use_sot=True 且该队有射正数累积时，用**射正数**代替进球数作为强度指标。
    射正数样本量大（一场 4~6 次 vs 进球 1~2 个）、噪声小，赛季初收敛更快。
    没有射正数累积的球队自动退回进球口径。
    """
    if use_sot and stats.get("home_sot_played"):
        return {
            "team": {"id": stats["team_id"]},
            "home": {
                "played": stats["home_sot_played"],
                "goals": {"for": stats["home_sot_for"],
                          "against": stats["home_sot_against"]},
            },
            "away": {
                "played": stats["away_sot_played"],
                "goals": {"for": stats["away_sot_for"],
                          "against": stats["away_sot_against"]},
            },
        }
    return {
        "team": {"id": stats["team_id"]},
        "home": {
            "played": stats["home_played"],
            "goals": {"for": stats["home_for"], "against": stats["home_against"]},
        },
        "away": {
            "played": stats["away_played"],
            "goals": {"for": stats["away_for"], "against": stats["away_against"]},
        },
    }


def _blank_stats(team_id) -> dict:
    return {
        "team_id": team_id, "home_played": 0, "home_for": 0, "home_against": 0,
        "away_played": 0, "away_for": 0, "away_against": 0,
        # 射正数累积（可选）。场次与进球口径分开计数：
        # CSV 里偶尔缺射正字段，此时该队退回进球口径，不能让两个口径混算。
        "home_sot_played": 0, "home_sot_for": 0.0, "home_sot_against": 0.0,
        "away_sot_played": 0, "away_sot_for": 0.0, "away_sot_against": 0.0,
        # DSA 逐场日志：(序号, 进球数)。序号越大表示越晚发生（越"近"）。
        "home_for_log": [], "home_against_log": [],
        "away_for_log": [], "away_against_log": [],
    }


class WalkForwardBacktester:
    """滚动前进回测器。

    流程：按时间正序遍历比赛 → 用**当前累积的历史**生成预测 →
    与实际结果比对 → 再把本场赛果并入历史（供后续使用）。

    这样 Elo 与攻防强度都是真实的「当时可知」，不含未来信息。
    """

    def __init__(self, *, min_history: int = DEFAULT_MIN_HISTORY,
                 use_elo: bool = True, use_dc: bool = False,
                 competition: str = "BT", prior_games: int | None = None,
                 use_dsa: bool = False, use_dsa_clamp: bool = False,
                 use_sot: bool = False) -> None:
        self.min_history = max(0, int(min_history))
        # 射正数口径：一场射正 4~6 次 vs 进球 1~2 个，样本量大、噪声小。
        # 默认 False —— 未拿到射正数的数据源必须保持原有进球口径，
        # 否则两侧量纲不一致会静默算错。
        self.use_sot = bool(use_sot)
        self.use_elo = bool(use_elo)
        self.use_dc = bool(use_dc)
        # DSA：需要 history 里累积逐场日志（见 _observe）
        self.use_dsa = bool(use_dsa)
        self.use_dsa_clamp = bool(use_dsa_clamp)
        self.competition = competition
        self.prior_games = prior_games
        self.analyzer = MatchAnalyzer()
        # ρ：预热期结束后用预热数据拟合一次；之后每 refit_every 场重拟合一次
        # （只用截至当时的历史，仍无前视偏差）。样本少时 ρ 方差极大，
        # 固定拟合一次的噪声会盖过修正收益，故需要随数据积累持续收敛。
        self.rho: float = DEFAULT_RHO if use_dc else 0.0
        self._rho_fitted: bool = (not use_dc)
        self.refit_every: int = 100
        self.min_fit_samples: int = 200
        self._dc_observations: list[tuple] = []
        # 累积状态
        self._team_stats: dict[str, dict] = {}
        self._ratings: dict[str, float] = {}
        self._games: dict[str, int] = {}
        self._seen = 0

    # ---- 历史累积 -----------------------------------------------------------
    def _ensure(self, team_id):
        tid = str(team_id)
        if tid not in self._team_stats:
            self._team_stats[tid] = _blank_stats(tid)
            self._ratings[tid] = DEFAULT_RATING
            self._games[tid] = 0
        return self._team_stats[tid]

    def _observe(self, home_id, away_id, hs: int, as_: int,
                 home_sot: int | None = None, away_sot: int | None = None) -> None:
        """把一场赛果并入历史：更新攻防统计 + Elo 评分。

        home_sot / away_sot 为可选射正数。两者必须同时有效才计入，
        只给一边会让同一队的攻防用不同口径累积，量纲就乱了。
        """
        h, a = self._ensure(home_id), self._ensure(away_id)
        h["home_played"] += 1; h["home_for"] += hs; h["home_against"] += as_
        a["away_played"] += 1; a["away_for"] += as_; a["away_against"] += hs

        if home_sot is not None and away_sot is not None:
            h["home_sot_played"] += 1
            h["home_sot_for"] += home_sot; h["home_sot_against"] += away_sot
            a["away_sot_played"] += 1
            a["away_sot_for"] += away_sot; a["away_sot_against"] += home_sot

        # DSA 逐场日志：用处理序号充当时间轴（run() 要求 matches 按时间正序），
        # 序号越大 = 越近。不依赖真实日期字段，故对任何数据源都成立。
        seq = self._seen
        h["home_for_log"].append((seq, hs))
        h["home_against_log"].append((seq, as_))
        a["away_for_log"].append((seq, as_))
        a["away_against_log"].append((seq, hs))

        hid, aid = str(home_id), str(away_id)
        rh, ra = self._ratings[hid], self._ratings[aid]
        from elo import calculate_elo, goal_diff_multiplier, k_factor_for, outcome_from_score

        result = outcome_from_score(hs, as_)
        mult = goal_diff_multiplier(hs, as_)
        k = (k_factor_for(self._games[hid]) + k_factor_for(self._games[aid])) / 2.0
        new_h, new_a = calculate_elo(rh, ra, result, k, multiplier=mult)
        self._ratings[hid] = min(max(new_h, 1200.0), 2400.0)
        self._ratings[aid] = min(max(new_a, 1200.0), 2400.0)
        self._games[hid] += 1
        self._games[aid] += 1
        self._seen += 1

    # ---- 预测 ---------------------------------------------------------------
    def _predict(self, home_id, away_id) -> tuple[float, float, float]:
        """用当前累积历史预测一场比赛，返回 (主胜, 平, 客胜)。"""
        return self._predict_detail(home_id, away_id)[0]

    def _predict_detail(self, home_id, away_id):
        """返回 ((主胜, 平, 客胜), λ主, λ客)。λ 供 Dixon-Coles 的 ρ 拟合使用。"""
        rows = [_standings_row_from_history(s, self.use_sot)
                for s in self._team_stats.values()]
        kwargs = {} if self.prior_games is None else {"prior_games": self.prior_games}
        if self.use_dsa:
            match_logs = {}
            for s in self._team_stats.values():
                tid = s["team_id"]
                if any(s[k] for k in ("home_for_log", "home_against_log",
                                      "away_for_log", "away_against_log")):
                    match_logs[tid] = {
                        "home_for": s["home_for_log"],
                        "home_against": s["home_against_log"],
                        "away_for": s["away_for_log"],
                        "away_against": s["away_against_log"],
                    }
            kwargs["match_logs"] = match_logs
            kwargs["use_dsa_clamp"] = self.use_dsa_clamp
        model = build_league_model(rows, **kwargs) if rows else None

        if model is None or not model.teams:
            # 完全没有历史：退回联赛经验值，不做无根据的判断
            return self._default_probs(), None, None

        h = model.strength(self._as_int(home_id))
        a = model.strength(self._as_int(away_id))

        elo_factor = None
        if self.use_elo:
            rh = self._ratings.get(str(home_id), DEFAULT_RATING)
            ra = self._ratings.get(str(away_id), DEFAULT_RATING)
            elo_factor = elo_multiplier(rh, ra)

        # λ 基准必须是**进球**均值。
        # 射正数口径下 model.avg_*_goals 是射正均值（约 4~5），
        # 直接拿去算 λ 会得到「预期进球 4 个」这种荒谬值。
        # 强度是相对值（÷ 各自均值），所以换成进球均值即可量纲正确。
        avg_h, avg_a = model.avg_home_goals, model.avg_away_goals
        if self.use_sot:
            avg_h, avg_a = self._goal_avg()

        res = self.analyzer.calculate_prediction(
            {"attack": h.attack_home, "defense": h.defense_home},
            {"attack": a.attack_away, "defense": a.defense_away},
            league_avg_home=avg_h,
            league_avg_away=avg_a,
            elo_factor=elo_factor,
            rho=self.rho,
        )
        return ((res["win_prob"], res["draw_prob"], res["loss_prob"]),
                res["lambda_home"], res["lambda_away"])

    def _goal_avg(self) -> tuple[float, float]:
        """联赛场均进球（主/客），只看进球口径的累积。

        射正数模式下 λ 基准仍然用它——模型输出的是进球数，不是射正数。
        没有累积时退回联赛经验值（与 analyzer 默认值一致）。
        """
        gp = gf = ga_ = 0.0
        for s in self._team_stats.values():
            gp += s["home_played"]; gf += s["home_for"]; ga_ += s["home_against"]
        if gp <= 0:
            return DEFAULT_AVG_HOME_GOALS, DEFAULT_AVG_AWAY_GOALS
        # 主场进球 / 主场失球 == 客队在该场的进球
        return max(gf / gp, 0.1), max(ga_ / gp, 0.1)

    def _default_probs(self) -> tuple[float, float, float]:
        """无历史时的基准猜测。

        刻意不用 1/3 均分：足球主场确实占优，用联赛经验值作为冷启动起点，
        比无差别均分更接近真实分布。
        """
        return (0.45, 0.27, 0.28)

    @staticmethod
    def _as_int(team_id):
        try:
            return int(team_id)
        except (TypeError, ValueError):
            return team_id

    # ---- 主流程 -------------------------------------------------------------
    def run(self, matches: list[dict]) -> BacktestReport:
        """matches 需按时间正序，元素含 home_team_id/away_team_id/home_score/away_score。"""
        label = "poisson"
        if self.use_dc:
            label = "dc" if not self.use_elo else "elo+dc"
        elif self.use_elo:
            label = "elo"
        report = BacktestReport(label=label)
        for m in matches:
            try:
                hs, as_ = m.get("home_score"), m.get("away_score")
                if hs is None or as_ is None:
                    continue
                hs, as_ = int(hs), int(as_)
                hid, aid = m.get("home_team_id"), m.get("away_team_id")
                if hid is None or aid is None:
                    continue

                if self._seen < self.min_history and self.use_dc and not self._rho_fitted:
                    # 预热期：只收集 (λ主, λ客, 实际比分) 用于拟合 ρ，不参与评分
                    _, lh, la = self._predict_detail(hid, aid)
                    if lh is not None:
                        self._dc_observations.append((lh, la, hs, as_))

                if self._seen >= self.min_history:
                    # 预热结束：用**仅含历史**的数据拟合 ρ（严禁使用未来赛果）。
                    # 之后每 refit_every 场重拟合，让 ρ 随样本积累逐步收敛。
                    if self.use_dc and (
                        not self._rho_fitted
                        or (self.refit_every > 0
                            and self._seen % self.refit_every == 0
                            and len(self._dc_observations) >= self.min_fit_samples)
                    ):
                        self.rho = fit_rho(self._dc_observations)
                        self._rho_fitted = True
                    probs = self._predict(hid, aid)
                    actual = outcome_index(hs, as_)
                    pred = favorite_index(probs)
                    hit = accuracy(pred, actual)
                    report.n += 1
                    report.hits += hit
                    report.log_loss_sum += log_loss(probs, actual)
                    report.rps_sum += rps(probs, actual)
                    report.brier_sum += brier(probs, actual)

                    # 信心等级：用于检验「高信心是否真的更准」
                    level = calculate_prediction_level({
                        "home_win": probs[0], "draw": probs[1], "away_win": probs[2],
                    })
                    key = level.get("key") or "unknown"
                    slot = report.by_level.setdefault(key, {"n": 0, "hit": 0})
                    slot["n"] += 1
                    slot["hit"] += hit
                    report.records.append({
                        "fixture_id": m.get("fixture_id") or m.get("id"),
                        "prob": probs[pred],
                        "predicted": OUTCOME_NAMES[pred],
                        "actual": OUTCOME_NAMES[actual],
                        "hit": bool(hit),
                        "level": key,
                    })

                # 无论是否参与评估，赛果都要并入历史（否则历史会被污染）
                self._observe(hid, aid, hs, as_,
                              m.get("home_sot"), m.get("away_sot"))
                # ρ 拟合样本：评估期也要持续累积，供后续重拟合使用
                if self.use_dc and self._seen >= self.min_history:
                    _, lh2, la2 = self._predict_detail(hid, aid)
                    if lh2 is not None:
                        self._dc_observations.append((lh2, la2, hs, as_))
            except Exception as exc:
                log.warning("回测单场失败（fixture=%s）：%s", m.get("fixture_id"), exc)
        return report


# ============================================================================
# 三、对比与结论
# ============================================================================

def compare(baseline: BacktestReport, challenger: BacktestReport,
            *, min_delta: float = 0.001) -> dict:
    """把挑战者（challenger）与基线（baseline）对比，给出可操作结论。

    主判据是对数损失与 RPS（越低越好），准确率只作参考。
    """
    bl, cl = baseline.log_loss, challenger.log_loss
    br, cr = baseline.rps_score, challenger.rps_score
    ba, ca = baseline.accuracy_rate, challenger.accuracy_rate

    if baseline.n == 0 or challenger.n == 0:
        return {"verdict": "样本不足", "reason": f"基线 {baseline.n} 场 / 挑战者 {challenger.n} 场"}

    ll_delta = (bl - cl) if (bl is not None and cl is not None) else None
    rps_delta = (br - cr) if (br is not None and cr is not None) else None

    if ll_delta is None:
        verdict = "无法判断"
    elif ll_delta > min_delta and (rps_delta or 0) >= 0:
        verdict = "挑战者更优"
    elif ll_delta < -min_delta:
        verdict = "挑战者更差"
    else:
        verdict = "无显著差异"

    return {
        "baseline": baseline.to_dict(),
        "challenger": challenger.to_dict(),
        "log_loss_delta": ll_delta,       # 正数 = 挑战者更好
        "rps_delta": rps_delta,
        "accuracy_delta": (ca - ba) if (ba is not None and ca is not None) else None,
        "verdict": verdict,
    }


def level_monotonic(report: BacktestReport) -> dict:
    """检验信心等级是否单调：高信心命中率应 ≥ 中 ≥ 低。

    若不成立，说明信心分级是失效的装饰品。
    """
    rates = {}
    for key, slot in report.by_level.items():
        if slot["n"]:
            rates[key] = slot["hit"] / slot["n"]
    order = {"high": 3, "medium": 2, "low": 1}
    ordered = sorted(rates.items(), key=lambda kv: order.get(kv[0], 0))
    monotonic = all(
        ordered[i][1] >= ordered[i - 1][1] - 1e-9 for i in range(1, len(ordered))
    ) if len(ordered) > 1 else None
    return {"rates": rates, "ordered": ordered, "monotonic": monotonic}


def run_backtest(matches: list[dict], *, min_history: int = DEFAULT_MIN_HISTORY,
                 variant: str = "poisson") -> dict:
    """一键跑双路回测：纯泊松（基线） vs 指定变体（挑战者）。

    variant:
        "poisson" —— 纯 Maher/Poisson，与线上 /predict 口径一致（默认）
        "elo"     —— 泊松 + Elo 融合
        "dc"      —— 泊松 + Dixon-Coles 低比分修正

    默认取 "poisson"：线上 service.py 调用 predict_match() 时不传
    elo_home_advantage，即线上跑的就是纯泊松。回测默认必须与之对齐，
    否则报出来的数字描述的不是线上模型。
    """
    use_dc = (variant == "dc")
    use_elo = (variant == "elo")
    base = WalkForwardBacktester(min_history=min_history, use_elo=False,
                                 use_dc=False).run(matches)
    chal_tester = WalkForwardBacktester(min_history=min_history, use_elo=use_elo,
                                        use_dc=use_dc)
    chal = chal_tester.run(matches)
    return {
        "comparison": compare(base, chal),
        "baseline_levels": level_monotonic(base),
        "challenger_levels": level_monotonic(chal),
        # ρ 拟合在 tester 上（report 不携带），dc 变体才有意义
        "rho": chal_tester.rho if use_dc else None,
    }
