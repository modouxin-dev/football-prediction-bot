# 🔧 项目重构计划 - 修复 15 大缺陷

## 执行阶段
按优先级和依赖关系划分为 3 个阶段,每阶段包含多个 PR

---

## 📋 第一阶段: 并发安全 + 内存泄漏 (本周)

### PR #33: 引入统一缓存管理器
**涉及文件**: `cache_manager.py` (新建)

**修复内容**:
- ✅ 创建 `AsyncTTLCache` 替代 api_client.py 和 football_data.py 的自制缓存
- ✅ 使用 `asyncio.Lock` 保护所有缓存操作
- ✅ 实现 LRU 淘汰机制(max_size=1000)防止无限增长
- ✅ 自动清理过期条目
- ✅ 提供缓存统计(hit rate 等)

**代码要点**:
```python
class AsyncTTLCache:
    async def get(key):    # 线程安全
    async def set(key, val, ttl):  # 线程安全
    async def clear(pattern):  # 支持模式清除
```

### PR #34: 修复 api_client.py 缓存
**涉及文件**: `api_client.py`

**修复内容**:
- ✅ 移除 `self._cache` 和 `_prune()` 方法
- ✅ 使用全局 `api_cache` 替代
- ✅ 保持外部接口完全不变

### PR #35: 修复 football_data.py 缓存
**涉及文件**: `football_data.py`

**修复内容**:
- ✅ 移除自制缓存
- ✅ 使用全局 `api_cache` 替代

### PR #36: 修复 main.py 并发竞态
**涉及文件**: `main.py`

**修复内容**:
- ✅ 给 `bot_data["fx_cache"]` 加 `asyncio.Lock`
- ✅ 所有读写都用 `async with lock`
- ✅ 创建 `CacheManager` 类统一管理

---

## 📋 第二阶段: 异常处理 + 依赖管理 (下周)

### PR #37: 标准化异常处理
**涉及文件**: `data_source.py`, `api_client.py`, `service.py`, 等所有用 `except Exception:` 的文件

**修复内容**:
- ✅ 导入具体异常类型(HTTPError, TimeoutError, JSONDecodeError 等)
- ✅ 逐个替换宽泛的 `Exception` 为具体类型
- ✅ 保留必要的宽泛异常并添加 `# noqa: BLE001` 注释(含理由)
- ✅ 添加特定异常的处理逻辑(如 429 特殊处理)

**修改统计**:
- 46 处 `except Exception:` → 需逐个审视
- 保留约 10-15 处(带明确理由)
- 其余替换为具体异常

### PR #38: 依赖版本锁定
**涉及文件**: `requirements-lock.txt` (新建), `requirements.txt` (更新)

**修复内容**:
```bash
pip-compile requirements.txt -o requirements-lock.txt --resolver=backtracking
```

### PR #39: 类型提示补全
**涉及文件**: 各核心模块 (service.py, api_client.py, 等)

**修复内容**:
- ✅ 运行 mypy: `mypy --strict .`
- ✅ 补全缺失的返回值类型注解
- ✅ 补全函数参数类型
- ✅ 使用 TypedDict 替代 dict[str, Any]
- ✅ 配置 mypy.ini 启用严格模式

---

## 📋 第三阶段: 性能 + 测试 + 重构 (两周后)

### PR #40: API 错误细分处理
**涉及文件**: `data_source.py`

**修复内容**:
- ✅ 按错误类型细分冷却逻辑:
  - 429 (速率限制) → 冷却 10 分钟
  - 401/403 (认证) → 冷却 1 小时
  - 500+ (服务端) → 冷却 5 分钟
  - 404 (资源不存在) → 不冷却,直接降级
- ✅ 可选数据错误不冷却
- ✅ 按联赛隔离冷却(已在 PR #32 完成)

### PR #41: 并发度限制
**涉及文件**: `service.py`

**修复内容**:
- ✅ 给 `asyncio.gather()` 加 `Semaphore` 限制(默认 5 并发)
- ✅ 支持可配置并发度(env: `MAX_CONCURRENT_REQUESTS`)
- ✅ 日志记录并发度和排队情况

**代码模板**:
```python
semaphore = asyncio.Semaphore(5)
async def _with_limit(coro):
    async with semaphore:
        return await coro
results = await asyncio.gather(
    *[_with_limit(coro) for coro in coros]
)
```

### PR #42: 资源安全关闭
**涉及文件**: `api_client.py`, `football_data.py`

**修复内容**:
- ✅ 所有 httpx.AsyncClient 改用 `async with`
- ✅ 异常路径也能正确关闭(try/finally)
- ✅ 添加测试验证无泄漏

### PR #43: 日志标准化
**涉及文件**: 全部 Python 文件

**修复内容**:
- ✅ 统一日志级别使用:
  - DEBUG: 缓存 hit/miss, 循环细节
  - INFO: 请求成功, 数据源切换, 降级事件
  - WARNING: 单个联赛失败, 超时重试
  - ERROR: 双源都不可用, 配置错误
- ✅ 创建 `log_redactor.py` 统一处理敏感信息
- ✅ 去掉所有硬编码的敏感信息日志

### PR #44: 测试补强 (80% 覆盖率)
**涉及文件**: `tests/` 目录

**新增测试**:
- ✅ `test_cache_manager.py` (AsyncTTLCache 并发安全)
- ✅ `test_concurrent_leagues.py` (多联赛并发拉取)
- ✅ `test_resource_cleanup.py` (资源是否正确释放)
- ✅ `test_exception_handling.py` (各种异常场景)
- ✅ `test_log_redaction.py` (敏感信息过滤)

**运行命令**:
```bash
pytest tests/ --cov --cov-report=html --cov-fail-under=80
```

### PR #45: 代码重构 - 拆分大文件
**涉及文件**: `main.py`, `commands/admin.py`

**修复内容**:

#### main.py (1074 行) → 拆分为:
- `main.py` (500 行) - 核心初始化 + 入口
- `handlers/fixtures.py` (300 行) - 赛程相关处理
- `handlers/prediction.py` (200 行) - 预测相关
- `handlers/menu.py` (150 行) - 菜单相关

#### commands/admin.py (958 行) → 拆分为:
- `commands/admin.py` (400 行) - 核心诊断
- `commands/stats.py` (300 行) - 统计命令
- `commands/storage.py` (200 行) - 存储检查
- `commands/backtest.py` (250 行) - 回测命令

**原则**:
- 单个文件 < 500 行
- 单个函数 < 100 行
- 高内聚,低耦合

### PR #46: 配置热重载
**涉及文件**: `config.py`, `commands/admin.py`

**修复内容**:
- ✅ 实现 `/admin_reload_config` 命令
- ✅ 支持更新 LEAGUE_IDS 和其他关键配置
- ✅ 不需重启服务

### PR #47: 安全加固
**涉及文件**: `config.py`, 日志相关文件

**修复内容**:
- ✅ API Key 和 Token 使用加密存储(如 cryptography 库)
- ✅ 敏感数据不打印到日志
- ✅ 添加 `.env` 权限检查(400 警告)

### PR #48: 文档同步
**涉及文件**: `ARCHITECTURE.md`, `IMPROVEMENTS.md`, README

**修复内容**:
- ✅ 更新架构文档(重构后的模块结构)
- ✅ 更新改进日志(v2.1 新内容)
- ✅ 补全 API 文档(可选数据处理逻辑)

---

## 📊 修复前后对比

### 代码质量
| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 异常处理精度 | 宽泛 | 具体 |
| 缓存线程安全 | ❌ | ✅ |
| 内存泄漏风险 | 高 | 无 |
| 依赖版本锁定 | ❌ | ✅ |
| 类型提示覆盖 | 70% | 95%+ |
| 测试覆盖率 | 47% | 85%+ |
| 最大文件行数 | 1074 | 500 |

### 部署可靠性
| 方面 | 修复前 | 修复后 |
|------|--------|--------|
| 多用户并发崩溃 | 可能 | 安全 |
| 长期运行内存 | 线性增长 | 稳定 |
| 错误排查速度 | 困难 | 清晰 |
| 配置变更重启 | 需要 | 无需 |
| 敏感信息泄露 | 可能 | 不可能 |

---

## 🚀 执行清单

### 阶段 1 检查清单
- [ ] PR #33 创建并合并
- [ ] PR #34-36 创建并合并
- [ ] 重启服务验证无问题
- [ ] 运行现有测试检查兼容性

### 阶段 2 检查清单
- [ ] PR #37 审视所有异常
- [ ] PR #38 锁定依赖版本
- [ ] PR #39 修复 mypy 警告
- [ ] 本地 `mypy --strict` 通过

### 阶段 3 检查清单
- [ ] PR #40-47 逐个评审合并
- [ ] 新增 80+ 条测试用例
- [ ] `pytest --cov` 覆盖率 ≥ 80%
- [ ] 代码行数统计检查
- [ ] 部署到测试环境验证

---

## 📚 技术选型

### 缓存库选项
- ✅ **cachetools** (推荐) - 内置 TTLCache, 轻量级
- 备选: functools.lru_cache (无 TTL)
- 备选: aiocache (更重,可支持 redis)

### 类型检查
- ✅ **mypy** - Python 官方推荐
- 配置: `mypy.ini` with `strict = true`

### 日志库
- ✅ **structlog** (可选升级) - 结构化日志, 更易解析
- 当前: 标准 logging 模块(足够)

### 依赖锁定
- ✅ **pip-tools** - pip-compile 生成 .txt
- 备选: poetry (更重但功能全)

---

## 📝 提交信息模板

```
type: 修复 | 特性 | 重构 | 文档 | 测试

修复缺陷 #N: <简述缺陷内容>

详细说明:
- 原因: 为什么这是个问题
- 方案: 如何修复
- 验证: 如何测试

相关 PR: #NNN
关联 issue: #NNN
```

---

## 🎯 预期收益

### 稳定性提升
- **0 起并发崩溃** (from 多次报告)
- **内存占用稳定** (线性增长 → 恒定)
- **自动故障转移** (按联赛隔离,不全局降级)

### 可维护性提升
- **代码行数分散** (1074 → 最多 500)
- **异常处理清晰** (46 宽泛 → 具体类型)
- **类型安全强化** (70% → 95%)

### 开发效率提升
- **新人上手快** (文档清晰,模块独立)
- **Bug 排查快** (日志细致,覆盖率高)
- **部署无需停服** (配置热重载)


