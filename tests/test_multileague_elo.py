"""多联赛评分隔离测试：赛果只能改本联赛球队的 Elo 评分。

核心保证（防串味）：
    西甲的赛果不能改变英超球队的评分，否则 Elo 修正 λ 时会用到错误强度。
"""
import asyncio

from config import load_settings
from service import PredictionService

from tests.test_multileague_model import RecordingAPI, fx, run, settings_of


def test_elo_engine_is_per_league():
    """每个联赛一套引擎，competition 必须是该联赛的分区代码。"""
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    e39 = svc._elo_for(39)
    e140 = svc._elo_for(140)

    assert e39 is not e140
    assert e39.competition == "PL"
    assert e140.competition == "PD"


def test_elo_engine_cached_per_league():
    """同一联赛多次取应复用同一个引擎，避免重复读库。"""
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    assert svc._elo_for(39) is svc._elo_for(39)


def test_sync_elo_uses_fixture_own_league():
    """西甲的赛果必须进西甲的评分分区。"""
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    la_liga = fx(2, 140, 10, 20, 5)

    seen = {}
    real_for = svc._elo_for

    def spy(lid):
        eng = real_for(lid)
        seen[int(lid)] = eng
        return eng

    svc._elo_for = spy
    svc._sync_elo(la_liga, 2, 1)

    assert 140 in seen
    assert seen[140].competition == "PD"
    assert 39 not in seen  # 英超引擎不该被西甲赛果触发


def test_sync_elo_falls_back_to_primary_without_league_info():
    """赛程缺联赛信息时回退到主联赛，而不是崩溃。"""
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    seen = {}
    real_for = svc._elo_for

    def spy(lid):
        eng = real_for(lid)
        seen[int(lid)] = eng
        return eng

    svc._elo_for = spy
    bare = {"fixture": {"id": 7}, "league": {},
            "teams": {"home": {"id": 1}, "away": {"id": 2}}}
    svc._sync_elo(bare, 1, 0)

    assert list(seen) == [39]


def test_sync_elo_disabled_is_noop():
    svc = PredictionService(settings_of([39, 140]), RecordingAPI())
    svc.elo_enabled = False
    svc._sync_elo(fx(1, 39, 1, 2, 3), 1, 0)  # 不应抛异常
