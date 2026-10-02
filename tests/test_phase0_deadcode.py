"""阶段 0：死代码清理回归锁。

背景
----
``PredictionService.elo_factor_for`` 历史上是全仓零调用点的死代码，
且与回测实际使用的 ``elo.elo_multiplier`` 是两套独立实现，容易误导后续开发。
阶段 0 将其删除，本文件锁死"已删除"与"未误删"两端。

注意
----
本文件只做**静态结构断言**，不依赖任何外部数据或 CSV，
因此可在无网络、无历史数据的环境下运行。
"""

import importlib

import pytest


def test_elo_factor_for_is_removed():
    """死代码已从 PredictionService 上移除。"""
    service = importlib.import_module("service")
    assert not hasattr(service.PredictionService, "elo_factor_for"), (
        "elo_factor_for 应已在阶段 0 删除；若确有调用方，请先补测试再恢复"
    )


def test_elo_multiplier_survives():
    """真正被引用的 Elo 实现仍在，未被误删。"""
    elo = importlib.import_module("elo")
    assert callable(getattr(elo, "elo_multiplier", None))


@pytest.mark.parametrize(
    "name",
    ["stats", "refresh", "get_h2h", "_sync_elo", "_remember"],
)
def test_core_methods_not_accidentally_removed(name):
    """删除死代码时不得误伤邻近方法。"""
    service = importlib.import_module("service")
    assert hasattr(service.PredictionService, name), f"{name} 不应被删除"


def test_service_module_imports_cleanly():
    """service 模块可正常导入（防止删除时破坏语法/引用）。"""
    service = importlib.import_module("service")
    assert service.PredictionService is not None
