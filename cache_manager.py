"""缓存管理器:统一的线程安全缓存实现,替代散落各处的自制缓存。

特性:
- TTL 自动过期(可配置)
- LRU 淘汰(防止无限增长)
- 线程安全(用 asyncio.Lock)
- 支持手工清除
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TypeVar, Optional

log = logging.getLogger(__name__)

T = TypeVar('T')

class CacheEntry:
    """单个缓存条目"""
    def __init__(self, value: Any, ttl_seconds: float):
        self.value = value
        self.created_at = time.monotonic()
        self.ttl_seconds = ttl_seconds
    
    def is_expired(self) -> bool:
        """检查是否过期"""
        return time.monotonic() - self.created_at > self.ttl_seconds


class AsyncTTLCache:
    """异步 TTL 缓存(带 LRU 淘汰和并发安全)"""
    
    def __init__(self, max_size: int = 1000, default_ttl: float = 3600.0):
        self._cache: dict[tuple, CacheEntry] = {}
        self._access_order: list[tuple] = []  # 访问顺序(用于 LRU)
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._lock = asyncio.Lock()  # 并发保护
        self._hits = 0
        self._misses = 0
    
    async def get(self, key: tuple, default: Any = None) -> Any:
        """获取缓存值(线程安全)"""
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return default
            
            if entry.is_expired():
                del self._cache[key]
                if key in self._access_order:
                    self._access_order.remove(key)
                self._misses += 1
                return default
            
            # 更新访问顺序(移到末尾)
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)
            self._hits += 1
            return entry.value
    
    async def set(self, key: tuple, value: Any, ttl: float | None = None) -> None:
        """设置缓存值(线程安全)"""
        async with self._lock:
            ttl = ttl or self._default_ttl
            entry = CacheEntry(value, ttl)
            
            # 如果键已存在,先删除
            if key in self._cache:
                if key in self._access_order:
                    self._access_order.remove(key)
            
            self._cache[key] = entry
            self._access_order.append(key)
            
            # LRU 淘汰:超过 max_size 时删除最久未访问的
            while len(self._cache) > self._max_size:
                oldest_key = self._access_order.pop(0)
                if oldest_key in self._cache:
                    del self._cache[oldest_key]
                    log.debug(f"LRU 淘汰缓存: {oldest_key}")
            
            # 清理过期条目(每次 set 时进行一次)
            await self._cleanup_expired()
    
    async def clear(self, pattern: tuple | None = None) -> None:
        """清除缓存(可选按模式)"""
        async with self._lock:
            if pattern is None:
                self._cache.clear()
                self._access_order.clear()
                log.info("缓存已清空")
            else:
                # 清除匹配模式的所有键
                to_delete = [k for k in self._cache if k[:len(pattern)] == pattern]
                for k in to_delete:
                    del self._cache[k]
                    if k in self._access_order:
                        self._access_order.remove(k)
                log.info(f"清除缓存: {len(to_delete)} 条 (模式: {pattern})")
    
    async def _cleanup_expired(self) -> None:
        """清理过期条目(内部使用)"""
        expired_keys = [
            k for k, v in self._cache.items()
            if v.is_expired()
        ]
        for k in expired_keys:
            del self._cache[k]
            if k in self._access_order:
                self._access_order.remove(k)
        if expired_keys:
            log.debug(f"清理过期缓存: {len(expired_keys)} 条")
    
    async def stats(self) -> dict:
        """获取缓存统计"""
        async with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                'size': len(self._cache),
                'max_size': self._max_size,
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': f"{hit_rate:.1f}%",
            }
    
    async def reset_stats(self) -> None:
        """重置统计"""
        async with self._lock:
            self._hits = 0
            self._misses = 0


# 全局实例(一个给赛程,一个给 API)
fixtures_cache = AsyncTTLCache(max_size=100, default_ttl=3600)  # 赛程缓存 1 小时
api_cache = AsyncTTLCache(max_size=500, default_ttl=3600)      # API 响应缓存 1 小时

