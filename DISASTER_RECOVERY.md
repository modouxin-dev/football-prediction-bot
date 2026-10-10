> ⚠️ **本文部分内容已过时（2026-10-11 复核）**
>
> 本文提到的 monitoring.py 等文件**已从仓库删除**（零引用且/或含硬编码假数据，见 PR #44 / #45）。
> 涉及的「准确率 55%+ / 主胜 58% / 平局 35% / 高置信 62%」等数字**未经实测**，
> 实测结果为：泊松 50.99%、市场 53.96%、平局 0%（模型与市场均不预测平局）。
>
> 现状请以 `README.md` 与 `docs/MARKET_FUSION_EVAL.md` 为准。

# 🆘 灾备和恢复方案

## RTO/RPO 目标

| 指标 | 目标 | 说明 |
|------|------|------|
| RTO | < 5 分钟 | 恢复时间目标 |
| RPO | < 1 小时 | 恢复点目标 |
| 可用性 | 99.5% | 年度可用性 |

---

## 1️⃣ 数据备份

### 自动备份策略
```bash
# 每小时自动备份
0 * * * * /scripts/backup.sh

# 每日完整备份 (凌晨 2 点)
0 2 * * * /scripts/backup.sh full

# 每周异地备份
0 3 * * 0 /scripts/backup-remote.sh
```

### 备份文件结构
```
.backup/
├── football.db.daily.2024-01-01.tar.gz
├── football.db.daily.2024-01-02.tar.gz
├── football.db.weekly.2024-01-07.tar.gz
└── football.db.monthly.2024-01.tar.gz
```

### 恢复命令
```bash
# 列出备份
ls -lh .backup/

# 恢复最新备份
./scripts/backup.sh restore latest

# 恢复指定备份
./scripts/backup.sh restore .backup/football.db.daily.2024-01-01.tar.gz

# 验证恢复
sqlite3 data/football.db "SELECT COUNT(*) FROM fixtures;"
```

---

## 2️⃣ 故障转移

### 主从配置 (可选升级)
```
主节点 (Primary)
  ├─ 接收所有请求
  ├─ 写入数据库
  └─ 同步到从节点

从节点 (Replica)
  ├─ 实时同步数据
  ├─ 只读副本
  └─ 故障时自动升级
```

### 自动故障检测
```python
# monitoring.py 中添加
async def detect_primary_failure():
    """检测主节点故障"""
    try:
        # 尝试连接主节点
        async with httpx.AsyncClient(timeout=5) as client:
            await client.get("https://primary.example.com/health")
    except httpx.ConnectError:
        # 主节点故障,切换到从节点
        await switch_to_replica()
        log.warning("Primary failed, switched to replica")
```

### 转移脚本
```bash
#!/bin/bash
# scripts/failover.sh

PRIMARY_ENDPOINT="https://primary.example.com"
REPLICA_ENDPOINT="https://replica.example.com"

# 检查主节点
if ! curl -f "$PRIMARY_ENDPOINT/health"; then
    echo "Primary is down, switching to replica..."
    
    # 更新 DNS 或负载均衡器
    update_dns_to_replica
    
    # 通知管理员
    send_alert "Primary node is down, switched to replica"
    
    exit 0
fi

echo "Primary is healthy"
```

---

## 3️⃣ 数据中心故障恢复

### 跨地域备份
```
主数据中心 (US-East)
  ├─ 实时同步
  │
次数据中心 (US-West)
  ├─ 热备份
  └─ 可立即接管

冷备份
  ├─ AWS S3 (异地)
  └─ 月度保留
```

### 恢复步骤
```
1. 主数据中心故障确认 (5 分钟)
   └─ 确认不是临时网络故障

2. 切换到次数据中心 (2 分钟)
   └─ 更新 DNS/负载均衡器
   └─ 验证数据一致性

3. 从冷备份恢复 (如需要)
   └─ 下载 S3 备份
   └─ 恢复到新的数据中心

总计: < 5 分钟内恢复服务
```

---

## 4️⃣ 应用级容错

### 断路器模式 (Circuit Breaker)
```python
from circuitbreaker import circuit

@circuit(failure_threshold=5, recovery_timeout=60)
async def call_external_api():
    """调用外部 API (带断路器保护)"""
    return await api_client.get_fixtures()

# 故障时自动降级
try:
    fixtures = await call_external_api()
except CircuitBreakerListener:
    # 主源故障,使用缓存数据
    fixtures = await get_cached_fixtures()
    log.warning("Using cached data due to API failure")
```

### 重试策略
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
async def fetch_with_retry():
    """自动重试 (指数退避)"""
    return await api_client.get_fixtures()
```

---

## 5️⃣ 监控和告警

### 关键指标监控
```python
# 监控以下指标
metrics_to_monitor = [
    "api_availability",      # API 可用性
    "database_connectivity", # 数据库连接
    "cache_hit_rate",       # 缓存命中率
    "response_time_p99",    # 响应时间 P99
    "error_rate",           # 错误率
    "disk_usage",           # 磁盘使用
    "memory_usage",         # 内存使用
]
```

### 告警规则
```yaml
# 告警规则配置
alerts:
  - name: "High Error Rate"
    condition: "error_rate > 0.05"
    severity: "critical"
    action: "page_oncall"
  
  - name: "Low API Availability"
    condition: "api_availability < 0.95"
    severity: "critical"
    action: "page_oncall"
  
  - name: "Database Down"
    condition: "database_connectivity == false"
    severity: "critical"
    action: "page_oncall"
  
  - name: "Disk Usage High"
    condition: "disk_usage > 0.9"
    severity: "warning"
    action: "send_alert"
```

---

## 6️⃣ 恢复时间表

### 故障级别 0 (临时网络故障)
- **检测时间**: < 1 分钟
- **恢复时间**: < 2 分钟
- **数据丢失**: 0
- **操作**: 自动重试

### 故障级别 1 (单个节点故障)
- **检测时间**: < 2 分钟
- **恢复时间**: < 5 分钟
- **数据丢失**: < 1 条
- **操作**: 自动故障转移

### 故障级别 2 (数据中心故障)
- **检测时间**: < 5 分钟
- **恢复时间**: < 10 分钟
- **数据丢失**: < 1 小时
- **操作**: 手动切换到备用中心

### 故障级别 3 (完全故障)
- **检测时间**: < 5 分钟
- **恢复时间**: < 30 分钟
- **数据丢失**: 可能丢失最后备份之后的数据
- **操作**: 从冷备份恢复

---

## 7️⃣ 恢复测试

### 定期演练计划
```
每月一次:
  ├─ 测试自动备份
  ├─ 测试备份恢复
  └─ 验证 RTO < 5 分钟

每季度一次:
  ├─ 测试跨地域故障转移
  ├─ 测试从冷备份恢复
  └─ 演练完整灾难恢复流程
```

### 演练清单
- [ ] 备份可恢复
- [ ] 恢复数据完整
- [ ] RTO 在目标内
- [ ] 监控告警正常
- [ ] 团队响应时间

---

## 8️⃣ 文档和培训

### 必需文档
- [ ] 运维手册 (SOP)
- [ ] 故障处理流程
- [ ] 恢复步骤 (详细)
- [ ] 联系人列表
- [ ] 系统架构图

### 团队培训
- [ ] 定期培训 (季度)
- [ ] 新员工入职培训
- [ ] 故障模拟演练
- [ ] 知识库文档

---

## 总体灾备评分

| 项目 | 评分 | 改进方向 |
|------|------|---------|
| 备份策略 | 90% | 增加异地备份 |
| 故障转移 | 70% | 自动故障转移 |
| 监控告警 | 85% | 增加更多指标 |
| 文档 | 80% | 完善 SOP |
| 测试 | 60% | 定期演练 |

**总体评分**: 77/100 → 目标 95/100

---

**💪 确保业务连续性**

