"""AsyncTTLCache 过期回收回归测试。

修复前的实测缺陷(用 max_size=500、ttl=0.05 复现):
    写入 500 条 -> 全部过期 -> 不 set 不 get -> stats()['size'] 仍为 500
    只 get 一条    -> size 499(仅删命中那一条,其余 499 条永久滞留)
    300 条全过期、只 get 50 次 -> 250 条滞留,且 size 把它们算作存活

根因:_cleanup_expired() 只在 set() 里调用,没有后台或读路径清扫;
stats() 直接 len(self._cache),把过期但未命中的条目计入容量。

为什么不用 sleep 等过期:
    初版用 default_ttl=0.05 + asyncio.sleep(0.2) 制造过期,结果在 CI 的
    py3.10 runner 上偶发失败:填充 300 条本身的耗时超过 50ms,set() 里的
    _cleanup_expired() 边填边清,填充结束时只剩 40 条,
    `assert len(cache._cache) == 300` 收到 40 而挂(本地快机器上为 300,故不红)。
    这是典型的 wall-clock 依赖:测的是「机器有多快」而不是「清扫逻辑对不对」。

    现在改为:填充用长 TTL(填充期绝不过期,条数确定),
    再直接回拨条目的 created_at 使其立即过期 —— 过期状态是构造出来的,
    与真实时间流逝无关,结果在任何机器上都一致,且省掉 sleep。
"""
import asyncio

from cache_manager import AsyncTTLCache

# 填充期使用的 TTL:足够长,保证填充过程中不会有任何一条过期
_FILL_TTL = 60.0


def run(coro):
    return asyncio.run(coro)


async def _fill(cache, n, ttl=_FILL_TTL):
    for i in range(n):
        await cache.set(("k", i), i, ttl=ttl)


def _expire_all(cache, back=_FILL_TTL + 10.0):
    """回拨所有条目的 created_at,使其立即过期。

    直接构造过期状态,不依赖 sleep 与机器速度。
    """
    for entry in cache._cache.values():
        entry.created_at -= back


def test_get_reclaims_expired_entries_without_set():
    """静默期(只读不写)也必须回收过期条目。"""
    cache = AsyncTTLCache(max_size=500, default_ttl=_FILL_TTL, cleanup_interval=0)

    async def go():
        await _fill(cache, 300)
        _expire_all(cache)
        assert len(cache._cache) == 300   # 此刻尚未清扫(条数确定,不受机器速度影响)
        await cache.get(("k", 0))         # 一次 get 即触发清扫
        return len(cache._cache)

    # 修复前为 299(只删掉命中的那一条)
    assert run(go()) == 0


def test_stats_size_excludes_expired():
    """stats() 的 size 必须只统计未过期条目。"""
    cache = AsyncTTLCache(max_size=500, default_ttl=_FILL_TTL, cleanup_interval=0)

    async def go():
        await _fill(cache, 500)
        _expire_all(cache)
        return await cache.stats()

    stats = run(go())
    # 修复前为 500
    assert stats["size"] == 0
    assert stats["max_size"] == 500


def test_sweep_keeps_live_entries():
    """清扫不得误删未过期条目,命中与 LRU 顺序保持可用。"""
    cache = AsyncTTLCache(max_size=100, default_ttl=_FILL_TTL, cleanup_interval=0)

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
    cache = AsyncTTLCache(max_size=3, default_ttl=_FILL_TTL, cleanup_interval=0)

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
    cache = AsyncTTLCache(max_size=100, default_ttl=_FILL_TTL,
                          cleanup_interval=3600)

    async def go():
        await _fill(cache, 50)
        _expire_all(cache)
        # 读一个不存在的键:既不命中也不会就地删除任何条目,
        # 因此条目数变化只能来自全表清扫
        await cache.get(("absent",))
        return len(cache._cache)

    # 间隔未到:本次读不触发全表清扫,50 条原样保留
    assert run(go()) == 50
    assert cache._cleanup_interval == 3600


def test_expire_all_helper_actually_expires():
    """自检:_expire_all 必须真的让条目过期,否则上面几条测试会变成空断言。"""
    cache = AsyncTTLCache(max_size=100, default_ttl=_FILL_TTL, cleanup_interval=3600)

    async def go():
        await _fill(cache, 10)
        before = len(cache._cache)
        _expire_all(cache)
        expired = sum(1 for e in cache._cache.values() if e.is_expired())
        return before, expired

    before, expired = run(go())
    assert before == 10
    assert expired == 10   # 若不成立,说明回拨失效,其余测试都在空跑
