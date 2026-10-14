"""排行榜标题的联赛名解析。

为什么单测：main.py 曾硬编码 f"联赛 {league_id}"，用户看到的是「联赛 39 ·
赛季 2026」—— 光看数字不知道是哪个联赛。templates.LEAGUE_NAMES 里早就登记
了 39 = 英格兰超级联赛，但排行榜这条路径没走映射。
"""

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_main():
    """只取标题构造函数，不启动机器人。"""
    spec = importlib.util.spec_from_file_location(
        "bot_main_under_test",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def main_mod():
    return _load_main()


def test_registered_league_uses_real_name(main_mod):
    """39 必须显示真实联赛名，不能是「联赛 39」。"""
    title = main_mod.standings_title(39, 2026)
    assert "英格兰超级联赛" in title
    assert "联赛 39" not in title


def test_title_keeps_season(main_mod):
    assert "2026" in main_mod.standings_title(39, 2026)


def test_unregistered_league_falls_back(main_mod):
    """未登记的联赛不能凭空造名字，回退「联赛 N」。"""
    assert main_mod.standings_title(999999, 2026) == "联赛 999999 · 赛季 2026"


@pytest.mark.parametrize("lid,name", [
    (39, "英格兰超级联赛"),
    (140, "西班牙甲级联赛"),
    (78, "德国甲级联赛"),
    (135, "意大利甲级联赛"),
    (61, "法国甲级联赛"),
])
def test_common_leagues_resolve(main_mod, lid, name):
    assert name in main_mod.standings_title(lid, 2026)
