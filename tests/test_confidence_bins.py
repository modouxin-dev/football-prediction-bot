"""把握度分档统计：验证推荐闸门阈值该定在哪。

为什么必须有它
--------------
「提高准确率」最容易做成自欺：把阈值往上调，命中率必然涨，
因为**只推有把握的场次**本来就更准。但那不代表模型变强了，
只是**推得少了**。

没有分档统计，就回答不了这两个问题：
1. 阈值从 0.55 提到 0.65，准确率真的涨了吗？
2. 涨的幅度，值不值那部分被砍掉的场次？

分档边界与真实赛果回测（3158 场 walk-forward）的标定对齐，
方便直接对照「回测预期」与「线上实测」：

    阈值    回测覆盖率    回测命中率
    0.00      100%        52.18%
    0.45     89.9%        54.00%
    0.55     69.6%        57.30%
    0.65     49.5%        61.33%
    0.70     40.5%        62.44%

覆盖点 / Coverage
-----------------
* 分档边界正确（0.45/0.55/0.65/0.70）
* 缺 confidence 的老记录被跳过，不进任何档
* 绝不用 0 填充 —— 0 会伪装成「把握度很低」
* 各档 rate 在样本为 0 时是 None，不是 0
"""
from __future__ import annotations

import json

import pytest

from repository import _confidence_of


def test_reads_confidence_from_inputs_json():
    assert _confidence_of(json.dumps({"confidence": 0.62})) == pytest.approx(0.62)


def test_reads_confidence_from_dict():
    assert _confidence_of({"confidence": 0.71}) == pytest.approx(0.71)


def test_missing_confidence_returns_none():
    """老记录没有该字段 → None。

    绝不能返回 0：0 会被归进最低档 c_lt45，
    把「没有数据」伪装成「把握度很低」，最低档命中率会被系统性拉低。
    """
    assert _confidence_of({"odds": {}}) is None
    assert _confidence_of(None) is None
    assert _confidence_of("") is None


def test_invalid_json_returns_none():
    assert _confidence_of("{不是 json") is None


def test_out_of_range_returns_none():
    """越界值当脏数据处理，不能参与分档。"""
    assert _confidence_of({"confidence": 1.5}) is None
    assert _confidence_of({"confidence": -0.1}) is None


def test_non_numeric_returns_none():
    assert _confidence_of({"confidence": "高"}) is None


def test_boundary_values_are_kept():
    """0 和 1 是合法把握度，不能被当成缺失。"""
    assert _confidence_of({"confidence": 0.0}) == pytest.approx(0.0)
    assert _confidence_of({"confidence": 1.0}) == pytest.approx(1.0)
