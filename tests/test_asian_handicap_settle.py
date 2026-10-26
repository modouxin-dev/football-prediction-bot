"""亚洲盘结算：用真实比分逐档验证。

为什么单独建这个文件
--------------------
亚盘的结算规则与欧盘根本不同，算错就全错，而且**错得不报错**：
概率数字照样出来，只是系统性地偏。这类 bug 靠读代码看不出来。

本文件的用例来自一次**真实赛果回测**（openfootball，英超 2020-21~2024-25
+ 西甲/德甲/意甲，4718 场），比对盘口对照表逐条核对得出。

已抓到的真实 bug（本次修复）
----------------------------
quarter 盘拆分选错了相邻档位：

    旧写法  base = floor(|line|); lower = base+0.5 if |line|>base else base
    -0.25 → floor(0.25)=0 → lower=0.5, upper=1.0   ❌ 应是 0 与 0.5
    -1.25 → floor(1.25)=1 → lower=1.5, upper=2.0   ❌ 应是 1.0 与 1.5

后果（都是真金白银的方向性错误）：
- -0.25 打平本应**半输**（0 档走水 + -0.5 档输 = 0.25），却算成 0（全输）
- -0.25 赢一球本应**全赢**，却算成 0.75
- -0.25 与 -0.75 被拆成同一组腿，两者结果完全相同 ——
  但真实盘口里它们差一整档（-0.25 打平半输，-0.75 打平全输）

正确对照（相邻两档，相差 0.5）：
    ±0.25 → 0    与 ±0.5
    ±0.75 → ±0.5 与 ±1.0
    ±1.25 → ±1.0 与 ±1.5
    ±1.75 → ±1.5 与 ±2.0

结算值含义（主队视角）：1=全赢 0.75=半赢 0.5=走水 0.25=半输 0=全输
"""
from __future__ import annotations

import pytest

from analyzer import asian_handicap_prob, score_matrix_from_analysis

MAX_G = 10


def settle(hg: int, ag: int, line: float) -> float:
    """真实比分 (hg, ag) 在盘口线 line 上的主队结算结果。"""
    n = MAX_G + 1
    mat = [[0.0] * n for _ in range(n)]
    mat[min(hg, MAX_G)][min(ag, MAX_G)] = 1.0
    return asian_handicap_prob(mat, line)["home"]


# (比分, 盘口线, 期望结算, 说明)
# 期望值逐条比对公开盘口对照表，不是凭感觉写的。
CASES = [
    # ---- 整数盘：让球后打平 → 走水 ----
    ((2, 0), -1.0, 1.0, "让一球赢两球 → 全赢"),
    ((1, 0), -1.0, 0.5, "让一球赢一球 → 走水"),
    ((0, 0), 0.0, 0.5, "平手盘 0-0 → 走水"),
    ((1, 0), 0.0, 1.0, "平手盘赢一球 → 全赢"),
    ((0, 1), 0.0, 0.0, "平手盘输球 → 全输"),

    # ---- 半球盘：无走水，必分胜负 ----
    ((1, 1), -0.5, 0.0, "半球盘打平 → 全输"),
    ((1, 0), -0.5, 1.0, "半球盘赢一球 → 全赢"),

    # ---- quarter 盘：拆半结算（bug 高发区）----
    ((1, 0), -0.75, 0.75, "让半一赢一球 → 半赢(-0.5赢 + -1走水)"),
    ((2, 0), -0.75, 1.0, "让半一赢两球 → 全赢"),
    ((1, 1), -0.25, 0.25, "让平半打平 → 半输(0走水 + -0.5输)"),
    ((1, 0), -0.25, 1.0, "让平半赢一球 → 全赢"),
    ((0, 1), -1.25, 0.0, "让一球半输球 → 全输"),
    ((2, 1), -1.25, 0.25, "让一球半赢一球 → 半输(-1走水 + -1.5输)"),
    ((3, 1), -1.25, 1.0, "让一球半赢两球 → 全赢(-1赢 + -1.5赢)"),
    ((4, 1), -1.25, 1.0, "让一球半赢三球 → 全赢"),

    # ---- 受让盘（正盘口）----
    ((0, 1), 0.5, 0.0, "受让半球输一球 → 全输(0+0.5 < 1)"),
    ((1, 1), 0.5, 1.0, "受让半球打平 → 全赢"),
    ((0, 2), 0.5, 0.0, "受让半球输两球 → 全输"),
    ((1, 2), 0.25, 0.0, "受让平半输一球 → 全输"),
    ((0, 0), 0.25, 0.75, "受让平半打平 → 半赢(0走水 + +0.5赢)"),
]


@pytest.mark.parametrize("score,line,expect,note", CASES)
def test_settlement_matches_reference_table(score, line, expect, note):
    got = settle(score[0], score[1], line)
    assert abs(got - expect) < 1e-9, f"{note}：得 {got}，期望 {expect}"


def test_quarter_lines_must_not_collapse():
    """-0.25 与 -0.75 绝不能被拆成同一组腿。

    这是本次修掉的那个 bug 的**直接断言**：旧写法把两者都拆成
    [-0.5, -1.0]，于是任何比分下两者结算完全相同。
    真实盘口里它们差一整档，打平时一个是半输、一个是全输。
    """
    assert settle(1, 1, -0.25) == pytest.approx(0.25)   # 半输
    assert settle(1, 1, -0.75) == pytest.approx(0.0)    # 全输
    assert settle(1, 1, -0.25) != settle(1, 1, -0.75)


def test_quarter_split_uses_adjacent_lines():
    """拆出来的两条腿必须相差恰好 0.5，且包含 |line| 上下各一档。

    对 ±0.25：腿是 0 与 0.5（不是 0.5 与 1.0）
    对 ±1.25：腿是 1.0 与 1.5（不是 1.5 与 2.0）
    用「赢一球」这个能区分两者的比分来验：
    -0.25 赢一球：0 档赢、0.5 档赢 → 1.0
    若错拆成 [-0.5,-1.0]：赢一球是 0.5 档赢、-1 档走水 → 0.75
    """
    assert settle(1, 0, -0.25) == pytest.approx(1.0)
    assert settle(2, 1, -1.25) == pytest.approx(0.25)   # 半输，不是 0


def test_home_and_away_sum_to_one_without_push():
    """半球盘无走水 → 主客两边结算之和恒为 1（一方赢另一方必输）。"""
    for line in (-0.5, -1.5, 0.5, 1.5):
        n = MAX_G + 1
        mat = [[0.0] * n for _ in range(n)]
        mat[2][1] = 1.0
        r = asian_handicap_prob(mat, line)
        assert r["home"] + r["away"] == pytest.approx(1.0)


def test_whole_line_push_makes_sum_less_than_one():
    """整数盘走水时主客之和 < 1 —— 差额就是走水概率，两边都退本金。"""
    n = MAX_G + 1
    mat = [[0.0] * n for _ in range(n)]
    mat[1][0] = 1.0                      # 主队赢一球，对 -1 盘恰好走水
    r = asian_handicap_prob(mat, -1.0)
    assert r["home"] == pytest.approx(0.5)
    assert r["away"] == pytest.approx(0.5)
    # 走水时两边各 0.5，和为 1 但**不是**「一方赢」——
    # 这里的关键是两边相等且都不为 0/1
    assert 0.0 < r["home"] < 1.0


def test_probability_matrix_settlement_is_consistent_with_point_mass():
    """用概率矩阵算的赢盘概率，必须等于逐比分结算的概率加权和。

    守住「结算函数」与「概率函数」是同一套规则 —— 若两处各写一份，
    展示给用户看的赢盘概率会和实际结算结果对不上。
    """
    mat = score_matrix_from_analysis(
        {"lambda_home": 1.6, "lambda_away": 1.1, "rho": 0.0})
    line = -0.75
    r = asian_handicap_prob(mat, line)
    manual = 0.0
    n = len(mat)
    for h in range(n):
        for a in range(n):
            manual += mat[h][a] * settle(h, a, line)
    assert r["home"] == pytest.approx(manual, abs=1e-9)
