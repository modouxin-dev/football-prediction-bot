"""队名索引完整性 / Team-name index integrity.

覆盖三类「机器能确定对错」的问题，都是真实踩过坑后补的：

1. 归一键撞车（EC Vitória 巴西 vs Vitória SC 葡萄牙）：同一个键对应两支
   不同的队，无论留谁当代表，另一支队的数据源别名都会被显示成错的中文名。
2. 不可分解字母（ø/ı 等）被正则当分隔符，导致同一队的两种写法归一不到一起。
3. 收录键带首尾空格，精确匹配失效、只能靠归一化兜底。

以及未命中埋点：静态收录表永远滞后，未命中的原名必须能被程序自己收集出来。
"""
from __future__ import annotations

import pytest

from formatkit import (
    TEAM_COLLISIONS,
    _TEAM_INDEX,
    _team_key,
    clear_unmatched,
    team_short_name,
    unmatched_team_names,
)
from templates import LEAGUE_NAMES, LEAGUE_SHORT, TEAM_NAMES


# ── 1. 归一键撞车 ────────────────────────────────────────────

def test_collision_key_is_excluded_from_index():
    """撞车的键不进索引——留任何代表都会让另一支队被误显示。"""
    assert TEAM_COLLISIONS, "撞车表为空，说明检测失效或冲突已被误吞"
    for key, names in TEAM_COLLISIONS.items():
        assert key not in _TEAM_INDEX, f"撞车键 {key!r} 不应进索引：{names}"


def test_vitoria_variants_never_mislabeled():
    """巴西 EC Vitória 与葡萄牙 Vitória SC 的别名不得互相串名。"""
    assert "vitoria" in TEAM_COLLISIONS
    # 精确收录仍要能命中（用户看到的是已收录的原名）
    assert team_short_name("EC Vitória") == "维多利亚"
    assert team_short_name("Vitória SC") == "吉马良斯"
    # 未收录的变体：宁可显示英文原名，也不能张冠李戴
    for raw in ("EC Vitoria", "Vitoria SC"):
        assert team_short_name(raw) not in ("维多利亚", "吉马良斯"), (
            f"{raw!r} 走了撞车键，会被显示成另一支队的中文名"
        )


# ── 2. 不可分解字母 ──────────────────────────────────────────

@pytest.mark.parametrize(
    "a,b",
    [
        ("Lillestrøm SK", "Lillestrom"),
        ("Bodø/Glimt", "Bodo Glimt"),
        ("Kasımpaşa SK", "Kasimpasa"),
        ("Strømsgodset IF", "Stromsgodset"),
        ("Tromsø IL", "Tromso IL"),
    ],
)
def test_nordic_letters_normalize_together(a, b):
    """带 ø/ı 的写法与 ASCII 写法必须归到同一个键。

    NFKD 拆不开这些字母，正则会把它们当分隔符（"Lillestrøm"→"lillestr m"），
    结果是同一支队只能靠逐条硬收录，北欧/土耳其联赛全中招。
    """
    assert _team_key(a) == _team_key(b)
    assert " " in _team_key(a) or _team_key(a)  # 非空即可，防空键静默通过
    assert _team_key(a).strip() == _team_key(a)


def test_apostrophe_does_not_split_token():
    """撇号要去掉而不是当分隔符。

    "Newell's Old Boys" 若按分隔符切会得到单字母碎片 "s"，键变成
    "newell s old boys"，与数据源写法 "Newells OB" 对不上——同一支队
    只能靠逐条硬收录，漏一条就退化成英文原名。
    """
    assert _team_key("Newell's Old Boys") == _team_key("Newells OB")
    assert _team_key("Newell's Old Boys") == "newells old boys"


# ── 3. 收录键卫生 ────────────────────────────────────────────

def test_no_whitespace_padded_keys():
    assert [k for k in TEAM_NAMES if k != k.strip()] == []


# ── 4. 未命中埋点 ────────────────────────────────────────────

def test_unmatched_names_are_recorded():
    clear_unmatched()
    assert team_short_name("Arsenal FC") == "阿森纳"
    assert unmatched_team_names() == [], "已收录的队不该被记成未命中"

    unknown = "Zzz Unknown United"
    assert team_short_name(unknown) == unknown
    assert unmatched_team_names() == [unknown]


def test_unmatched_is_idempotent():
    clear_unmatched()
    for _ in range(5):
        team_short_name("Zzz Repeated FC")
    assert unmatched_team_names() == ["Zzz Repeated FC"]


def test_unmatched_ignores_empty():
    clear_unmatched()
    assert team_short_name("") == "?"
    assert unmatched_team_names() == []


# ── 5. 联赛中英双注释的配套完整性 ────────────────────────────

def test_every_league_has_short_name_and_order():
    """新增联赛必须同时补中文名、短名、排序，缺一会显示成裸编号。"""
    from templates import LEAGUE_ORDER

    missing_short = sorted(set(LEAGUE_NAMES) - set(LEAGUE_SHORT))
    missing_order = sorted(set(LEAGUE_NAMES) - set(LEAGUE_ORDER))
    assert missing_short == [], f"缺短名：{missing_short}"
    assert missing_order == [], f"缺排序：{missing_order}"


def test_league_names_are_chinese():
    """值必须是中文（英文放在行尾注释里，不参与渲染）。"""
    import unicodedata

    for lid, name in LEAGUE_NAMES.items():
        assert any("一" <= ch <= "鿿" for ch in name), (
            f"联赛 {lid} 的中文名缺失：{name!r}"
        )
        assert not any(unicodedata.name(ch, "").startswith("LATIN")
                       for ch in name), f"联赛 {lid} 混入了英文：{name!r}"
