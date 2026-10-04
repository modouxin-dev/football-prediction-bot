"""新增联赛接入后的队名短名回退 / Short-name fallback for newly added leagues.

背景：把联赛从 17 个扩到 55 个后，/teammiss 埋点吐出一批未翻译队名。其中
「Tottenham」「Newcastle」「Leeds」「Brighton」「Central Cordoba de Santiago」
这类是**数据源下发短名、TEAM_NAMES 收录全名**，归一化后键不同，精确匹配必漏。

本测试钉死两点：
1. 前缀回退能覆盖它们（且这 5 条**只依赖前缀机制**——TEAM_NAMES 里没有对应的
   裸名条目，机制一旦失效它们立刻变红，不会像「机制+收录双保险」那样互相兜底
   导致死代码测不出来）。
2. 歧义前缀绝不猜测（"Manchester" 同时是 United 与 City 的前缀）。

/list 中的队名字符串均来自线上 /teammiss 埋点导出的真实原文，不是构造的。
"""
from __future__ import annotations

import pytest

from formatkit import (
    TEAM_NAMES,
    TEAM_COLLISIONS,
    _TEAM_INDEX,
    _TEAM_PREFIX,
    _team_key,
    team_name,
    team_short_name,
)


# /teammiss 埋点导出的真实未翻译原名（2026-10-05，联赛扩容后首次采集）
TEAMMISS_NAMES = [
    "Tenerife",
    "Cordoba",
    "RCD Espanyol de Barcelona",
    "Málaga CF",
    "Tottenham",
    "Coventry",
    "Ipswich",
    "Newcastle",
    "Hull City",
    "Leeds",
    "Brighton",
    "Central Cordoba de Santiago",
]

# 仅靠前缀机制命中（TEAM_NAMES 中不存在这些裸名条目）。
# 注：Central Cordoba de Santiago 虽也走前缀，但它在远端已被硬收录，
# 不满足「只依赖机制」的前提，故不列入——列进来会让断言被收录表兜住。
PREFIX_ONLY = [
    ("Tottenham", "托特纳姆热刺"),
    ("Newcastle", "纽卡斯尔联"),
    ("Leeds", "利兹联"),
    ("Brighton", "布莱顿"),
]


@pytest.mark.parametrize("raw", TEAMMISS_NAMES)
def test_teammiss_names_all_translated(raw):
    """埋点名单里的每一个原名都必须翻出中文，不能再退回英文。"""
    out = team_short_name(raw)
    assert out != raw, f"{raw} 仍未翻译（退回英文原名）"
    assert not any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in out), (
        f"{raw} 翻出来的仍是英文：{out}"
    )


@pytest.mark.parametrize("raw,expected", PREFIX_ONLY)
def test_prefix_fallback_only_mechanism(raw, expected):
    """这 5 条只依赖前缀机制，不依赖硬收录。

    机制若失效（例如 _TEAM_PREFIX 值存成归一键、或根本没接进匹配链路），
    这里立刻变红——防止「收录表刚好也补了裸名」把死代码掩盖过去。
    """
    assert raw not in TEAM_NAMES, f"{raw} 被硬收录了，本用例就测不到前缀机制了"
    assert team_short_name(raw) == expected


@pytest.mark.parametrize("raw,expected", PREFIX_ONLY)
def test_prefix_fallback_in_bilingual_mode(raw, expected):
    """双语模式同样走前缀回退（team_name 与 team_short_name 两条链路都要接）。"""
    out = team_name(raw)
    assert out.startswith(expected), f"双语模式未走前缀回退：{out}"
    assert f"({raw})" in out


def test_prefix_index_values_are_original_names():
    """前缀索引的值必须是**原名**而非归一键。

    调用方用它查 TEAM_NAMES（键是数据源原名）。若存成归一键，查不到会静默
    返回 None，前缀索引形同虚设——而测试若只断言「命中」不看值就会漏掉。
    """
    assert _TEAM_PREFIX, "前缀索引不应为空"
    for prefix, value in _TEAM_PREFIX.items():
        assert value in TEAM_NAMES, f"前缀 {prefix!r} 的值 {value!r} 不是 TEAM_NAMES 的键"


@pytest.mark.parametrize("ambiguous", ["Manchester", "Real", "United"])
def test_ambiguous_prefix_never_guessed(ambiguous):
    """歧义前缀一律不猜：宁可显示英文原名，也不张冠李戴。

    "Manchester" 同时是 United 与 City 的前缀，猜中任何一边都是错的。
    （Alaves / Suwon 不在本列：它们本身是精确收录/归一键命中，不是猜测。）
    """
    assert ambiguous not in _TEAM_PREFIX, (
        f"{ambiguous!r} 是歧义前缀，不该进前缀索引（会张冠李戴）"
    )
    assert team_short_name(ambiguous) == ambiguous


def test_collision_key_never_guessed():
    """已知冲突键不许通过**前缀**猜出中文（巴西维多利亚 vs 葡萄牙吉马良斯）。

    "vitoria" 虽因冲突没进 idx，但它同时是其他更长队名的前缀；若不显式排除，
    会被前缀索引收进去并张冠李戴——这是测试跑出来的真实漏洞。

    注：裸名 "Vitoria" 本身在 TEAM_NAMES 里有精确收录（既有行为，绑定巴西队），
    走的是精确匹配而非前缀，不在本用例约束范围内。
    """
    assert "vitoria" in TEAM_COLLISIONS
    # 索引层断言：任何已知冲突键都不得作为前缀被采纳（通用，不依赖挑样例）
    for collision_key in TEAM_COLLISIONS:
        assert collision_key not in _TEAM_PREFIX, (
            f"冲突键 {collision_key!r} 进了前缀索引，会张冠李戴"
        )


def test_full_key_never_used_as_prefix():
    """本身已是某队完整键的短名，不得再当作别队的前缀。

    "suwon" 是水原FC 的归一键，同时又是「水原三星」的前缀。只按「贡献者唯一」
    判断会把它当成无歧义前缀，让水原FC 的短名被别的队占用。
    """
    assert "suwon" in _TEAM_INDEX, "前提变了：suwon 不再是完整键，本用例需换样例"
    assert "suwon" not in _TEAM_PREFIX


def test_prefix_does_not_cross_token_boundary():
    """前缀按 token 切，不允许半个单词命中（"tottenham ho" 不该命中热刺）。"""
    assert _team_key("Tottenham Ho") not in _TEAM_PREFIX
    assert team_short_name("Tottenham Ho") == "Tottenham Ho"


def test_prefix_fallback_no_regression_on_known_names():
    """已收录的全名不能被前缀机制改写（抽验主流队）。"""
    known = {
        "Manchester City FC": "曼城",
        "Manchester United FC": "曼联",
        "Liverpool FC": "利物浦",
        "Arsenal FC": "阿森纳",
        "Tottenham Hotspur FC": "托特纳姆热刺",
        "Newcastle United FC": "纽卡斯尔联",
        "Leeds United FC": "利兹联",
        "Brighton & Hove Albion FC": "布莱顿",
        "Real Madrid": "皇家马德里",
        "FC Barcelona": "巴塞罗那",
        "Bayern Munich": "拜仁慕尼黑",
        "Estudiantes L.P.": "拉普拉塔大学生",
        "Newells OB": "纽维尔老男孩",
        "CA Central Córdoba de Santiago del Estero": "中央科尔多瓦",
    }
    for raw, expected in known.items():
        assert team_short_name(raw) == expected, f"{raw} 回归失败"


def test_collisions_still_excluded():
    """冲突键依旧不进索引（前缀索引建立在 idx 之上，不能绕过这条约束）。"""
    for key in _TEAM_PREFIX:
        assert key not in TEAM_COLLISIONS, f"冲突键 {key!r} 混进了前缀索引"
        assert key not in _TEAM_INDEX, "前缀索引不应包含完整键"
