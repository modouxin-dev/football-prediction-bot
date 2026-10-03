# 更新日志（CHANGELOG）

> **同步规则（每次推送必须遵守）**
> 1. 推送代码的同时**必须**更新本文件，新增一条 `## [提交 SHA]` 记录；
> 2. 改动文件清单从 `git show --stat` 或 GitHub API 实测拉取，**不许凭记忆写**；
> 3. 每条记录必须附实测证据（pytest 输出 / 回查 SHA / curl 返回值），
>    没跑过的标注「未验证」或不写；
> 4. 涉及用户可见行为变化（新命令、新环境变量、展示文案变化）时，
>    **同时**更新 `README.md` 与 `PROGRESS.md`；
> 5. 只改模型或内部实现、对外行为不变时，只记本文件即可。
>
> 本文件是「改了什么」的唯一权威索引。README 描述当前能力，
> CHANGELOG 描述**从哪个版本变成现在这样**。

---

## 2026-10-03 · 市场对照改用去水概率（模型 vs 市场可比口径）

### 赔率对比卡新增「去水后」对照表；Value Bet 判据保持不变

| 文件 | 改动 |
| --- | --- |
| `analyzer.py` | +56 新增 `implied_probabilities()` / `market_gap()` |
| `views/prediction.py` | +38 -11 赔率对比卡改为三行去水对照表 |
| `tests/test_market_probability.py` | **新增** 13 条 |
| `CHANGELOG.md` | 本条记录 |

**问题**：赔率对比卡原先用 `1/赔率` 当市场概率。博彩赔率含抽水
（英超实测 Σ1/o 均值 1.055），所以这个数**系统性偏高**，与模型概率
相减得到的「模型领先多少」被放大。展示层已诚实标注为「含抽水的隐含
概率」，但读者拿它做对比仍会得出偏乐观的结论。

**改法**（两个口径分离，不互相替换）：
- `implied_probabilities()`：除以三项之和归一化 → 三项和恰为 1，
  这是**对比口径**，回答「模型相对市场有没有信息优势」；
- `analyze_value()` / `edge`：**保持不变**。它等于 `p − 1/赔率`，
  与 `EV = p×赔率 − 1 > 0` 等价，是 Value Bet 的**下注口径**，数学上正确；
- 卡片同时给出两者，并明写「不可互换」。

**实测**（远端 `acd0160b` 干净副本，全量重跑）：
```
基线     786 passed
改动后   799 passed   ← +13，零破坏
```

**变异验证**（证明测试能拦错，不是摆设）：
| 注入的错误 | 被抓 |
| --- | --- |
| 去掉归一化（直接返回 1/赔率） | 3 条 FAILED |
| 视图文案改回「含抽水」 | 1 条 FAILED |
| 去掉 NaN 自检（NaN 会污染整盘概率） | 1 条 FAILED（补测后） |

第三组最初**没被抓住**——原测试没有 NaN 用例，守卫形同虚设。
补 `test_nan_price_invalidates_whole_board` 后重跑变异，确认失败。

**渲染样例**（1.91 / 3.60 / 4.20，模型 54/25/21）：
```
            主胜    平局    客胜
模型       54.0%   25.0%   21.0%
市场(去水) 50.4%   26.7%   22.9%
差值      +3.6pp  -1.7pp  -1.9pp
```
旧口径下「市场」会显示 52.8%（=1/1.91），比去水值高 2.4pp，
这 2.4pp 就是抽水造成的虚高。

---

## 2026-10-03 · 修复降级闭环：备用源 ID 不再污染主源

### [ddfa1762](https://github.com/modouxin-dev/football-prediction-bot/commit/ddfa1762edb61dce0e02e3a2552fe6f7ac7233e4) 备用源比赛 ID 按归属路由；/status 套餐归属主源账号 ✅ 当前 HEAD

| 文件 | 改动 |
| --- | --- |
| `data_source.py` | +23 -1 新增 `FIXTURE_ID_METHODS` 与 ID 归属路由守卫 |
| `commands/admin.py` | +20 -1 新增 `_account_status_preferring_primary()` |
| `tests/test_fallback_id_routing.py` | **新增** 7 条 |
| `tests/test_status_plan_ownership.py` | **新增** 4 条 |

**线上事故（由 `/diag` 暴露）**：
```
📡 主源记录  fixture: The Fixture field must contain an integer.
🧊 冷却状态  冷却中（剩余 316 秒）
```

**根因**：`normalize.py` 规定备用源 ID 统一带 `fd-` 前缀（字符串）。
主源冷却期间用备用源拉赛程 → 拿到 `fd-5001` → 查赔率时把它传给主源 →
主源拒绝并报类型错误 → 主源被判定为故障 → 进入 10 分钟冷却 →
更依赖备用源 → 产出更多 `fd-` ID → **自我维持的降级闭环**。

讽刺的是 `data_source.py:24` 早就 `from normalize import is_fallback_id`，
但**全仓没有任何使用点** —— 守卫被 import 了却没接上。

**修复**：按 ID 归属路由。带 `fd-` 前缀的 ID 只走备用源，绝不问主源，
也不参与主源的健康判定。受保护方法：`get_odds`、`get_injuries`。
备用源未配置时，可选数据静默返回空，不中断预测。

**第二处修复**：`/status` 的套餐行原先用常规路由读取，
主源冷却时读到的是备用源的 Free 档，把付费账号显示成 `Free（有效）`，
让人误以为订阅失效。套餐是主源账号的属性，与当前生效哪个源无关，
因此改为优先直连主源读取，读不到才回落。

**实测**（远端 `10aa8c98` 干净副本，非本地）：
```
基线            754 passed, 1 skipped
改动后          765 passed, 1 skipped   ← +11，零破坏
```

**变异验证**（证明测试真能拦错）：
| 注入的错误 | 结果 |
| --- | --- |
| 守卫整体关闭 | 4 条立刻 FAILED |
| `get_injuries` 漏保护 | 2 条立刻 FAILED |
| 不再优先主源读账号 | 1 条立刻 FAILED |

**环境备注**：沙盒依赖曾缺失（pytest/telegram 均无），已按
`requirements.txt` 重装后取基线，数字为重装后实测。

## 2026-10-03 · 回测样本并入内置历史（340 → 1140 场）

### [a50a6b14](https://github.com/modouxin-dev/football-prediction-bot/commit/a50a6b14d39037ec95d45194835cb5f1e5823118) 回测语料合并：库内赛果 + 镜像内置三季历史 ✅ 当前 HEAD

| 文件 | 改动 |
| --- | --- |
| `backtest_corpus.py` | **新增** 语料合并模块 |
| `tests/test_backtest_corpus.py` | **新增** 10 条 |
| `tests/test_backtest_cmd.py` | +1 条（内置历史解锁小样本）、1 条改为隔离测试 |
| `migrate_elo.py` | `fetch_finished` 增加 `utc_date`（合并排序需要，向后兼容） |
| `commands/admin.py` | `/backtest` 改用合并语料；新增「来源」行；更正口径文案 |

**为什么**：此前 `/backtest` 只吃库内当季赛果，赛季初长期卡在 340 场，
甚至触发「样本不足」而不给结论。镜像里已内置三季真实赛果，却没被回测用上。

**关键设计（队 ID 必须统一）**：
`matches` 表唯一键含 `home_team_id`；若 CSV 用 slug、库里用官方数字 id，
同一支球队会同时存在两个 id，走前回测会把它们当成两支不同的队，
历史完全接不上——合并就等于白做。
因此先用库内「队名 → id」反查表喂给 CSV 解析器（`known=`），
查不到才退回 slug 兜底。

**踩坑记录**：`repo._connect()` 返回的是**线程内共享连接**（按路径缓存），
初版在 `finally` 里 `close()` 会关掉这条共享连接、破坏后续所有数据库操作，
已修正为「只查询、不关闭」，与 `fetch_finished` 保持一致。

**实测**（远端干净副本重跑，非本地）：
```
基线（改动前）  761 passed
改动后          772 passed（+11，零破坏）
远端副本复跑    772 passed
```

**语料实测数字**：`{'db': 0, 'history': 1140, 'total': 1140, 'seasons': [2023, 2024, 2025]}`

**变异验证**（确认新测试真能拦错，不是摆设）：
```
M1 队 id 不对齐 → test_csv_team_id_reuses_db_id        FAILED
M2 不去重       → test_same_match_not_counted_twice    FAILED
M3 不按时间排序 → test_sorted_by_time                  FAILED
```

**行为变化**：库内只有 10 场时不再显示「样本不足」——内置历史已补足样本。
原「小样本拦截」逻辑**保留**，其测试改为「隔离内置历史后」验证，
另新增一条验证「有内置历史时小样本不再被拦截」。

---

## 2026-10-03 · 历史数据接入 + 框架文档同步

### [7aa95f35](https://github.com/modouxin-dev/football-prediction-bot/commit/7aa95f35944575c1f35efe4f4824744c5287e923) 内置三季英超历史数据 + 本地加载器 ✅ 当前 HEAD

| 文件 | 改动 |
| --- | --- |
| `data/history/E0_2023.csv` | **新增** 380 场 |
| `data/history/E0_2024.csv` | **新增** 380 场 |
| `data/history/E0_2025.csv` | **新增** 380 场 |
| `football_data_uk.py` | **新增** +16,291B 本地 CSV 加载器 |
| `tests/test_history_data.py` | **新增** 13 条 |

**数据来源**：用户上传的 football-data.co.uk 原始 CSV，本地精简后入库（三季合计 **1140 场**，165.8KB）。
字段：赛果 + 射门/射正 + 角球 + Bet365/Pinnacle/市场均值赔率。

**为什么需要内置**：沙盒与部署环境出网受限（历史 CSV 无法在线拉取），
内置后历史数据随仓库分发，任何环境都能做长周期回测。

**实测**（远端 `b16b0fb` 干净副本，非本地）：
```
基线    747 passed, 1 failed（748 条收集）
套用后  761 passed（748 + 13 新增），零破坏
```
> 基线那条 `test_concurrent_read_during_heavy_writes` 为**既有抖动**：
> 单独复跑 3/3 全过（10 passed），仅在满套件负载下偶发失败，非本次改动引入。

**加载器设计**：`football_data_uk.py` **仅依赖标准库**（csv/io/zlib/pathlib），
不耦合项目任何模块，可独立测试。去水后三家概率和实测 = 1.000000。

**已验证用途**：固定评估区间、只变历史量的对照实验显示，
补满历史后模型与市场的 Log Loss 差距**缩小约一半**（净改善 −0.0227）。

### [38da2210](https://github.com/modouxin-dev/football-prediction-bot/commit/38da2210b3db17145733b0b30d83ee9b0e08f6be) 修复测试时序缺陷

| 文件 | 改动 |
| --- | --- |
| `tests/test_menu.py` | +5 `today_fixtures` 跨天回退后补一次「是否在过去」判断 |

**根因**：`now + 1h` 跨 UTC 日时回退到当天 `23:00`，但该时刻已早于当前时间，
构造出的比赛全落在过去 → `next` 模式返回 `window_empty` 而非 `ok`。

**必现窗口**：UTC 每天 `23:00–24:00`，即**北京时间 07:00–08:00**。
实测 `UTC 2026-10-02 23:33` 连续 3 次全失败，非抖动。

**验证**：修复后 `tests/test_menu.py` 单独跑 3/3 全过（42 passed），全量 **761 passed 零失败**。

> 属既有缺陷，与历史数据接入无关——在此之前本次只新增文件，未改动任何 `.py`。
> 顺带说明：此前记录的「748 测试全绿」同样受此时段影响，准确表述应为
> **748 条收集 / 747 稳定通过 / 1 条时段相关失败（已修复）**。

### 文档同步（同一批）

| 文件 | 改动 |
| --- | --- |
| `CHANGELOG.md` | **新增** —— 「改了什么」的唯一权威索引 |
| `README.md` | +多联赛 / +套餐与配额 / +`/backfill` / +文档索引 |
| `PROGRESS.md` | +第 9~12 阶段 |

> 起因：改动散落在多次提交里，不看 CHANGELOG 无法回答「哪里动过」。
> 已在文件顶部写死同步规则：每次推送必须更新，附实测证据，不许凭记忆写。

---

## 2026-10-02 · 当天全部改动（6 次提交）

主线：订阅 API-Football Pro → 主源恢复 → 配额计数 → 伤停 → 历史赛季 → 多联赛。

### [b16b0fb9](https://github.com/modouxin-dev/football-prediction-bot/commit/b16b0fb9dae41b6317b8f304e1b1021991c26864) 多联赛步骤 6：展示层 ✅ 当前 HEAD

| 文件 | 改动 |
| --- | --- |
| `formatkit.py` | +20 新增 `leagues_label`（多联赛展示名，去重保序） |
| `views/menu.py` | +2/-1 菜单显示全部已启用联赛 |
| `commands/admin.py` | +2/-1 `/status` 列出全部联赛 |
| `tests/test_multileague_display.py` | **新增** +71 |

**实测**：变异验证「展示只列第一个联赛」→ 2 条测试失败；「`/status` 只列主联赛」→ 1 条失败。还原后通过。

### [217272da] 多联赛步骤 5：Elo 评分按联赛分区

| 文件 | 改动 |
| --- | --- |
| `service.py` | +22/-2 赛果回写只改本联赛球队评分 |
| `tests/test_multileague_elo.py` | **新增** +74 |

**实测**：变异验证「统一用主联赛 Elo」→ 2 条失败。
分区码：39→PL、140→PD（西甲赛果不再污染英超评分）。

### [bd70715c] 多联赛步骤 4：建模层按联赛隔离（**核心价值**）

| 文件 | 改动 |
| --- | --- |
| `service.py` | +83/-9 |
| `tests/test_multileague_model.py` | **新增** +211 |

新增 `_league_id_of`（认比赛自带的联赛 ID）、`_model_for`（按联赛取积分榜，同联赛只请求一次省配额）、`_fetch_fixtures_multi`（逐联赛拉取 + 按开赛时间合并）。

**实测**：变异验证「忽略比赛所属联赛」→ 5 条失败；「统一用主联赛建模」→ 2 条失败。
> 这一步保证：西甲比赛**不会**拿英超积分榜算强度。

### [09b310e8] 多联赛步骤 2-3：配置层 + 同步层

| 文件 | 改动 |
| --- | --- |
| `config.py` | +35/-1 新增 `league_ids` 字段与 `LEAGUE_IDS` 解析 |
| `sync.py` | +163/-76 逐联赛拉取、按联赛分区保存、单联赛失败不拖垮整体 |
| `service.py` | +7/-2 |
| `tests/test_multileague_config.py` | **新增** +60 |
| `tests/test_multileague_sync.py` | **新增** +183 |

**关键设计**：`league_ids[0]` 恒等于 `league_id`。**不配 `LEAGUE_IDS` 时行为与旧版完全一致**，零风险。
**实测**：变异验证三例全部被抓（同分区 4 条、只同步主联赛 2 条、单联赛失败即全失败 1 条）。

### [198ca9b7] 伤停接入 + 历史赛季回填

| 文件 | 改动 |
| --- | --- |
| `api_client.py` | +23 `get_injuries()`（`/injuries`，TTL 3 小时） |
| `commands/admin.py` | +78 `/backfill` 支持指定赛季与 `all` |
| `sync.py` | +51 `sync_season()` |
| `service.py` | +32 深度分析汇总伤停 |
| `views/analysis.py` | +28 展示「🚑 伤停信息」 |
| `football_data.py` / `data_source.py` | +18 / +9 备用源兼容 |
| `tests/test_injuries.py` | **新增** +60 |
| `tests/test_season_backfill.py` | **新增** +100 |

**实测**：远端 `198ca9b7` 干净副本全量 **705 passed**（基线 693 + 12）。
> 伤停**只展示、不参与模型计算**；无数据时明说「当前数据源未提供本场伤停名单」，不伪造。

### [4bdbdb2e] 配额计数：`/status` 显示真实请求次数

| 文件 | 改动 |
| --- | --- |
| `repository.py` | +81 `api_quota` 表 + `bump_quota` / `quota_today` |
| `data_source.py` | +10 `quota_sink` 回调挂在 `_log_request` |
| `main.py` | +7 注入 sink |
| `commands/admin.py` | +29/-2 分源显示 |
| `tests/test_quota.py` | **新增** +142 |

**根因**（代码实证）：football-data.org 免费层无额度查询端点，返回 `requests: {}` 空字典，故显示 `? / ?`。
**实测发现**：主源失败切备用时，**一次 `/predict` 实际消耗 2 次**请求（失败请求也发出网络往返）。

### [1f035403] 阶段 0：删除死代码

删除零调用的 `PredictionService.elo_factor_for`。当日基线起点。

---

## 累计状态（截至 38da2210）

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 持久化 | ✅ | DB 540KB，重启后保留 |
| 配额计数 | ✅ | `/status` 显示 `主源 2 / 7500｜本地实测 主源 5` |
| API-Football Pro | ✅ | `/status` 显示 `Pro（有效）` |
| 伤停展示 | ✅ | 深度分析「🚑 伤停信息」 |
| 历史赛季回填 | ✅ | `/backfill all`（**待用户实测确认解锁赛季数**） |
| 多联赛 | ✅ 代码已上线 | `LEAGUE_IDS` 未配置，默认仍单联赛 |
| **历史数据接入（1140 场 CSV）** | ✅ **已上线** | `7aa95f35`，761 passed |
| 框架文档同步 | ✅ | CHANGELOG / README / PROGRESS 三件套 |

---

## 待办与阻塞

| 项 | 状态 |
| --- | --- |
| 让 `/backtest` 吃这 1140 场（样本 340 → 1110+） | ⬜ 未开始 |
| 市场概率对照基准（历史赔率已就位） | ⬜ 未开始 |
| 把历史赛果灌进强度榜，让生产预测用完整历史 | ⬜ 未开始 |
| 国家队赛事 | 模型强度来自积分榜，国家队无积分榜，需重构为 Elo 驱动 |

---

## 附：提交历史索引（10-02 之前）

| SHA | 日期 | 摘要 |
| --- | --- | --- |
| `1f035403` | 10-02 09:29 | 阶段 0 删除死代码 |
| `e67f5c65` | 09-30 15:49 | `/backtest` 变体参数不再被忽略 |
| `b41ba62b` | 09-30 13:06 | `/backtest` 默认评估纯泊松，与线上对齐 |
| `9ee3ed3e` | 09-30 09:53 | `/backtest` 用本地真实赛果（零 API 开销） |
| `25bc74be` | 09-30 08:51 | 标记 DSA 停用并记录负向回测结果 |
| `10edb25f` | 09-30 08:31 | DSA 动态强度（需 `match_logs` 开启） |
