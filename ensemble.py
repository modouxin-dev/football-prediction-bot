"""多模型集成与分层兜底 —— 把若干路独立预测合成一个最终结论。

为什么需要它
------------
项目原有口径是「泊松 + Elo」单条路径，赔率只用于展示对比。实测发现
（见 exp_ensemble.py / exp_ens2.py，英超三赛季 1140 场走前回测）：

    market(纯市场)      acc 54.04%  rps 0.1955   ← 最强
    poisson             acc 50.83%  rps 0.2089
    elo(分桶)           acc 50.87%  rps 0.2096
    prior(联赛基线)      acc 42.72%  rps 0.2342

市场是最强单模型，但把它当唯一答案有两个真实风险：

    1. **市场会缺席**。低级别联赛、早期赛事、冷门赛程常常没有赔率，
       库里 5.9 万场里有赔率的可能只是少数。此时必须有统计模型顶上。
    2. **单模型无冗余**。任一路算错（数据源抖动、参数跑飞）就直接输出错值，
       没有任何东西能拉回来。

因此这里的定位不是「用集成打败市场」，而是：

    · 有市场时 → 动态权重自动把大权给市场（eta=200 实测收敛到 0.796，
      与项目既有的 MARKET_BLEND_WEIGHT=0.75 相互印证）
    · 无市场时 → 统计模型按各自实测表现加权，实测 rps 0.2083，
      略优于最好的单统计模型 poisson 0.2089
    · 全都没有 → 联赛经验值兜底，永不返回 None

诚实结论（务必知悉）
--------------------
在这批英超数据上，**任何集成都无法超越纯市场**（等权 0.2049、
动态权重最优 0.1966，均差于市场 0.1955）。集成带来的是**鲁棒性与
兜底能力**，不是准确率提升。真正的准确率增量只能来自市场没有的
信息（伤停、首发阵容、xG 等实时数据），见 README「模型演进」章节。

算法：指数权重（Hedge / Multiplicative Weights）
------------------------------------------------
每路维护累积得分 s_i（取 −RPS，越大越好），权重 w_i ∝ exp(η·s_i/ n_i)。
理论 regret 上界 O(√(T·ln N))，即长期逼近「事后最优单模型」。

    为什么用「均值」而非「总和」：各路参与场次数不同（市场常缺席），
    用总和会让出场少的路被不公平放大。

    为什么 η 默认 200：RPS 量级 0.2、模型间差距仅 0.01~0.02，
    η 太小权重分不开（η=12 时市场权重仅 0.49，几乎等于均分）；
    η=200 实测收敛到 0.796，兼顾「跟随强者」与「保留修正空间」。

状态持久化
----------
动态权重依赖累积得分，进程重启会丢失。这里把它做成可序列化的
`to_dict` / `from_dict`，由调用方决定存 JSON 还是入库。
默认 `learn=False` 时使用固定的经验权重，行为完全确定、便于测试。

纯 Python 实现 —— scipy / numpy 都不在 requirements.txt，
生产环境导入会直接 ImportError。
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

__all__ = [
    "MODEL_NAMES",
    "DEFAULT_ETA",
    "FALLBACK_PROBS",
    "EnsemblePredictor",
    "normalize_probs",
    "mix_probs",
]

#: 模型池。顺序无关，仅作默认枚举。
#:   poisson 攻防强度 × 联赛均值的泊松模型
#:   dc      泊松 + Dixon-Coles ρ 修正（低比分相关）
#:   elo     按 Elo 差分桶的历史胜平负频率（非参数，稳定）
#:   market  多家博彩共识赔率去水
#:   prior   联赛基线频率（冷启动 / 最终兜底）
MODEL_NAMES: tuple[str, ...] = ("poisson", "dc", "elo", "market", "prior")

DEFAULT_ETA = 200.0

#: 兜底概率。刻意不用 1/3 均分 —— 足球主场确实占优，
#: 与 backtest.WalkForwardBacktester._default_probs 保持同一经验值。
FALLBACK_PROBS: tuple[float, float, float] = (0.45, 0.27, 0.28)

_EPS = 1e-9


def normalize_probs(probs: Sequence[float]) -> tuple[float, float, float]:
    """归一化三项概率；非法输入（和 ≤ 0、含负）时回退到兜底值。"""
    try:
        h, d, a = float(probs[0]), float(probs[1]), float(probs[2])
    except (TypeError, ValueError, IndexError):
        return FALLBACK_PROBS
    if math.isnan(h) or math.isnan(d) or math.isnan(a):
        return FALLBACK_PROBS
    h, d, a = max(h, 0.0), max(d, 0.0), max(a, 0.0)
    total = h + d + a
    if total <= _EPS:
        return FALLBACK_PROBS
    return (h / total, d / total, a / total)


def mix_probs(
    parts: Iterable[tuple[Sequence[float], float]],
) -> tuple[float, float, float] | None:
    """按权重混合若干路概率。

    parts 元素为 (三路概率, 权重)。权重 ≤ 0 的路自动跳过。
    有效权重之和 ≤ 0 时返回 None —— 由调用方兜底，绝不臆造概率。
    """
    acc = [0.0, 0.0, 0.0]
    total_w = 0.0
    for probs, w in parts:
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        p = normalize_probs(probs)
        for i in range(3):
            acc[i] += w * p[i]
        total_w += w
    if total_w <= _EPS:
        return None
    return normalize_probs((acc[0] / total_w, acc[1] / total_w, acc[2] / total_w))


class EnsemblePredictor:
    """多模型指数权重集成器，带分层兜底。

    典型用法::

        ens = EnsemblePredictor()
        out = ens.predict({
            "poisson": (0.45, 0.28, 0.27),
            "dc":      (0.44, 0.29, 0.27),
            "elo":     (0.50, 0.25, 0.25),
            "market":  (0.42, 0.30, 0.28),
            "prior":   (0.45, 0.25, 0.30),
        })
        # out = {"probs": (...), "weights": {...}, "used": [...],
        #        "fallback": False}

    缺失的路直接传 None 或不传即可，权重会自动重归一化。
    """

    def __init__(
        self,
        models: Sequence[str] | None = None,
        *,
        eta: float = DEFAULT_ETA,
        floor: float = 0.0,
        learn: bool = False,
    ) -> None:
        self.models: list[str] = list(models) if models is not None else list(MODEL_NAMES)
        if not self.models:
            raise ValueError("模型池不能为空")
        # η 必须有限且非负；负数会让「最差模型权重最大」，是静默的方向性错误
        try:
            eta = float(eta)
        except (TypeError, ValueError):
            eta = DEFAULT_ETA
        if math.isnan(eta) or eta < 0:
            eta = DEFAULT_ETA
        self.eta = eta
        # floor：每路保底权重。实测它会拖累集成（地板强制分给弱模型），
        # 默认 0。仅在希望「任何路都不被彻底淘汰」时才调大。
        try:
            floor = float(floor)
        except (TypeError, ValueError):
            floor = 0.0
        self.floor = min(max(floor, 0.0), 1.0)
        self.learn = bool(learn)
        self._scores: dict[str, float] = {n: 0.0 for n in self.models}
        self._counts: dict[str, int] = {n: 0 for n in self.models}

    # ---- 权重 ---------------------------------------------------------------
    def raw_weights(self) -> dict[str, float]:
        """全模型池的当前权重（未考虑可用性）。"""
        if not self.learn:
            # 固定经验权重（与项目既有的 MARKET_BLEND_WEIGHT=0.75 同源）：
            #   市场 0.75 —— 实测最强单模型
            #   prior 0.05 —— 只做正则化锚点，实测它最不准（rps 0.2342），
            #                 给大了会把整体拉低；它的主战场是「其他路全缺」
            #                 时由 fallback 机制接管
            #   其余统计模型平分剩下的 0.20
            w: dict[str, float] = {}
            if "market" in self.models:
                w["market"] = 0.75
            if "prior" in self.models:
                w["prior"] = 0.05
            rest = [n for n in self.models if n not in ("market", "prior")]
            if rest:
                per = max(1.0 - sum(w.values()), 0.0) / len(rest)
                for n in rest:
                    w[n] = per
            return {n: w.get(n, 0.0) for n in self.models}
        raw: dict[str, float] = {}
        avg: dict[str, float] = {}
        for n in self.models:
            avg[n] = (self._scores[n] / self._counts[n]
                      if self._counts[n] > 0 else 0.0)
        # 必须用**相对**得分：log 概率恒为负，直接用 exp(η·avg) 时
        # η 稍大就会让所有路一起下溢成 0，权重全空 → 预测退化成兜底值。
        # 减去最大值后最优路恒为 exp(0)=1，数值永远稳定。
        best = max(avg.values()) if avg else 0.0
        for n in self.models:
            gap = self.eta * (avg[n] - best)
            # gap ≤ 0，再夹下界防止极端 η 下 exp 下溢把该路静默置 0
            raw[n] = math.exp(max(min(gap, 0.0), -50.0))
        if self.floor > 0:
            s = sum(raw.values())
            if s > _EPS:
                n = len(self.models)
                raw = {k: (1.0 - self.floor) * v / s + self.floor / n
                       for k, v in raw.items()}
        return raw

    def weights(self, available: Iterable[str] | None = None
                ) -> dict[str, float]:
        """只看**可用**模型的权重，缺失路归零后重归一化。

        这就是兜底的第一层：市场缺席时，原本给市场的权重会按剩余各路的
        相对实力重新分配，而不是让空缺拖着整个预测走偏。
        """
        raw = self.raw_weights()
        if available is None:
            avail = set(self.models)
        else:
            avail = {n for n in available if n in raw}
        picked = {n: v for n, v in raw.items() if n in avail and v > 0}
        if not picked:
            return {}
        s = sum(picked.values())
        if s <= _EPS:
            # 全部为 0（极端：η 极大且得分极差）→ 退化为均分
            k = len(picked)
            return {n: 1.0 / k for n in picked}
        return {n: v / s for n, v in picked.items()}

    # ---- 预测 ---------------------------------------------------------------
    def predict(
        self, probs_by_model: Mapping[str, Sequence[float] | None],
    ) -> dict:
        """把各路预测合成最终概率。

        probs_by_model 的值可为 None（该路本次不可用）。
        返回 dict：
            probs     最终三项概率 (主胜, 平, 客胜)
            weights   实际生效的 {模型: 权重}（已归一化）
            used      参与本次融合的模型名列表
            fallback  True 表示走到了兜底（没有任何可用模型）
            dropped   被忽略的模型名列表（值为 None 或不在模型池内）
        """
        dropped: list[str] = []
        usable: dict[str, tuple[float, float, float]] = {}
        for name, value in (probs_by_model or {}).items():
            if name not in self._scores:
                dropped.append(name)
                continue
            if value is None:
                dropped.append(name)
                continue
            usable[name] = normalize_probs(value)

        # 兜底最后一层：一路都没有 → 联赛经验值
        if not usable:
            return {
                "probs": FALLBACK_PROBS,
                "weights": {},
                "used": [],
                "fallback": True,
                "dropped": dropped,
            }

        w = self.weights(usable.keys())
        if not w:
            # 权重全零：退化为均分，仍比返回兜底值更贴近各路共识
            k = len(usable)
            w = {n: 1.0 / k for n in usable}
        mixed = mix_probs([(usable[n], w[n]) for n in usable])
        if mixed is None:
            return {
                "probs": FALLBACK_PROBS,
                "weights": {},
                "used": [],
                "fallback": True,
                "dropped": dropped,
            }
        return {
            "probs": mixed,
            "weights": dict(w),
            "used": sorted(usable.keys()),
            "fallback": False,
            "dropped": dropped,
        }

    # ---- 学习 ---------------------------------------------------------------
    def update(
        self,
        probs_by_model: Mapping[str, Sequence[float] | None],
        actual: int,
        *,
        metric: str = "rps",
    ) -> None:
        """用一场已完赛结果更新各路累积得分。

        actual: 0=主胜 1=平 2=客胜。得分取负值（RPS/LogLoss 越低越好）。
        只在 learn=True 时有意义；learn=False 时调用它是安全的空操作，
        这样调用方不必到处判断开关。
        """
        if not self.learn:
            return
        if actual not in (0, 1, 2):
            return
        for name, value in (probs_by_model or {}).items():
            if name not in self._scores or value is None:
                continue
            p = normalize_probs(value)
            if metric == "logloss":
                # −log p(actual)，p 极小时夹住避免 inf
                p_actual = max(p[actual], 1e-6)
                score = math.log(p_actual)
            else:
                score = -_rps(p, actual)
            self._scores[name] += score
            self._counts[name] += 1

    # ---- 持久化 -------------------------------------------------------------
    def to_dict(self) -> dict:
        """导出可序列化状态（η、地板、各路得分与计数）。"""
        return {
            "models": list(self.models),
            "eta": self.eta,
            "floor": self.floor,
            "learn": self.learn,
            "scores": dict(self._scores),
            "counts": dict(self._counts),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "EnsemblePredictor":
        """从 to_dict 的输出恢复。字段缺失时用默认值，不抛异常。"""
        models = data.get("models") or list(MODEL_NAMES)
        ens = cls(
            models,
            eta=data.get("eta", DEFAULT_ETA),
            floor=data.get("floor", 0.0),
            learn=bool(data.get("learn", False)),
        )
        scores = data.get("scores") or {}
        counts = data.get("counts") or {}
        for n in ens.models:
            try:
                ens._scores[n] = float(scores.get(n, 0.0))
                ens._counts[n] = max(int(counts.get(n, 0)), 0)
            except (TypeError, ValueError):
                ens._scores[n] = 0.0
                ens._counts[n] = 0
        return ens


def _rps(probs: Sequence[float], actual: int) -> float:
    """排序概率分数（越低越好）。本地实现，避免与 backtest 循环导入。"""
    total = 0.0
    cum_p = cum_a = 0.0
    for i in range(3):
        cum_p += probs[i]
        cum_a += 1.0 if i == actual else 0.0
        total += (cum_p - cum_a) ** 2
    return total / 2.0
