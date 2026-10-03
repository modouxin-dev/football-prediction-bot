"""生产接线：service._model_for 必须把跨赛季先验传给建模。

为什么单独测这个
----------------
变异测试暴露了一个缺口：把 ``service._model_for`` 里的
``prior_strength=prior`` 删掉，全部 814 条测试**依然全绿**——
也就是说，先验模块和建模函数都对，但接线错了没人会发现。
本文件专门钉住这条接线。
"""

from __future__ import annotations

import asyncio

import pytest

import service as service_mod
from config import load_settings
from service import PredictionService
from season_prior import clear_cache
from api_client import flatten_standings
from tests.sample_data import standings_response

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


class _Api:
    """只提供 _model_for 需要的 get_standings。"""

    def __init__(self):
        self.rows = flatten_standings(standings_response())

    async def get_standings(self, league_id, season):
        return self.rows


@pytest.fixture(autouse=True)
def _clean():
    clear_cache()
    yield
    clear_cache()


def test_model_for_passes_cross_season_prior(monkeypatch):
    """建模调用必须带上 prior_strength 关键字。

    删掉它 → 全部其它测试仍然通过，只有这里会红。
    """
    captured: dict = {}

    def spy(rows, **kwargs):
        captured.update(kwargs)
        return real(rows, **kwargs)

    real = service_mod.build_league_model
    monkeypatch.setattr(service_mod, "build_league_model", spy)

    svc = PredictionService(SETTINGS, _Api())
    run(svc._model_for(39))
    assert "prior_strength" in captured, "建模未传入跨赛季先验"


def test_model_for_survives_prior_failure(monkeypatch):
    """先验模块抛异常时，建模必须照常完成（先验只是增强项）。"""
    def boom(*a, **kw):
        raise RuntimeError("先验炸了")

    monkeypatch.setattr("season_prior.load_prior_strength", boom)
    svc = PredictionService(SETTINGS, _Api())
    model = run(svc._model_for(39))
    assert model is not None and model.teams, "先验失败不应导致建模失败"
