"""赛前上下文信号：球员评估与天气 / Player quality & weather as λ multipliers.

它补上 `prematch.py` 做不到的两件事 / What this adds over `prematch.py`
-------------------------------------------------------------------------
`prematch.py` 只能**数人头**：几个伤停、首发里几个后卫几个前锋。这在两类问题
上明显不够：

    `prematch.py` can only **count heads**: how many absentees, how many
    defenders vs forwards. That falls short in two ways:

1. 同样是「缺 2 人」，缺的是核心前锋还是第四中卫，影响天差地别。
   "Two absentees" means very different things depending on whether they are
   the star striker or the fourth centre-back.
2. 首发 11 人的**质量**完全没被利用 —— 一支全主力球队和一支轮换球队
   在「人数」上都是 11。

   The **quality** of the starting eleven is unused — a full-strength side and
   a rotated one both have 11 players.

本模块用 API-Football 的赛季球员统计解决这两点：

    This module fixes both using API-Football season player statistics:

* `/players/squads?team=X` —— 全队名单**含位置**，把伤停 player.id 映射回
  位置，于是「前锋缺阵更致命」可以做加权（此前伤停端点只有缺阵类型、没有位置）。

  `/players/squads?team=X` — full squad **with positions**, so an absentee's
  id maps back to a position and "a missing striker hurts more" becomes
  weighting (the injuries endpoint carries only absence type, no position).

* `/players?team=X&season=Y` —— 赛季累计统计，含 `games.rating`（截至上一场的
  平均评分）。首发 11 人按出场时间加权平均，就是这支球队**今晚真正派出的实力**。

  `/players?team=X&season=Y` — season stats including `games.rating` (average
  up to the previous match). A minutes-weighted average over the starting XI
  is the strength this team **actually fields tonight**.

主力缺阵因此**自动**被量化，不再靠数人数：缺阵者不在首发里，顶上来的替补
rating 通常更低，加权均值自然下降。这比「缺 1 人扣多少」精确得多。

    Star absences are thus quantified **automatically**: the absentee is not in
    the XI, his replacement's rating is usually lower, and the weighted mean
    drops on its own — far more precise than "minus X per absentee".

为什么评分是赛前可用的 / Why ratings are pre-match-safe
------------------------------------------------------
`games.rating` 是该球员**本赛季截至当前**的累计平均，只含已结束的比赛。
赛前调用不会引入任何未来信息。（相对的，`/fixtures/players` 的单场 rating 是
赛后数据，**绝不能**用于赛前预测。）

    `games.rating` is a season-to-date cumulative average over finished matches
    only — no look-ahead. (By contrast `/fixtures/players` per-match ratings are
    post-match data and must **never** be used pre-match.)

诚实边界 / Honest limits
------------------------
* **权重仍未校准。** 与 `prematch.py` 一样：历史语料里没有评分列，拟合不了。
  数值是保守量级，靠 `RATING_STRENGTH` / `WEATHER_STRENGTH` 调节，等线上
  积累 A/B 样本后定标（见 README「赛前信号」）。

  Weights remain **uncalibrated**, same as `prematch.py`: no historical corpus
  carries ratings. Values are conservative, tuned via `RATING_STRENGTH` /
  `WEATHER_STRENGTH`, pending live A/B samples.

* 天气**默认关闭**（`WEATHER_STRENGTH=0`）：公开研究结论互相矛盾，且本项目
  尚未验证。管道已通（见 `weather.py`），但**启用前必须在自己的库上验证** ——
  这是四类信号里唯一能真正回测的一项（Open-Meteo 有 1940 年至今的归档，
  而伤停/阵容/评分都不存在于任何历史语料）。

  Weather is **off by default** (`WEATHER_STRENGTH=0`): published results
  disagree and we have not validated it. The pipe works (see `weather.py`), but
  **validate on your own DB before enabling** — it is the only one of the four
  signals that can genuinely be back-tested (Open-Meteo archives reach back to
  1940, whereas injuries, lineups and ratings exist in no historical corpus).

* 无数据 / 未启用时，所有乘数**精确为 1.0**，模型行为与接入前逐位一致。

  With no data or disabled, every multiplier is **exactly 1.0** — bit-for-bit
  identical to before this module existed.
"""

from __future__ import annotations

import math
from typing import Any

# ---- 位置权重（伤停加权用） -----------------------------------------------
# 数值含义：该位置缺 1 人 ≈ 相当于多少个「标准缺阵」。
# 前锋最高：进球是最稀缺、最难替代的能力。门将偏低：替补门将水平差距相对小，
# 且门将表现方差大、对期望进球的影响弱于前锋。
#   Meaning: one absentee here ≈ how many "standard absences".
#   Attacker highest — goals are the scarcest, least replaceable skill.
#   Keeper lower — backup keepers are closer in level, and keeper variance
#   affects expected goals less than a missing striker does.
POSITION_WEIGHTS = {
    "G": 0.6, "GK": 0.6, "GOALKEEPER": 0.6, "门将": 0.6,
    "D": 0.8, "DEFENDER": 0.8, "后卫": 0.8,
    "M": 1.0, "MIDFIELDER": 1.0, "中场": 1.0,
    "F": 1.2, "A": 1.2, "ATTACKER": 1.2, "FORWARD": 1.2, "前锋": 1.2,
}
DEFAULT_POSITION_WEIGHT = 1.0

# ---- 评分 → 实力 ----------------------------------------------------------
# 联赛首发球员评分区间大致 6.3–7.5，基准取 6.8。
#   A league starter's rating spans roughly 6.3–7.5; baseline 6.8.
BASELINE_RATING = 6.8
# 归一化尺度：偏离基准多少算「满格」。1.0 表示 ±1.0 分即饱和。
#   Normalisation scale: deviation at which the effect saturates.
RATING_SPREAD = 1.0
# 单队上限：再豪华的首发也不许让进攻力凭空涨超过 ±25%。
#   Per-side cap: even a star XI may not inflate attack by more than ±25%.
MAX_RATING_EFFECT = 0.25
# 防守端继承进攻端效应的比例。防守更依赖体系而非个人，故打折。
#   Share of the attack effect carried to defence. Defending is more systemic.
DEFENCE_SHARE = 0.6

# ---- 天气 → 总进球 --------------------------------------------------------
# 舒适区间：10–20°C。偏离越远，体能与传球质量越受影响。
#   Comfort band 10–20 °C; deviations degrade stamina and passing.
TEMP_COMFORT_LOW, TEMP_COMFORT_HIGH = 10.0, 20.0
TEMP_FALLOFF = 12.0          # 每偏离这么多度算「满格一级」
PRECIP_FULL = 4.0            # 毫米/小时，达到即「大雨满格」
WIND_FULL = 40.0             # km/h，达到即「大风满格」
# 三项权重（和为 1）：降水与风对进球的抑制比温度更直接。
WEATHER_WEIGHTS = {"temp": 0.25, "precip": 0.45, "wind": 0.30}
MAX_WEATHER_EFFECT = 0.20    # 极端天气最多压低总进球 20%

# 可配置默认值 / Tunable defaults（默认都不改结果）
DEFAULT_RATING_STRENGTH = 0.5   # 半强度，与 prematch 保持一致
DEFAULT_WEATHER_STRENGTH = 0.0  # 天气默认关闭
STRENGTH_MIN, STRENGTH_MAX = 0.0, 1.0

# 出场时间下限：低于此分钟的赛季样本不足以代表真实水平，权重按 minutes 折算，
# 但至少给 0.15，避免"新援首秀"被当成 0 分拖垮均值。
MIN_MINUTES = 90.0
MIN_MINUTE_WEIGHT = 0.15


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


# ---- 位置解析 --------------------------------------------------------------

def position_weight(position: Any) -> float:
    """位置（'G'/'Defender'/…）→ 权重。无法识别给 1.0（中性）。"""
    key = str(position or "").strip().upper()
    if not key:
        return DEFAULT_POSITION_WEIGHT
    return POSITION_WEIGHTS.get(key, DEFAULT_POSITION_WEIGHT)


def squad_positions(squad: list[dict[str, Any]] | None) -> dict[str, str]:
    """球队名单 → {player_id: position}。

    兼容两种形态：`/players/squads` 的 [{player:{id,pos}}] 与扁平的
    [{id, position}]。解析不了的条目直接跳过。
    """
    out: dict[str, str] = {}
    for entry in squad or []:
        if not isinstance(entry, dict):
            continue
        player = entry.get("player")
        pid = entry.get("id") if isinstance(entry.get("id"), (int, str)) else None
        pos = entry.get("position") or entry.get("pos")
        if isinstance(player, dict):
            pid = player.get("id", pid)
            pos = pos or player.get("pos") or player.get("position")
        if pid is None or not pos:
            continue
        out[str(pid)] = str(pos)
    return out


def weighted_absence_load(
    injuries: list[dict[str, Any]] | None,
    team_id: Any,
    positions: dict[str, str] | None = None,
) -> float:
    """按位置加权后的有效缺阵人数。无位置信息时退化为按人数计（权重 1.0）。

    这样即使 squad 拉取失败，也不会比 `prematch.absence_load` 更差 ——
    只是少了位置区分，不会直接失效。
    """
    if not injuries:
        return 0.0
    target = str(team_id)
    total = 0.0
    for item in injuries or []:
        if not isinstance(item, dict):
            continue
        if str(((item.get("team") or {}).get("id"))) != target:
            continue
        player = item.get("player")
        if not isinstance(player, dict) or not player:
            continue
        # 缺阵确定性权重沿用 prematch 的口径（存疑只算 0.4）
        reason = str(player.get("type") or player.get("reason") or "")
        certainty = _injury_certainty(reason)
        pid = player.get("id")
        # 没有 id 就无法映射位置，但「有人缺阵」这个事实本身仍然成立，
        # 因此按中性权重计入 —— 跳过它会系统性**低估**缺阵。
        #   Without an id we cannot map a position, but the absence is still
        #   real, so count it neutrally — skipping would systematically
        #   *under*-count absentees.
        pos = (positions or {}).get(str(pid)) if pid is not None else None
        total += certainty * position_weight(pos)
    return total


def _injury_certainty(reason: str) -> float:
    """缺阵状态 → 确定性权重。与 prematch 保持同一口径。"""
    from prematch import _absence_weight  # 局部导入避免循环依赖

    return _absence_weight(reason)


# ---- 球员评估 --------------------------------------------------------------

def player_ratings(stats: list[dict[str, Any]] | None) -> dict[str, tuple[float, float]]:
    """`/players` 响应 → {player_id: (rating, minutes)}。

    只取**该赛季**第一条统计（同一球员若有多条，取出场时间最长的那条，
    通常是他实际效力的联赛）。
    """
    out: dict[str, tuple[float, float]] = {}
    for entry in stats or []:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("player")
        pid = str(pid.get("id")) if isinstance(pid, dict) else (str(pid) if pid is not None else "")
        if not pid:
            continue
        best: tuple[float, float] | None = None
        for block in entry.get("statistics") or []:
            if not isinstance(block, dict):
                continue
            games = block.get("games") or {}
            rating = _as_float(games.get("rating"))
            if rating is None or rating <= 0:
                continue
            minutes = _as_float(games.get("minutes")) or 0.0
            if best is None or minutes > best[1]:
                best = (rating, minutes)
        if best is not None:
            prev = out.get(pid)
            if prev is None or best[1] > prev[1]:
                out[pid] = best
    return out


def _minute_weight(minutes: float) -> float:
    """出场时间 → 置信权重。样本太少时打折，但保底 0.15。"""
    if minutes <= 0:
        return MIN_MINUTE_WEIGHT
    return max(minutes / MIN_MINUTES, MIN_MINUTE_WEIGHT)


def xi_rating(
    start_xi: list[dict[str, Any]] | None,
    ratings: dict[str, tuple[float, float]] | None,
) -> float | None:
    """首发 11 人的出场时间加权平均评分。拿不到评分返回 None（不是 0）。"""
    if not start_xi or not ratings:
        return None
    total_w = 0.0
    total_r = 0.0
    for entry in start_xi:
        if not isinstance(entry, dict):
            continue
        player = entry.get("player") or {}
        pid = str(player.get("id")) if isinstance(player, dict) else None
        if not pid:
            continue
        hit = ratings.get(pid)
        if hit is None:
            continue
        rating, minutes = hit
        weight = _minute_weight(minutes)
        total_r += rating * weight
        total_w += weight
    if total_w <= 0:
        return None
    return total_r / total_w


def rating_effect(rating: float | None, strength: float = DEFAULT_RATING_STRENGTH) -> float:
    """首发评分 → 实力效应 ∈ [-MAX, MAX]。无评分或强度为 0 时精确返回 0.0。"""
    if rating is None or strength <= 0:
        return 0.0
    delta = _as_float(rating)
    if delta is None:
        return 0.0
    # tanh 压缩：极端评分也不会线性放大
    norm = math.tanh((delta - BASELINE_RATING) / RATING_SPREAD)
    return _clamp(norm * MAX_RATING_EFFECT * strength, -MAX_RATING_EFFECT, MAX_RATING_EFFECT)


# ---- 天气 ------------------------------------------------------------------

def weather_effect(sample: Any | None, strength: float = DEFAULT_WEATHER_STRENGTH) -> float:
    """天气快照 → 对**双方总进球**的抑制系数（≥0，越大越压低）。

    返回的是「抑制量」而非乘数：0.0 表示不影响。作用于双方 λ 同乘。
    """
    if sample is None or strength <= 0:
        return 0.0
    temp = _as_float(getattr(sample, "temp_c", None))
    precip = _as_float(getattr(sample, "precip_mm", None))
    wind = _as_float(getattr(sample, "wind_kmh", None))

    parts = 0.0
    weight_sum = 0.0

    if temp is not None:
        if temp < TEMP_COMFORT_LOW:
            dev = (TEMP_COMFORT_LOW - temp) / TEMP_FALLOFF
        elif temp > TEMP_COMFORT_HIGH:
            dev = (temp - TEMP_COMFORT_HIGH) / TEMP_FALLOFF
        else:
            dev = 0.0
        parts += _clamp(dev, 0.0, 1.0) * WEATHER_WEIGHTS["temp"]
        weight_sum += WEATHER_WEIGHTS["temp"]

    if precip is not None:
        parts += _clamp(precip / PRECIP_FULL, 0.0, 1.0) * WEATHER_WEIGHTS["precip"]
        weight_sum += WEATHER_WEIGHTS["precip"]

    if wind is not None:
        parts += _clamp(wind / WIND_FULL, 0.0, 1.0) * WEATHER_WEIGHTS["wind"]
        weight_sum += WEATHER_WEIGHTS["wind"]

    if weight_sum <= 0:
        return 0.0
    # 按**实际拿到的**项重新归一化，缺一项不会系统性低估
    return _clamp(parts / weight_sum, 0.0, 1.0) * MAX_WEATHER_EFFECT * strength


# ---- 合成 ------------------------------------------------------------------

def context_factors(
    home_id: Any,
    away_id: Any,
    *,
    start_xi: dict[str, list[dict[str, Any]]] | None = None,
    ratings: dict[str, dict[str, tuple[float, float]]] | None = None,
    weather_sample: Any | None = None,
    rating_strength: float = DEFAULT_RATING_STRENGTH,
    weather_strength: float = DEFAULT_WEATHER_STRENGTH,
) -> dict[str, Any]:
    """合成球员评估与天气的 λ 修正因子。

    返回结构与 `prematch.squad_factors` 一致，便于相乘合并；
    另附 `weather` 单一乘数（天气对双方对称，不分队）。
    """
    sides: dict[str, dict[str, Any]] = {}
    detail: dict[str, Any] = {}
    for key, tid in (("home", home_id), ("away", away_id)):
        key_id = str(tid)
        xi = (start_xi or {}).get(key_id) or []
        rating = xi_rating(xi, (ratings or {}).get(key_id))
        effect = rating_effect(rating, rating_strength)
        # 评分高 → 进球更多、失球更少；concede 是失球系数，故取减
        attack = 1.0 + effect
        concede = max(1.0 - effect * DEFENCE_SHARE, 0.5)
        if effect == 0.0:
            attack, concede = 1.0, 1.0
        sides[key] = {"attack": attack, "concede": concede}
        detail[key] = {
            "xi_rating": round(rating, 3) if rating is not None else None,
            "xi_size": len(xi),
            "rating_effect": round(effect, 4),
        }

    w_effect = weather_effect(weather_sample, weather_strength)
    weather_mult = 1.0 - w_effect if w_effect else 1.0
    detail["weather"] = {
        "effect": round(w_effect, 4),
        "multiplier": round(weather_mult, 4),
        "sample": getattr(weather_sample, "as_dict", lambda: None)(),
    }
    return {"home": sides["home"], "away": sides["away"],
            "weather": weather_mult, "detail": detail}


def merge_factors(*factors: dict[str, Any] | None) -> dict[str, Any] | None:
    """把多个因子集合并成一个（同侧同名相乘，weather 连乘）。

    任一项为 None 会跳过；全部为空则返回 None（表示「不修正」）。
    """
    merged: dict[str, Any] | None = None
    for item in factors:
        if not item:
            continue
        if merged is None:
            merged = {"home": dict(item.get("home") or {}),
                      "away": dict(item.get("away") or {}),
                      "weather": item.get("weather", 1.0),
                      "detail": {}}
        else:
            for side in ("home", "away"):
                src = item.get(side) or {}
                dst = merged[side]
                for field in ("attack", "concede"):
                    if field in src:
                        dst[field] = dst.get(field, 1.0) * src[field]
            merged["weather"] = merged.get("weather", 1.0) * item.get("weather", 1.0)
        detail = item.get("detail") or {}
        for key, value in detail.items():
            merged["detail"].setdefault(key, {}).update(
                value if isinstance(value, dict) else {"value": value}
            )
    return merged


def apply_weather(l_home: float, l_away: float, factor: float | None) -> tuple[float, float]:
    """天气对双方对称：两个 λ 乘同一个数。"""
    if not factor or abs(factor - 1.0) < 1e-12:
        return l_home, l_away
    return min(max(l_home * factor, 0.05), 6.0), min(max(l_away * factor, 0.05), 6.0)
