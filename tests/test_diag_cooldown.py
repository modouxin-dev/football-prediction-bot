"""主源冷却的全局视图：诊断命令依赖它判断"主源到底有没有被调用"。

冷却是按联赛隔离存在字典里的，_primary_cooling(league_id) 只能回答单个
联赛。诊断命令曾用 _primary_cooling() 无参调用（恒返回 False），于是永远
显示「未冷却」，而主源其实正被冷却挡着 —— 排查方向被误导。本文件锁定
primary_cooldown_remaining 的语义，防止该属性被改回按单个联赛判定。
"""
import time

from data_source import DataSourceRouter


class _Stub:
    async def get_fixtures(self, *a, **k):
        return [{"fixture": {"id": 1}}]


def _router():
    return DataSourceRouter(_Stub(), None, mode="api-football")


def test_no_cooldown_is_zero():
    """未发生任何失败时，剩余冷却为 0（不是 None、不是负数）。"""
    r = _router()
    assert r.primary_cooldown_remaining == 0


def test_single_league_cooldown_positive():
    """单个联赛冷却：无参 _primary_cooling() 看不见，属性必须能看见。"""
    r = _router()
    r._primary_cooldown_by_league[39] = time.monotonic() + 120
    assert r._primary_cooling() is False          # 无参版本恒 False（历史行为）
    assert 100 < r.primary_cooldown_remaining <= 120


def test_takes_max_across_leagues():
    """多联赛同时冷却时取最大值（诊断要显示"还要等多久"）。"""
    r = _router()
    now = time.monotonic()
    r._primary_cooldown_by_league[39] = now + 50
    r._primary_cooldown_by_league[2] = now + 300
    assert 290 < r.primary_cooldown_remaining <= 300


def test_expired_cooldown_is_zero():
    """冷却已过期时归零，不能返回负数（否则诊断会显示"剩余 -1 秒"）。"""
    r = _router()
    r._primary_cooldown_by_league[39] = time.monotonic() - 10
    assert r.primary_cooldown_remaining == 0
