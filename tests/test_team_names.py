"""队名中文映射：数据源缩写写法与易混队名区分。

背景：API-Football 对西语/葡语联赛下发缩写名（"Argentinos Jrs"、
"Estudiantes L.P."、"Ind. Rivadavia"），而 TEAM_NAMES 收录完整名，
精确匹配会落空、退化成英文原名，再被移动端列宽截断成 "Central Cordoba…"。
"""
from __future__ import annotations

import pytest

from formatkit import team_short_name
from templates import TEAM_NAMES

# 数据源实际下发的缩写/短名 → 期望中文。
# 这些写法来自数据源公开的积分榜与赛事资料，不是推测。
ABBREV_CASES = [
    ("Argentinos Jrs", "阿根廷青年人"),
    ("Argentinos JRS", "阿根廷青年人"),
    ("Estudiantes L.P.", "拉普拉塔大学生"),
    ("Gimnasia M.", "门多萨体操"),
    ("Gimnasia Mendoza", "门多萨体操"),
    ("Ind. Rivadavia", "门多萨独立"),
    ("Independiente Rivadavia", "门多萨独立"),
    ("Newells OB", "纽维尔老男孩"),
    ("Dep. Riestra", "列斯特拉"),
    ("Atl.Tucuman", "图库曼竞技"),
    ("Est. Rio Cuarto", "里奥夸尔托"),
    ("Instituto", "科尔多瓦学院"),
    ("Sarmiento", "萨米恩托"),
    ("Coritiba", "科里蒂巴"),
    ("Remo", "雷莫"),
    ("Chapecoense", "沙佩科恩斯"),
    ("Mito Hollyhock", "水户蜀葵"),
    ("Urawa Reds", "浦和红钻"),
    ("V-Varen Nagasaki", "长崎航海"),
    ("JEF United Chiba", "千叶市原"),
]


@pytest.mark.parametrize("raw,expect", ABBREV_CASES)
def test_data_source_abbrev_maps_to_cn(raw, expect):
    assert team_short_name(raw) == expect


# 这一组**没有**直接收录在 TEAM_NAMES 里，只能靠缩写展开命中。
# 单独列出来的原因：上面 ABBREV_CASES 里每一条都同时被直接收录了，
# 于是"把缩写展开表清空"这种变异，测试照样全绿——测的是收录不是展开。
# 这两条是唯一能证明展开机制真的在工作的用例。
EXPANSION_ONLY_CASES = [
    ("Atl. Madrid", "马德里竞技"),
    ("Dep. Alaves", "阿拉维斯"),
]


@pytest.mark.parametrize("raw,expect", EXPANSION_ONLY_CASES)
def test_abbrev_expansion_itself_works(raw, expect):
    """缩写展开机制本身生效（不依赖直接收录）。"""
    assert raw not in TEAM_NAMES, f"{raw} 已被直接收录，本用例失去意义"
    assert team_short_name(raw) == expect


def test_similar_team_names_stay_distinct():
    """易混队名不能被缩写展开合并成同一支。

    "Independiente"（阿根廷独立）与 "Independiente Rivadavia"（门多萨独立）
    是两支不同的队；"Ind. Rivadavia" 展开后必须与前者区分。
    """
    assert team_short_name("Independiente") == "独立队"
    assert team_short_name("Ind. Rivadavia") == "门多萨独立"
    assert team_short_name("Independiente") != team_short_name("Ind. Rivadavia")

    # 三个 Estudiantes 分属两支不同的队
    assert team_short_name("Estudiantes L.P.") == "拉普拉塔大学生"
    assert team_short_name("Estudiantes Rio Cuarto") == "里奥夸尔托"
    assert team_short_name("Estudiantes L.P.") != team_short_name("Estudiantes Rio Cuarto")


def test_parenthesized_suffix_still_matches():
    """带城市后缀的长名（"Central Cordoba (Santiago del Estero)"）应命中已收录短名。

    这类长名若未命中会被列宽截断成 "Central Cordoba…"，是用户实际看到的英文串。
    """
    assert team_short_name("Central Cordoba (Santiago del Estero)") == "中央科尔多瓦"
    assert team_short_name("Central Cordoba SdE") == "中央科尔多瓦"


def test_every_recorded_name_still_resolves():
    """防回归：TEAM_NAMES 里每条原名都必须仍解析到它自己登记的中文名。

    缩写展开表是全局生效的，新增缩写可能把已有队名改写到别的键上。
    这条测试覆盖全部条目，是防止"补一批、坏一批"的总闸门。
    """
    broken = [
        (raw, expect, team_short_name(raw))
        for raw, expect in TEAM_NAMES.items()
        if team_short_name(raw) != expect
    ]
    assert not broken, f"缩写展开破坏了 {len(broken)} 条已收录队名，前几条：{broken[:5]}"


def test_unknown_name_returns_original():
    """未收录的队绝不臆造：原样返回英文，不返回空串或占位符。"""
    unknown = "Zzz Nonexistent United FC"
    assert team_short_name(unknown) == unknown
    assert team_short_name("") == "?"
    assert team_short_name(None) == "?"


# /teammiss 实测输出的 30 条未翻译队名（2026-10-26 抓取）
TEAMMISS_BATCH = [
    ("Deportivo Pasto", "帕斯托"),
    ("Deportes Tolima", "托利马体育"),
    ("Sport Lisboa e Benfica", "本菲卡"),
    ("Alverca", "阿尔韦卡"),
    ("Estac Troyes", "特鲁瓦"),
    ("Deportivo La Coruna", "拉科鲁尼亚"),
    ("ADO Den Haag", "海牙"),
    ("Cambuur", "坎布尔"),
    ("Academico Viseu", "维塞乌"),
    ("Le Mans", "勒芒"),
    ("Frosinone", "弗罗西诺内"),
    ("Atletico Paranaense", "巴拉纳竞技"),
    ("1899 Hoffenheim", "霍芬海姆"),
    ("Southampton FC", "南安普顿"),
    ("Leicester City FC", "莱斯特城"),
    ("Huddersfield", "哈德斯菲尔德"),
    ("Cardiff", "加的夫"),
    ("Sheffield Utd", "谢菲尔德联"),
    ("Coventry City FC", "考文垂"),
    ("Norwich", "诺维奇"),
    ("Luton", "卢顿"),
    ("Reading", "雷丁"),
    ("QPR", "女王公园巡游者"),
    ("Middlesbrough", "米德尔斯堡"),
    ("Watford", "沃特福德"),
    ("Wigan", "维冈"),
]


@pytest.mark.parametrize("raw,expected", TEAMMISS_BATCH)
def test_teammiss_batch_all_translated(raw, expected):
    """/teammiss 实测到的队名，逐条断言必须翻成中文。

    这批是**生产埋点真实抓到的**未命中字符串，不是推测会出现的名字。
    有任何一条回退成英文，用户就会在实时板块看到英文。
    """
    assert team_short_name(raw) == expected


def test_sheffield_united_not_confused_with_wednesday():
    """谢菲尔德联 ≠ 谢菲尔德星期三。两支不同的队，不能张冠李戴。"""
    from formatkit import _team_key

    assert _team_key("Sheffield Utd") != _team_key("Sheffield Wednesday")
    assert team_short_name("Sheffield Utd") == "谢菲尔德联"


@pytest.mark.parametrize("raw", [
    "Norwich City", "Luton Town", "Reading FC", "Cardiff City",
    "Sheffield United", "Coventry City", "Wigan Athletic",
    "Middlesbrough FC", "Watford FC", "Huddersfield Town",
    "Queens Park Rangers", "Leicester City", "Southampton",
])
def test_full_names_also_translated(raw):
    """完整名与短名都要能翻——数据源两种写法都会下发。"""
    assert team_short_name(raw) != raw


def test_longest_chinese_name_within_mobile_width():
    """最长的中文名也不得超出手机单行可读宽度。"""
    from formatkit import MOBILE_MAX_COLS

    def width(s: str) -> int:
        return sum(2 if ord(ch) > 127 else 1 for ch in s)

    for raw, cn in TEAMMISS_BATCH:
        assert width(cn) <= MOBILE_MAX_COLS, f"{cn} 过长（{width(cn)}）"
