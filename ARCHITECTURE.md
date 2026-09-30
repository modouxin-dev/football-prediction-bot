# 项目最终架构 / Final Architecture

> 本文是项目的**唯一架构说明**。代码变了，本文要跟着变。

## 一、全景图

```
                        ┌─────────────────────────────┐
   外部数据源             │  API-Football (RapidAPI/官方) │
                        │  football-data.org（备用源）  │
                        └──────────────┬──────────────┘
                                       │ 异步 HTTP（缓存 + 重试 + 错误翻译）
                        ┌──────────────▼──────────────┐
   接入层                │  api_client.py / data_source.py│
                        │  football_data.py / normalize.py│
                        └──────────────┬──────────────┘
                                       │
                        ┌──────────────▼──────────────┐
   计算层（纯函数，无 IO）│  analyzer.py   泊松 + Dixon-Coles│
                        │  elo.py        Elo 评分        │
                        │  backtest.py   回测与校准       │
                        └──────────────┬──────────────┘
                                       │
                        ┌──────────────▼──────────────┐
   业务层（SSOT）        │        service.py             │
                        │  赛程 + 积分榜 + 赔率 → 预测    │
                        └──────────────┬──────────────┘
                                       │ 写入侧唯一真理来源
                        ┌──────────────▼──────────────┐
   存储层                │  repository.py  SQLite (WAL)  │
                        │  paths.py       持久化探测      │
                        └──────────────┬──────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              │                              │
┌───────▼────────┐         ┌────────────▼──────────┐       ┌─────────▼────────┐
│ Telegram 通道   │         │  定时任务通道            │       │  Web 看板通道     │
│ main.py        │         │  scheduler.py          │       │  api.py          │
│ commands/      │         │  (PTB JobQueue)         │       │  analytics.py    │
│ views/ 等      │         │  NotificationManager    │       │  web/index.html  │
└────────────────┘         └─────────────────────────┘       └──────────────────┘
```

**三个通道共用同一个 SQLite**，互不阻塞（WAL）。Web 看板只读，不消耗 API 额度。

## 二、分层与依赖规则

依赖只能**自上而下**，违反即在 CI 失败（有专门的 AST 断言守着）：

| 层 | 模块 | 允许依赖 |
|---|---|---|
| 配置/存储路径 | `config.py` `paths.py` | 标准库 |
| 静态文案 | `templates.py` | **零项目内依赖**（依赖链最底层） |
| 计算 | `analyzer.py` `elo.py` `backtest.py` | 标准库 + numpy（可选） |
| 接入 | `api_client.py` `data_source.py` `football_data.py` `normalize.py` | 配置层 |
| 业务 | `service.py` | 接入层 + 计算层 + 存储层 |
| 存储 | `repository.py` | 配置层 |
| 展示原语 | `formatkit.py` `keyboards.py` | 静态文案层 |
| 视图 | `views/*.py` | 展示原语 + 静态文案 |
| 门面 | `bot_handler.py` | 视图 + 键盘 + 原语 |
| 指令 | `commands/*.py` | 业务层 + `support.py` + `scheduler.py` |
| 调度 | `scheduler.py` | 业务层 + `support.py` |
| 入口 | `main.py` | 全部 |

**铁律**：`commands/`、`views/`、`keyboards/`、`formatkit/`、`templates` **均不得反向依赖 `main` 或 `bot_handler`**——否则形成循环导入。CI 有断言。

## 三、模块清单

### 入口与编排

| 模块 | 行数 | 职责 |
|---|---:|---|
| `main.py` | 750 | 装配依赖 → 注册指令 → 启动长轮询（重构前 1242 行） |
| `scheduler.py` | 163 | 定时推送 / 赛程同步 / 赛果结算 + `NotificationManager` |
| `support.py` | 110 | 权限、异常翻译、消息渲染（命令层与调度层共用） |

### 指令层（Command Pattern）

| 模块 | 行数 | 职责 |
|---|---:|---|
| `commands/__init__.py` | 128 | `CommandDispatcher`（查表 + 权限拦截 + 调用） |
| `commands/basic.py` | 66 | `/start` `/help` `/menu` `/web` |
| `commands/matches.py` | 89 | `/fixtures` `/predict` `/standings` `/refresh` `/analysis` `/next` `/date` |
| `commands/admin.py` | 220 | `/test` `/status` `/stats` `/storage` |
| `commands/adapters.py` | 47 | Message → CallbackQuery 适配器 |

新增指令只需在对应模块 `register` 一行，**`main.py` 不会变长**。

### 展示层（按视图垂直切分）

| 模块 | 行数 | 职责 |
|---|---:|---|
| `templates.py` | 114 | 静态文案与展示常量（零依赖） |
| `formatkit.py` | 257 | 转义、概率条、队名、积分榜摘要等原子工具 |
| `keyboards.py` | 117 | 按钮键盘布局 |
| `views/common.py` | 74 | 时间、对阵、概率条 |
| `views/prediction.py` | 292 | 预测主消息 / 卡片 / 赔率 |
| `views/analysis.py` | 182 | 深度分析 / 历史交锋 |
| `views/fixtures.py` | 138 | 赛程页 |
| `views/standings.py` | 85 | 积分榜 |
| `views/menu.py` | 155 | 欢迎 / 菜单 / 帮助 |
| `bot_handler.py` | 90 | **纯门面**（Mixin 组装 + 兼容导出），重构前 1135 行 |

**为什么按视图切分而不是抽 JSON 模板**：实测文案只占原文件 40%~60%，其余是循环与分支；抽走文案压不到目标体积。更关键的是文案与「在什么条件下说这句话」强耦合——拆开后改一句文案要跨两个文件。按视图切分，改文案只动 `views/` 里一个文件。

### 数据与计算

| 模块 | 行数 | 职责 |
|---|---:|---|
| `service.py` | 844 | 业务逻辑（SSOT） |
| `repository.py` | 754 | SQLite：WAL + 原子事务 + 指数退避 |
| `analyzer.py` | 469 | 泊松模型 + Dixon-Coles + 赔率工具（纯计算） |
| `elo.py` | 278 | Elo 评分（已实现，**未接入预测**，回测证明无效） |
| `backtest.py` | 478 | 滚动前进回测 + 校准曲线 |
| `backtest_cli.py` | 235 | 回测命令行工具 |
| `analytics.py` | 270 | 看板只读分析层 |
| `api_client.py` | 226 | API-Football 异步客户端 |
| `data_source.py` | 219 | 主备数据源路由 |
| `football_data.py` | 470 | 备用数据源 |
| `normalize.py` | 124 | 数据归一化 |
| `sync.py` | 128 | 赛程同步 |
| `chart.py` | 474 | matplotlib 出图 |
| `config.py` | 229 | 环境变量解析与校验 |
| `paths.py` | 158 | 持久化路径与探测 |
| `api.py` | 194 | Web API（可选依赖 FastAPI） |
| `migrate_elo.py` | 126 | Elo 冷启动迁移脚本 |

## 四、数据流向

```
赛程/积分榜/赔率 ──► service.predict_fixture()
                        │
                        ├─► analyzer.calculate_prediction()  泊松 + DC 修正
                        ├─► analyzer.evaluate_outcomes()     价值偏差
                        └─► Prediction 对象
                                │
                                ├─► repository.save()          落盘（用于命中率统计）
                                ├─► views/prediction.py        渲染成 HTML 消息
                                └─► scheduler.NotificationManager ──► Telegram

比赛结束 ──► service.sync_results() ──► repository.settle() ──► 回写真实比分
                                                                   │
                                                                   ▼
                                                      analytics.model_health()
                                                      （看板读，不联网）
```

## 四·补：DSA 动态强度调节（Dynamic Strength Adjustment）

> ## ⛔ 状态：**未启用 / 未验证有效 / 实测为负**
>
> **当前线上预测行为与 DSA 无关**——`service.py` 与 `analytics.py` 的所有
> 调用点均未传 `match_logs`，走的是原逻辑。DSA 是 opt-in 的死代码路径。
>
> **没有证据表明它有效，反而有证据表明它有害。** 详见下方"回测结论"。
>
> *最后复核：2026-09-30*

**位置**：`analyzer.build_league_model()`，通过**可选**参数 `match_logs` 启用。

### 数学

原强度（向联赛平均收缩后）：

```
λ = (G + prior·λ_avg) / (N + prior) / λ_avg      prior = PRIOR_GAMES = 5
```

DSA 把 (G, N) 换成**时间加权**后的 (G_eff, N_eff)，再走同一个收缩式：

```
G_eff = Σ(G_i · W_i)      N_eff = Σ W_i
λ_DSA = (G_eff + prior·λ_avg) / (N_eff + prior) / λ_avg
```

> 因为加权后仍复用同一个收缩式，所以当所有 `W_i = 1.0` 时，
> `λ_DSA` 与原 `λ` **严格相等**（等价性由 `tests/test_dsa.py` 锁住）。

### 权重分级表

| 场次（由近到远） | 权重 | 含义 |
| :--- | :---: | :--- |
| 第 1–5 场 | **1.15** | 核心状态区 |
| 第 6–15 场 | **1.0** | 趋势稳定区（基准） |
| 第 16 场起 | **0.6** | 基础实力区 |

⚠️ **权重经回测下调，非原始提案值**（提案为 `2.0 / 1.0 / 0.5`）。
权重差异过大会放大方差，代价超过"贴合近期状态"的收益。

### 回测结论（2026-09-30 复核，覆盖此前结论）

用合成联赛重跑 **20 个场景**（10 seed × drift 0/1），当前权重
`1.15 / 1.0 / 0.6` 的结果：

| 指标 | 结果 |
| :--- | ---: |
| 平均 Δ Log Loss | **+0.002747**（变差） |
| 改善场景 | **1 / 20** |
| 唯一"改善"的幅度 | −0.000049（噪声量级） |
| Δ 波动范围 | −0.000049 ~ +0.006444 |
| Δ 标准差 | 0.001595 |

平均效应是标准差的 **1.7 倍**，方向**稳定为负面**——不是随机波动。

⚠️ **此前记录的"平均 −0.00043、2/4 改善"作废。** 那组数字是在特定合成数据
构造方式下扫描出来的，换一套抽样/漂移设定即**符号翻转**，属过拟合产物，
无泛化意义。本表数据来自重建后的回测（脚本：`dsa_probe.py`）。

**根因判断（基于上述数据）**：逐场进球噪声极大（单场 0 球或 4 球的随机性
远大于"近期状态"的真实信号）。加权越激进越是在拟合噪声。

**结论：不要启用。** 等真实样本充足（≥60 场已完赛）再回来验证；合成数据上
证明不了的东西，真实数据上才有讨论资格。

### 启用条件与局限

- **线上预测路径不启用**：`service.py` 用的是 `/standings` 聚合行（只有累计
  `played` / `goals.for`，**无逐场、无日期**），构不出 `W_i` 序列。
  要启用就得新增逐场 API 请求，与"零额度开销"约束冲突。
- **本地强度榜可启用**：`analytics.py` 的 `matches` 表有逐场 `utc_date` + 比分，
  读本地库不消耗额度。
- 未提供 `match_logs` 的球队自动回退到聚合行，**不破坏既有接口**。

### 安全阀

`use_dsa_clamp=True` 时，强度被夹在 `[0.7, 1.3]`（本文件强度是 ÷ 联赛平均后的
**相对值**，故赛季平均 ≡ 1.0）。

**默认关闭**：弱队进攻 / 强队防守天然低于 0.7（19 场 0 进球的球队约 0.21），
开启会把这些真实存在的极端值强行拉回，改变既有模型行为。需真实回测数据支撑后
再决定是否开启。

## 五、并发与持久化

- **WAL**：`repository.py` 强制 `PRAGMA journal_mode=WAL`，读写不互相阻塞
- **原子事务**：写操作走 `BEGIN IMMEDIATE`，事务期间 `busy_timeout` 降到 100ms 快速失败
- **指数退避**：20ms → 40ms → 80ms + 50%~100% 随机抖动防惊群
- **实测**：10 线程写 400 场 + 持续读取线程 → 0 错误

> 为什么事务期间要**降低** busy_timeout：改之前连接级 15s 超时会先耗尽，
> 退避根本没机会接管，实测总耗时 45s；改成 100ms 后降到 **0.37s**。

## 六、测试与质量门禁

**566 个测试**，全量本地跑约 20 秒，CI 双版本矩阵（3.10 / 3.12）。

| 类别 | 文件 | 覆盖重点 |
|---|---|---|
| 模型 | `test_analyzer.py` `test_dixon_coles.py` | 泊松、DC 手算值验证（精度 1e-12） |
| Elo | `test_elo.py` `test_elo_integration.py` `test_elo_migration.py` | 零和、幂等、冷启动 |
| 回测 | `test_backtest.py` | 指标口径、滚动前进 |
| 并发 | `test_concurrency.py` | WAL、退避、400 场并发 |
| 持久化 | `test_persistence.py` | 跨重连保留、Volume 探测 |
| 指令 | `test_command_wiring.py` `test_bot.py` | 15 条指令注册与权限 |
| 模板 | `test_templates.py` `test_template_robustness.py` | 输出等价性、转义、降级 |
| 看板 | `test_analytics.py` `test_api.py` `test_web_entry.py` | 端点、响应时间、样本不足 |
| 启动 | `test_startup.py` `test_command_wiring.py` | 无未定义名、冒烟 |

**每次改动后必须跑 `verify_stage.sh`**（基线 + 补丁 → 全量 pytest → 冒烟 → 各专项校验 → 逐字节一致性）。

## 七、已实现但未接入的功能

| 功能 | 状态 | 原因 |
|---|---|---|
| Elo 评分 | 已实现，**未接入预测** | 回测 7 种子 ΔLogLoss 均值 −0.0033，0/7 为正 → 无效 |
| Dixon-Coles | 已实现，**未接入预测** | 7 种子均值 +0.0015，6/7 为正但 t=1.89 → 未达显著性死线 |

**保留代码的原因**：实现已验证数学正确；等攒够真实历史数据后可重新评估。README 已记录防止误删。
