"""统一数据契约 / Unified data contract.

两个数据源（API-Football 主源、football-data.org 备用源）必须产出**同一个结构**，
上层（service / bot_handler / chart）才不需要关心数据来自哪里。

统一后的比赛结构（与 API-Football 原生结构保持一致，改造成零成本）::

    {
        "fixture": {"id": str, "date": str(ISO-8601), "status": {"short": str}, "venue": {...}},
        "league":  {"id": int, "name": str, "season": int, "round": str},
        "teams":   {"home": {"id": str, "name": str}, "away": {...}},
        "goals":   {"home": int|None, "away": int|None},
    }

关键约束：
- 球队 / 比赛 ID 必须是字符串；备用源统一加 ``fd-`` 前缀，避免与主源数字 ID 撞车
- 时间统一为 UTC 的 ISO-8601 字符串（展示层再按 TIMEZONE 换算）
- 状态统一为 API-Football 的 ``status.short`` 枚举（NS / LIVE / HT / FT / PST ...）
"""
from __future__ import annotations

from typing import Any

# 统一后的比赛字段契约：字段名 → (类型元组, 是否必需)
FIXTURE_CONTRACT: dict[str, tuple[tuple, bool]] = {
    "fixture": ((dict,), True),
    "league": ((dict,), True),
    "teams": ((dict,), True),
}

# 子结构契约
FIXTURE_SUB: dict[str, tuple[tuple, bool]] = {
    "id": ((str,), True),
    "date": ((str,), True),
    "status": ((dict,), True),
}
TEAMS_SUB: dict[str, tuple[tuple, bool]] = {
    "home": ((dict,), True),
    "away": ((dict,), True),
}
TEAM_SUB: dict[str, tuple[tuple, bool]] = {
    "id": ((str,), True),
    "name": ((str,), True),
}

# 统一的状态枚举 / Unified status enum（API-Football status.short）
STATUS_NS = "NS"      # 未开始
STATUS_LIVE = "LIVE"  # 进行中
STATUS_HT = "HT"      # 中场
STATUS_FT = "FT"      # 已完场
STATUS_PST = "PST"    # 延期
STATUS_CANC = "CANC"  # 取消
STATUS_SUSP = "SUSP"  # 中断

KNOWN_STATUSES = {STATUS_NS, STATUS_LIVE, STATUS_HT, STATUS_FT,
                  STATUS_PST, STATUS_CANC, STATUS_SUSP}

# 备用源 ID 前缀 / Fallback ID prefix
FALLBACK_PREFIX = "fd-"


class ContractError(ValueError):
    """数据结构不符合统一契约（开发期断言用，不影响线上容错）。"""


def is_fallback_id(value: Any) -> bool:
    """判断该 ID 是否来自备用数据源。"""
    return isinstance(value, str) and value.startswith(FALLBACK_PREFIX)


def _check(node: dict, spec: dict[str, tuple[tuple, bool]], path: str) -> list[str]:
    problems: list[str] = []
    for key, (types, required) in spec.items():
        if key not in node:
            if required:
                problems.append(f"{path}.{key} 缺失")
            continue
        value = node[key]
        if not isinstance(value, types):
            names = "/".join(t.__name__ for t in types)
            problems.append(f"{path}.{key} 类型应为 {names}，实际 {type(value).__name__}")
    return problems


def validate_fixture(fx: Any) -> list[str]:
    """校验一条比赛数据是否符合统一契约，返回问题列表（空列表 = 合规）。"""
    if not isinstance(fx, dict):
        return [f"比赛数据应为 dict，实际 {type(fx).__name__}"]

    problems = _check(fx, FIXTURE_CONTRACT, "fixture_root")

    info = fx.get("fixture")
    if isinstance(info, dict):
        problems += _check(info, FIXTURE_SUB, "fixture")
        status = info.get("status")
        if isinstance(status, dict):
            short = status.get("short")
            if short and short not in KNOWN_STATUSES:
                problems.append(f"fixture.status.short 取值未知：{short!r}")

    teams = fx.get("teams")
    if isinstance(teams, dict):
        problems += _check(teams, TEAMS_SUB, "teams")
        for side in ("home", "away"):
            team = teams.get(side)
            if isinstance(team, dict):
                problems += _check(team, TEAM_SUB, f"teams.{side}")

    return problems


def same_shape(a: Any, b: Any) -> bool:
    """比较两条比赛数据的**键结构**是否一致（不比较具体取值）。

    用于测试：验证主源与备用源产出的数据同构。
    """
    def shape(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: shape(v) for k, v in sorted(node.items())}
        if isinstance(node, list):
            return [shape(v) for v in node] if node else "[]"
        return type(node).__name__

    return shape(a) == shape(b)
