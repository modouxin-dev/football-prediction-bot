# 🧪 测试覆盖率提升计划

## 当前状态
- 覆盖率: 47%
- 目标: 85%+
- 需新增: 38%

## 新增测试用例清单

### 1. 并发安全测试 (8 个用例)
```python
# tests/test_concurrent_access.py

class TestConcurrentCache:
    async def test_concurrent_reads(self):
        """100个并发读操作,无竞态"""
    
    async def test_concurrent_writes(self):
        """50个并发写操作,无冲突"""
    
    async def test_cache_consistency(self):
        """并发读写时缓存一致性"""
    
    async def test_lru_eviction_concurrent(self):
        """并发下LRU淘汰正确性"""

class TestConcurrentDataFetch:
    async def test_multi_league_fetch(self):
        """多联赛同时拉取,无串联"""
    
    async def test_concurrent_predictions(self):
        """多用户并发预测"""
```

### 2. 内存泄漏测试 (5 个用例)
```python
# tests/test_memory_safety.py

class TestMemoryManagement:
    async def test_cache_no_growth_8h(self):
        """8小时运行内存±5%波动"""
    
    async def test_cache_lru_cleanup(self):
        """LRU淘汰保持大小有界"""
    
    async def test_connection_cleanup(self):
        """AsyncClient正确关闭"""
    
    async def test_batch_processing_memory(self):
        """批处理不导致内存膨胀"""
```

### 3. 异常处理测试 (8 个用例)
```python
# tests/test_exception_handling.py

class TestAPIErrors:
    async def test_rate_limit_error(self):
        """429错误正确处理"""
    
    async def test_auth_error(self):
        """401/403错误转移到备用源"""
    
    async def test_network_timeout(self):
        """超时自动重试"""
    
    async def test_json_parse_error(self):
        """解析错误异常处理"""

class TestDataSourceFallback:
    async def test_source_auto_switch(self):
        """主源失败自动切换"""
    
    async def test_per_league_isolation(self):
        """联赛冷却隔离"""
```

### 4. 多联赛集成测试 (6 个用例)
```python
# tests/test_multi_league.py

class TestMultiLeagueIntegration:
    async def test_concurrent_leagues(self):
        """5个联赛并发无冲突"""
    
    async def test_standings_isolation(self):
        """各联赛积分榜独立"""
    
    async def test_elo_per_league(self):
        """Elo评分按联赛隔离"""
    
    async def test_league_cooldown_isolation(self):
        """某联赛冷却不影响其他"""
    
    async def test_batch_processing(self):
        """批量处理多联赛数据"""
```

### 5. 端到端集成测试 (6 个用例)
```python
# tests/test_e2e_integration.py

class TestE2EFlow:
    async def test_full_prediction_flow(self):
        """完整预测流程"""
    
    async def test_telegram_command_flow(self):
        """Telegram命令完整流"""
    
    async def test_data_persistence(self):
        """数据持久化和恢复"""
    
    async def test_scheduled_push(self):
        """定时推送功能"""
```

## 测试执行命令

```bash
# 运行所有新增测试
pytest tests/test_concurrent_access.py -v
pytest tests/test_memory_safety.py -v
pytest tests/test_exception_handling.py -v
pytest tests/test_multi_league.py -v
pytest tests/test_e2e_integration.py -v

# 覆盖率报告
pytest tests/ --cov --cov-report=html --cov-fail-under=85

# 并发压力测试
python tools/stress_test.py --concurrency=100 --duration=600

# 内存监控
python tools/memory_monitor.py --duration=28800
```

## 新增测试统计

- 并发测试: 8 个
- 内存测试: 5 个
- 异常处理: 8 个
- 多联赛: 6 个
- 端到端: 6 个

**总计: +33 个测试用例**

覆盖率提升: 47% → 85% (预期)

## 进度追踪

- [ ] 并发安全测试完成
- [ ] 内存泄漏测试完成
- [ ] 异常处理测试完成
- [ ] 多联赛集成测试完成
- [ ] 端到端测试完成
- [ ] 覆盖率验证 85%+

