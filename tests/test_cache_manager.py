"""AsyncTTLCache 过期回收回归测试。

修复前的实测缺陷(用 max_size=500、ttl=0.05 复现):
    写入 500 条 -> 全部过期 -> 不 set 不 get -> stats()['size'] 仍为 500
    只 get 一条    -> size 499(仅删命中那一条,其余 499 条永久滞留)
    300 条全过期、只 get 50 次 -> 250 条滞留,且 size 把它们算作存活

根因:_cleanup_expired() 只在 set() 里调用,没有后台或读路径清扫;
stats() 直接 len(self._cache),把过期但未命中的条目计入容量。
"""
import asyncio

from cache_manager import AsyncTTLCache


def run(coro):
    return asyncio.run(coro)


async def _fill(cache, n, ttl=0.05):
    for i in range(n):
        await cache.set(("k", i), i, ttl=ttl)


def test_get_reclaims_expired_entries_without_set():
    """静默期(只读不写)也必须回收过期条目。"""
    cache = AsyncTTLCache(max_size=500, default_ttl=0.05, cleanup_interval=0)

    async def go():
        await _fill(cache, 300)
        await asyncio.sleep(0.2)          # 全部过期
        assert len(cache._cache) == 300   # 此刻尚未清扫
        await cache.get(("k", 0))         # 一次 get 即触发清扫
        return len(cache._cache)

    # 修复前为 299(只删掉命中的那一条)
    assert run(go()) == 0


def test_stats_size_excludes_expired():
    """stats() 的 size 必须只统计未过期条目。"""
    cache = AsyncTTLCache(max_size=500, default_ttl=0.05, cleanup_interval=0)

    async def go():
        await _fill(cache, 500)
        await asyncio.sleep(0.2)
        return await cache.stats()

    stats = run(go())
    # 修复前为 500
    assert stats["size"] == 0
    assert stats["max_size"] == 500


def test_sweep_keeps_live_entries():
    """清扫不得误删未过期条目,命中与 LRU 顺序保持可用。"""
    cache = AsyncTTLCache(max_size=100, default_ttl=60, cleanup_interval=0)

    async def go():
        for i in range(10):
            await cache.set(("k", i), i)
        await cache.get(("k", 1))
        return len(cache._cache), await cache.get(("k", 5))

    size, hit = run(go())
    assert size == 10
    assert hit == 5


def test_cache_and_access_order_stay_consistent():
    """清扫后 _cache 与 _access_order 必须一一对应,否则 LRU 会淘汰幽灵键。"""
    cache = AsyncTTLCache(max_size=3, default_ttl=60, cleanup_interval=0)

    async def go():
        for i in range(3):
            await cache.set(("k", i), i)
        for i in range(5):
            await cache.get(("k", i % 3))
        return sorted(cache._cache), sorted(cache._access_order)

    keys, order = run(go())
    assert keys == order
    assert len(cache._cache) == 3


def test_cleanup_interval_defers_sweep():
    """清扫间隔内不重复全表扫描:默认 60 秒是设计值,不应被改成每次读都扫。"""
    cache = AsyncTTLCache(max_size=100, default_ttl=0.05,
                          cleanup_interval=3600)

    async def go():
        await _fill(cache, 50)
        await asyncio.sleep(0.2)
        # 读一个不存在的键:既不命中也不会就地删除任何条目,
        # 因此条目数变化只能来自全表清扫
        await cache.get(("absent",))
        return len(cache._cache)

    # 间隔未到:本次读不触发全表清扫,50 条原样保留
    assert run(go()) == 50
    assert cache._cleanup_interval == 3600
