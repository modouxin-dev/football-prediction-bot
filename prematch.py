"""赛前实时信号：伤停与首发阵容 → 攻防修正因子 / Pre-match signals → strength multipliers.

为什么需要它 / Why this exists
-------------------------------
模型一直只用「赛季积分榜 + 近期战绩」估进球率 λ，这些都是**赛前很久就固化**
的信息。而一场比赛的真实条件里，还有两类只有临近开赛才拿得到的东西：

    The model has always estimated goal rates λ from season standings + recent
    form — information that is already stale long before kick-off. Two things
    only become available close to kick-off:

1. 伤停（/injuries）—— 谁**打不了**。
   Injuries: who **cannot** play.
2. 首发阵容（/fixtures/lineups）—— 谁**真上场**、站什么位置。
   Lineups: who **actually** starts, and in which position.

市场赔率会吸收这些信息（这也是为什么实测「市场 > 泊松」），
但**赔率反映的是所有人的共识，不含我们对「这份缺阵名单到底多严重」的判断**。
把这两类信号显式接入，模型才第一次拥有市场之外的独立信息。

    Market odds absorb this information (which is exactly why backtests show
    market > Poisson). But odds encode the *consensus*; they carry no view of
    ours on how severe a given absentee list really is. Wiring these signals in
    explicitly is the first time the model holds information the market lacks.

作用点 / Where it acts
----------------------
修正**只作用在进球率 λ 上**，不直接改三项概率：

    Adjustments act on the goal rates λ only — never directly on the three
    outcome probabilities:

    λ主 = 主进攻 · 客防守 · 联赛均值 · att_mult(主)
    λ客 = 客进攻 · 主防守 · 联赛均值 · att_mult(客)

这样比分矩阵、大小球、BTTS 全部随 λ 一起变，不会出现
「最可能比分是 1-0（主胜），三项概率却说客胜最高」这种自相矛盾。

    This keeps the score matrix, over/under and BTTS consistent with λ,
    avoiding contradictions such as "most likely score 1-0 (home win)" while
    the three-way probabilities say away is most likely.

诚实边界 / Honest limits
------------------------
* 权重**未经本项目语料校准** —— 回测语料（英超 CSV）根本没有伤停/阵容列，
  无法用历史数据拟合。这里的数值是保守量级，靠 `PREMATCH_STRENGTH` 调节，
  并需要线上 A/B（见 README「赛前信号」）积累真实样本后再定。

  These weights are **NOT calibrated on this project's corpus** — the backtest
  corpus (EPL CSV) has no injury/lineup columns at all, so no historical fit is
  possible. Values here are deliberately conservative, tunable via
  `PREMATCH_STRENGTH`, and must be re-fitted from live A/B samples later.

* 伤停端点**不含球员位置**（只有缺阵类型），因此无法做「前锋缺阵影响更大」
  这类位置加权；位置信息只在首发阵容里才有，且缺阵者不在首发中。
  于是折损按固定比例分配到攻防两端，见 `ATTACK_SHARE`。

  The injuries endpoint carries **no player position** (only absence type), so
  position-weighted loss ("a missing striker hurts more") is impossible here;
  position exists only in lineups, and absentees are by definition absent from
  it. Loss is therefore split between attack/defence at a fixed ratio.

* 无数据时因子**恒等于 1.0**（浮点精确），模型行为与接入前逐位一致。

  With no data the multipliers are **exactly 1.0** (float-exact), so model
  behaviour is bit-for-bit identical to before this module existed.
"""

from __future__ import annotations

import math
import re
from typing import Any

# 缺阵确定性权重 / Absence-certainty weights.
# 数据源给出的是「状态描述」而非布尔值：确定缺阵和「存疑」不能同等对待。
# The source gives a status string, not a boolean: a confirmed absence and a
# "questionable" one must not count equally.
CERTAIN_WEIGHT = 1.0      # "Missing Fixture" 之类：确定不打
DOUBTFUL_WEIGHT = 0.4     # "Questionable"/"Doubtful"：可能上场

_DOUBTFUL_HINTS = ("questionable", "doubtful", "50", "maybe")
_CERTAIN_HINTS = ("missing", "out", "suspend", "injur", "injured")

# 半饱和点：有效缺阵人数达到该值时折损封顶一半。
# 防止「10 人伤停」把 λ 打到 0 —— 真实球队再缺人也有下限。
# Half-saturation: load at which the loss reaches half of its ceiling.
# Stops "10 absentees" from driving λ to zero — real teams have a floor.
HALF_SATURATION = 4.0

# 折损在两端的分配 / How the loss splits between scoring and conceding:
#   attack  → 进球系数，≤ 1（少人 = 进球能力下降）
#   concede → 失球系数，≥ 1（少人 = 更容易被进球）
# 进攻端伤害更大：少一个前锋直接少进球，少一个后卫还能靠整体补位。
#   attack  → scoring multiplier, ≤ 1
#   concede → conceding multiplier, ≥ 1
# Attack is hurt more: a missing striker removes goals outright, a missing
# defender can be covered by the block.
ATTACK_SHARE = 0.6
CONCEDE_SHARE = 0.4

# 单个队伍折损的上限（相对值）：再严重的缺阵也不许让进攻力腰斩。
MAX_LOSS = 0.5

# 攻守倾向基准：以最经典的 4-4-2 为**中性点**（0），
#   4-3-3 / 3-4-3 偏进攻（>0），5-4-1 / 5-3-2 偏防守（<0）。
# 门将必上且不影响阵型倾向，中场算中性，故只看后卫与前锋人数之差。
# Tilt baseline: classic 4-4-2 is the **neutral** point (0);
#   4-3-3 / 3-4-3 attack (>0), 5-4-1 / 5-3-2 defend (<0).
# The keeper always plays and says nothing about shape; midfield is neutral,
# so only defender vs forward counts differ.
NEUTRAL_DEFENDERS = 4.0
NEUTRAL_FORWARDS = 2.0
_POS_WEIGHT = {"F": 1.0, "M": 0.35, "D": -1.0, "G": -1.0}   # 保留供后续细化
_FORMATION_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*[-–]?\s*(\d+)?")

# 可配置默认值 / Tunable defaults.
# strength=0 → 完全关闭修正（等价未接入），用于线上 A/B 对照。
DEFAULT_STRENGTH = 0.5     # 半强度：4 人缺阵约让本队进攻降 15%
STRENGTH_MIN, STRENGTH_MAX = 0.0, 1.0
DEFAULT_TILT = 0.0         # 阵容攻守倾向默认不启用（比伤停更不确定）
TILT_MAX = 0.2


def _as_float(value: Any, default: float = 0.0) -> float:
    """尽量取数字，失败给默认值（外部数据不可信，不能让解析异常打断预测）。"""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _absence_weight(reason: str) -> float:
    """缺阵状态 → 权重。未知状态按「确定缺阵」处理（宁可保守）。"""
    low = (reason or "").strip().lower()
    if not low:
        return CERTAIN_WEIGHT
    if any(h in low for h in _DOUBTFUL_HINTS):
        return DOUBTFUL_WEIGHT
    if any(h in low for h in _CERTAIN_HINTS):
        return CERTAIN_WEIGHT
    return CERTAIN_WEIGHT


def absence_load(injuries: list[dict[str, Any]] | None, team_id: Any) -> float:
    """某队的有效缺阵人数（按确定性加权）。无数据返回 0.0。"""
    if not injuries:
        return 0.0
    target = str(team_id)
    total = 0.0
    for item in injuries:
        if not isinstance(item, dict):
            continue
        tid = str(((item.get("team") or {}).get("id")))
        if tid != target:
            continue
        player = item.get("player")
        if not isinstance(player, dict) or not player:
            continue      # 连球员信息都没有的残缺记录不能当成「有人缺阵」
        reason = str(player.get("type") or player.get("reason") or "")
        total += _absence_weight(reason)
    return total


def loss_factor(load: float, strength: float = 1.0) -> float:
    """有效缺阵人数 → 实力保留系数 ∈ (0, 1]，0 人时精确返回 1.0。

    用半饱和曲线 n/(n+h) 而非线性，避免人数多时系数击穿下限。
    """
    n = _as_float(load)
    if n <= 0:
        return 1.0
    satur = n / (n + HALF_SATURATION)
    return 1.0 - min(max(satur * strength, 0.0), MAX_LOSS)


def _tilt(defenders: float, forwards: float, scale: float) -> float:
    """后卫/前锋人数 → 攻守倾向，4-4-2 为 0，正值偏进攻。"""
    return (forwards - (defenders - NEUTRAL_DEFENDERS) - NEUTRAL_FORWARDS) / scale


def starters_tilt(start_xi: list[dict[str, Any]] | None) -> float:
    """首发 11 人的位置分布 → 攻守倾向，范围约 [-1, 1]。无数据返回 0.0。"""
    if not start_xi:
        return 0.0
    defenders = forwards = 0
    for entry in start_xi:
        if not isinstance(entry, dict):
            continue
        pos = str(((entry.get("player") or {}).get("pos")) or "").strip().upper()
        if pos == "D":
            defenders += 1
        elif pos == "F":
            forwards += 1
    if defenders + forwards == 0:
        return 0.0
    return _tilt(defenders, forwards, 11.0)


def formation_tilt(formation: str | None) -> float:
    """阵型串（如 "4-3-3"）→ 攻守倾向。后卫数越多越保守。无法解析返回 0.0。"""
    match = _FORMATION_RE.search(formation or "")
    if not match:
        return 0.0
    defenders = _as_float(match.group(1))
    forwards = _as_float(match.group(3) or 0)
    if defenders <= 0:
        return 0.0
    return _tilt(defenders, forwards, 10.0)


def side_lineup(lineups: list[dict[str, Any]] | None, team_id: Any) -> dict[str, Any]:
    """从 lineups 响应里取出指定球队的那一段。"""
    target = str(team_id)
    for side in lineups or []:
        if not isinstance(side, dict):
            continue
        if str(((side.get("team") or {}).get("id"))) == target:
            return side
    return {}


def squad_factors(
    home_id: Any,
    away_id: Any,
    injuries: list[dict[str, Any]] | None = None,
    lineups: list[dict[str, Any]] | None = None,
    strength: float = 1.0,
    tilt_weight: float = 0.0,
    loads: dict[str, float] | None = None,
) -> dict[str, Any]:
    """合成两队的攻防修正因子。

    返回 {'home': {'attack':…, 'defence':…}, 'away': {...}, 'detail': {...}}。
    四个乘数在无任何信号时**全部精确为 1.0**。

    `loads` 允许调用方传入**已加权**的缺阵负荷（例如按球员位置加权，见
    `context.weighted_absence_load`）。不传时退化为按人数计。这样本模块
    无需知道「位置」这个概念——那属于球员评估的范畴，放在 context 里更合适，
    也避免两个模块循环依赖。
    """
    sides: dict[str, dict[str, Any]] = {}
    detail: dict[str, Any] = {}
    for key, tid in (("home", home_id), ("away", away_id)):
        load = (loads or {}).get(key)
        if load is None:
            load = absence_load(injuries, tid)
        keep = loss_factor(load, strength)
        attack_mult = 1.0 - (1.0 - keep) * ATTACK_SHARE
        concede_mult = 1.0 + (1.0 - keep) * CONCEDE_SHARE

        side = side_lineup(lineups, tid)
        start_xi = side.get("startXI") or []
        tilt = starters_tilt(start_xi)
        if tilt == 0.0:
            tilt = formation_tilt(side.get("formation"))
        if tilt and tilt_weight:
            # 偏进攻 → 进球系数上调，同时更容易失球（压上必然留出身后空间）
            delta = tilt * tilt_weight
            attack_mult = min(max(attack_mult + delta, 0.5), 1.5)
            concede_mult = min(max(concede_mult + delta * 0.5, 0.5), 1.5)

        sides[key] = {
            "attack": attack_mult,
            "concede": concede_mult,
        }
        detail[key] = {
            "absence_load": round(load, 3),
            "starters": len(start_xi) if isinstance(start_xi, list) else 0,
            "formation": side.get("formation") or "",
            "tilt": round(tilt, 3),
        }

    return {"home": sides["home"], "away": sides["away"], "detail": detail}


def apply_to_lambdas(
    lambda_home: float,
    lambda_away: float,
    factors: dict[str, Any] | None,
) -> tuple[float, float]:
    """把修正因子落到两个 λ 上。factors 为空时原样返回。

    λ主' = λ主 · 主attack(≤1) · 客concede(≥1)
    λ客' = λ客 · 客attack(≤1) · 主concede(≥1)

    即：主队缺员 → 主队进球变少、客队进球变多，方向必须相反。

    另有一个可选的 `weather` 标量：天气对双方**对称**，两个 λ 乘同一个数。
    它来自 `context.py`，这里只读字典键、不导入那个模块，避免循环依赖。
    """
    if not factors:
        return lambda_home, lambda_away
    home = factors.get("home") or {}
    away = factors.get("away") or {}
    new_home = lambda_home * _as_float(home.get("attack"), 1.0)
    new_home *= _as_float(away.get("concede"), 1.0)   # 客队防守缺员 → 主队更容易进球
    new_away = lambda_away * _as_float(away.get("attack"), 1.0)
    new_away *= _as_float(home.get("concede"), 1.0)   # 主队防守缺员 → 客队更容易进球
    weather = _as_float(factors.get("weather"), 1.0)
    if weather != 1.0:
        new_home *= weather
        new_away *= weather
    return min(max(new_home, 0.05), 6.0), min(max(new_away, 0.05), 6.0)
