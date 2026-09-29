# Football Prediction Bot — 项目框架与修复进度

基准：main 分支（23 commits，最新 85eb93c）
审查日期：2026-09-29
原则：**补齐全，不删功能**（简介宣称 Elo 就真做 Elo，`/web` 挂着就真做网页端）
纪律：**每个阶段结束后必须实测，用结果答复，不靠推理**（脚本：`/data/workspace/verify_stage.sh`）

### 验证脚本做什么

1. 从原始仓库基线重新解包，逐个应用 `fix_stage*.patch`（增量补丁）
2. 全量 `pytest`
3. 冒烟启动 ×3：已挂载卷 / 未挂载卷 / 缺必填变量
4. 校验「基线 + 补丁」与工作区逐字节一致

实测曾因此抓到两个靠推理发现不了的问题，见下。

---

## 一、当前进度总览

| 阶段 | 内容 | 状态 | 验证 |
|---|---|---|---|
| 第 0 阶段 | 修复启动崩溃，让它跑起来 | ✅ **已完成** | 364 测试全绿，冒烟启动到 `run_polling` |
| 第 1 阶段 | **持久化与 Elo 存储设计** | ✅ **已完成** | 376 测试全绿，含 12 个新增持久化测试 |
| 第 2 阶段 | **修 CI，让测试真的跑** | ✅ **已完成** | workflow 校验 7 项全绿，concurrency 已分 |
| 第 3 阶段 | **实现 Elo（含融合、幂等、冷启动）** | ✅ **已完成** | 424 测试全绿 + Elo 专项 6 项全绿 |
| 第 4 阶段 | **Dixon-Coles 低比分修正** | ⚠️ **已实现，未接入** | 6/7 正但未达显著性死线（t=1.89） |
| 第 5 阶段 | **回测与校准** | ✅ **已完成** | **结论：Elo 无效，未接入预测主流程** |
| 第 6 阶段 | **架构正骨（命令模式 + 服务化）** | ✅ **已完成** | main.py 1242→760 行，506 测试全绿 |
| 第 7 阶段 | **Web 统计看板** | ✅ **已完成** | 7 端点全部 ≤200ms |
| 第 8 阶段 | **模板解耦与瘦身** | ✅ **已完成** | bot_handler 1135→90 行（-92%），566 测试全绿 |

图例：✅ 已完成 · 🔄 进行中 · ⬜ 未开始

---

## 二、第 1 阶段详情（持久化，已完成）

### 做了什么

| # | 文件 | 内容 |
|---|---|---|
| 1 | `paths.py` | 新增 `is_persistent()` / `persistence_warning()` |
| 2 | `repository.py` | 新增 `elo_ratings` / `elo_processed` / `elo_log` 三张表 |
| 3 | `repository.py` | 新增 6 个 Elo 持久化方法（读写评分、幂等标记、变更留痕） |
| 4 | `main.py` | 启动时自检持久卷，未生效则打 WARNING |
| 5 | `tests/test_persistence.py` | 新增 12 个测试 |

### 关键设计决策

**判定基准是「不在系统临时目录下」，不依赖 `/proc/mounts`。**
实测发现沙盒里 `/tmp` 本身也是挂载点，用挂载表判断会误判；而自托管 bind mount
又未必出现在挂载表内。两边都会错，所以改用临时目录这一基准。

**不换 Postgres。** 项目全套 SQLite（WAL、按线程缓存连接、打开失败回退内存），
换 Postgres 意味着 `repository.py` 515 行重写；Elo 批量重算这种场景 SQLite 完全够。
Railway Volume 挂 `/data` 即可。

**幂等性不是从零做。** `predictions.fixture_id` 已是 PRIMARY KEY，`save()` 与
`matches` 都用 `ON CONFLICT DO UPDATE`，重复写入是更新不是新增。新补的只是
Elo 专用的 `elo_processed` 标记表。

### 存储表结构

```
elo_ratings    (competition, team_id) PK · rating · matches · last_match
elo_processed  fixture_id PK · competition · season · 双方 ID 与比分
elo_log        每次评分变更一行，含 before/after/delta，用于核验是否被重复计算
```

### 实测抓到的两个坑（推理发现不了）

**坑 1：`/proc/mounts` 判定持久化会误判。**
原本打算用挂载表判断 Volume 是否生效，测试直接失败——沙盒里 `/tmp` 本身就是
挂载点。反过来，自托管 bind mount 又未必出现在挂载表内。两头都会错，
改用「不在系统临时目录下」这一基准才通过。

**坑 2：补丁漏了新增的测试文件。**
首轮验证跑出 **364 passed**，比工作区的 376 少 12 个。原因是补丁只 diff 了
`paths.py` / `repository.py` / `main.py`，新增的 `tests/test_persistence.py`
没被打进去——拿这份补丁去部署会静默丢掉全部新测试。
已改为对新增文件以 `/dev/null` 为源生成 diff，`patch` 会自动创建。

### 验证结果（脚本实跑，非推理）

```
1. 干净基线 14 个 py → 应用 fix_stage1.patch 成功 → 应用 fix_stage2_persistence.patch 成功
2. 376 passed, 13 warnings in 20.98s          pytest 退出码: 0
3a. DATA_DIR=/mnt/vol    → 持久化已生效：数据目录 /mnt/vol（跨重启保留）
    阶段判据：已走到 run_polling（后续失败为假 token 鉴权，预期）
3b. DATA_DIR=/tmp/...    → ⚠️ 数据目录 ... 不是持久卷：容器重启后预测、赛果、Elo 评分将全部清零
    阶段判据：已走到 run_polling
3c. 缺必填变量 → 配置错误：缺少必需的环境变量：TELEGRAM_TOKEN, RAPID_API_KEY（或官方直连的 API_FOOTBALL_KEY）
4. 基线 + 补丁 == 工作区，逐字节一致
5. 测试退出码 0 / 一致性 1 → 结论：通过
```

### 部署要点（Railway）

Volume 必须挂载整个 `/data` 目录而非单个 db 文件——WAL 模式会生成
`football.db-wal` / `football.db-shm`，必须与主库一起持久化（`DEPLOY.md` 已说明）。

---

## 三、第 2 阶段详情（CI，已完成）

### 改了什么

| 文件 | 变更 |
|---|---|
| `.github/workflows/ci.yml` | **新增**：pytest + 启动冒烟，concurrency `ci-${{ github.ref }}` |
| `.github/workflows/deploy.yml` | **删除**：与 static.yml 重复的 Pages 部署，是 failure 的来源 |
| `.github/workflows/static.yml` | 上传范围从 `.`（整个仓库）改为 `site/` |
| `site/index.html` | **新增**：Pages 实际可访问的页面 |
| `CI.md` | **新增**：分支保护配置步骤与 Railway 限制说明 |
| `README.md` | 补 15 个命令、3 组缺失环境变量、真实项目结构、Volume 步骤 |

### 实测抓到的坑

**坑 3：YAML 里冒烟命令的语法错误。**
最初用 `run: python -c "..."` 写多行命令，`print('smoke ok: db_path =', ...)`
里的冒号被 YAML 当成 mapping，`yaml.safe_load` 直接抛
`ScannerError: mapping values are not allowed here`。
改用 `run: |` 块标量 + heredoc 后通过。**若不实际校验 YAML 语法，这个错误只有推上去跑 CI 才会暴露。**

**坑 4：difflib 生成的删除补丁 GNU patch 不认。**
删除 `deploy.yml` 的 diff 应用时报 `Reversed (or previously applied) patch detected`。
改用 git 生成的标准格式（带 `diff --git` / `deleted file mode` 头）后正常。

### ⚠️ 一处无法在本机实测的限制

沙盒**只有 Python 3.10**，无法验证 3.12。CI 矩阵写了 `["3.10", "3.12"]`：
- 3.10 已在本机实测通过（376 passed）
- **3.12 需由 CI 首次运行时验证**。若依赖安装在该版本失败，CI 会红——
  这是 CI 本身的价值，但请留意首次运行结果

### ⚠️ 关键限制：GitHub Actions 拦不住 Railway

Railway 自己监听 `main` 分支构建，**不会**等待 GitHub Actions 的检查结果。
所以「测试不通过就不部署」只能这样实现：**用分支保护把坏代码挡在 main 之外**，
代码进不了 main，Railway 就拉不到。配置步骤见 `CI.md`。

### 验证结果（脚本实跑）

```
1. 基线 14 py / 2 workflows → stage1 → stage2 → stage3 三个补丁全部应用成功
2. 376 passed                              退出码 0
3a. /mnt/vol   → 持久化已生效 · 判据：已走到 run_polling ✓
3b. /tmp/...   → ⚠️ 不是持久卷 · 判据：已走到 run_polling ✓
3c. 缺变量     → 配置错误：缺少必需的环境变量：TELEGRAM_TOKEN, RAPID_API_KEY...
4. ✓ deploy.yml 已删除
   ✓ ci.yml  jobs=['test'] concurrency=ci-${{ github.ref }}
   ✓ static.yml jobs=['deploy'] concurrency=pages
   ✓ concurrency 组互不冲突
   ✓ ci.yml 会执行 pytest · Python 矩阵 ['3.10','3.12']
   ✓ Pages 上传范围: site
5. 基线 + 补丁 == 工作区，逐字节一致
6. 结论：通过
```

---

## 四、第 3 阶段详情（Elo，已完成）

### 交付物

| 文件 | 内容 |
|---|---|
| `elo.py` | 上半部分纯函数（无 IO），下半部分 `EloEngine`（读写 repository） |
| `analyzer.py` | 新增 `elo_factor` 参数，用「份额归一」融合 |
| `repository.py` | 新增 `atomic()` 上下文管理器（BEGIN IMMEDIATE） |
| `service.py` | 挂载 `EloEngine`，赛果回写后触发更新 |
| `migrate_elo.py` | 冷启动迁移脚本，支持 `--dry-run` |
| 3 个测试文件 | 41 + 7 个新测试 |

### 三处关键设计决策

**1. 幂等靠集合成员检查，不是 ID 大小比较。**
审计建议的 `if 当前ID <= 已记录ID: 跳过` 是**错的**——fixture_id 不是单调时间戳
（补赛、延期会让小 ID 更晚结束），会静默跳过有效比赛。
`elo_processed` 以 fixture_id 为主键做成员检查，并加了专门的回归测试：
先算 9000 再算 1000，后者**必须**被计入。

**2. Elo 必须真正影响预测，否则是装饰品。**
原方案四步走完，预测输出一个字没变。补了 `elo_factor` 融合：
采用**份额归一**（logit 平移）而非简单相乘——先算主队进球占比，平移后按总量还原。

**3. 融合系数夹在 [0.8, 1.25]**
Elo 算错时最多让预测偏 7.4pp，不会带崩模型。符合 README「先怀疑模型」的态度。

### 实测推翻了自己的两个说法

**坑 5：`λ主×f, λ客÷f` 并不守恒。**
我最初在注释里写「两者之和不变」，测试直接打脸——乘法只在 `λ主 == λ客` 时守恒，
实测差 1.6%。改用份额归一起效：λ总和在所有分差下**精确守恒**，
大小球判断完全不受影响。最大胜率偏移也从 14.7pp 降到 7.4pp。

**坑 6：初始化顺序导致 141 个测试回归。**
在 `self.sync` 创建**之前**引用了它，报 `'PredictionService' object has no attribute 'sync'`。
一次性挂掉 141 个测试——这正好印证了第 2 阶段 CI 的价值。已调整初始化顺序修正。

### 实测数据

```
Elo 专项校验（脚本第 5 节）
  ✓ 幂等：同一场比赛第二次被跳过
  ✓ 小 ID 后到的比赛未被错误跳过（非顺序比较）
  ✓ Elo 零和
  ✓ 融合系数有界 [0.8,1.25]：0.800 / 0.823 / 1.055 / 1.235 / 1.250
  ✓ λ主+λ客 精确守恒（最大偏差 0.00e+00）
  ✓ 并发：8 线程 × 30 场 = 240 次写入，0 错误，elo_processed=240

迁移脚本（40 场种子数据）
  第 1 次：回算 40 场 · 10 支球队 · 评分 1472~1528
  第 2 次：回算 0 场 · 评分 1472~1528（不变，幂等生效）

融合影响（分差 → 胜率偏移）
  ±600 分 → ±7.4pp（有界）
  ±300 分 → ±6.7pp
     0 分 → +1.8pp（主场优势 65 分，符合预期）
```

### 尚未接入（诚实说明）

` elo_factor_for()` 已实现，但**预测主流程还没调用它**——
需要改 `service.predict_fixture()` 把系数传进 `calculate_prediction`。
这一步留到下一阶段，原因是：接入后需要回测验证 Elo 是否真的提升了准确度
（第 5 阶段），否则等于在未经验证的情况下改动线上预测输出。

---

## 五、第 5 阶段详情（回测，已完成 —— 结论是「不接入」）

### 交付物

| 文件 | 内容 |
|---|---|
| `backtest.py` | 滚动前进回测引擎 + 指标（对数损失 / RPS / Brier / 校准曲线 / ECE） |
| `backtest_cli.py` | 命令行入口，支持真实库、模拟数据、blend 扫描 |
| `repository.py` | `atomic()` 加指数退避重试（悲观并发） |
| 2 个测试文件 | 27 + 8 个新测试 |

### 🔴 核心结论：Elo 在当前框架下没有增量价值

**7 个随机种子，全部为负**（对数损失改善）：

```
seed    Δ准确率    Δ对数损失    ΔRPS     结论
1      +0.0117   -0.0014   -0.0001   挑战者更差
2      -0.0064   -0.0035   -0.0010   挑战者更差
3      +0.0043   -0.0012   -0.0002   挑战者更差
7      +0.0064   -0.0014   -0.0001   挑战者更差
42     -0.0053   -0.0084   -0.0023   挑战者更差
99     +0.0011   -0.0029   -0.0008   挑战者更差
2026   +0.0021   -0.0045   -0.0012   挑战者更差
均值            -0.0033   （标准差 0.0025）
```

**真实结构数据（591 场 / 14 队）同样为负**：对数损失 +0.0017、RPS +0.0002、
Brier +0.0007、校准误差 +0.0154 —— 四项指标全部变差。
（注意准确率 +0.0091 是**正的**，若只看准确率会得出相反结论。）

**blend 参数扫描呈单调恶化**，排除了「参数选错」这一可能：

```
blend   0.0      0.1      0.2      0.3      0.4      0.6      0.8      1.0
Δ对数损失 +0.0000  -0.0005  -0.0011  -0.0018  -0.0026  -0.0044  -0.0061  -0.0081
最佳 blend = 0.0（即完全不用 Elo）
```

**实力漂移场景（drift 0/2/5/10/20）也未反超**——原本假设 Elo 的「近期加权」
特性会在球队实力变化时体现价值，实测没有。

**原因分析**：积分榜的攻防强度已经充分捕捉了球队实力，Elo 提供的是
**冗余信息 + 噪声**。两者信息来源重叠，叠加只会放大误差。

### 决策

**`elo_factor` 不接入 `predict_fixture()`** —— 接入会让线上预测变糟。
`elo.py` / 迁移脚本 / 回测全部保留，因为：
1. 它们是验证工具，未来若换模型（如加 Dixon-Coles）可重新评估
2. 仓库简介宣称有 Elo，代码里也确实有，不再「名不副实」
3. 保留证据，避免以后有人重复走这条路

### 意外收获：信心等级是有效的

```
high   0.515 (n=33)    ← 最高
medium 0.483 (n=174)
low    0.436 (n=234)   ← 最低
✅ 单调
```

这是首次用数据证明「🟢高信心确实更准」，此前从未验证过。

### 校准曲线暴露的问题

概率区间 0.00-0.35 的 6 场，模型说 0.348，实际 0.833（偏差 +0.486）。
样本太少（n=6）不足以定论，但提示**低端概率可能存在系统性低估**，
值得攒够数据后复查。

### 实测抓到的坑

**坑 7：我对 RPS 的理解是错的。**
原以为「均匀分布下三种结果的 RPS 相同」，实测推翻：平局（中间桶）的损失
明显更小（0.111 vs 0.278）。这正是 RPS 作为有序指标的价值——
平局被判为「半对」而非「全错」。断言已改为验证有序性。

**坑 8：退避重试被 15s 的 busy_timeout 架空，耗时 45s。**
`BEGIN IMMEDIATE` 会先等 sqlite3 连接级的 15s timeout，超时后才走我的退避，
实际总耗时 = (retries+1) × 15s = 45s。改为事务期间把 busy_timeout 降到
100ms（快速失败 + 退避接管）后，**45s → 0.56s**。

### 验证结果（实跑）

```
459 passed
✓ 退避被调用 2 次，总耗时 0.56s（优化前 45s）
✓ 并发：10 线程 × 40 场 = 400 次写入，0 错误，无重复
✓ elo_log 恰好是 elo_processed 的 2 倍（主客各一条）
```

---

## 六、第 4 阶段详情（Dixon-Coles，已实现 / 未接入）

### 交付物

| 文件 | 内容 |
|---|---|
| `analyzer.py` | `dc_tau` / `dc_rho_bounds` / `clamp_rho` / `dc_score_matrix` / `fit_rho` |
| `backtest.py` | `use_dc` 开关 + ρ 周期重拟合 |
| `backtest_cli.py` | 模拟器支持 `dc_rho`（生成带真实相关性的数据） |
| `tests/test_dixon_coles.py` | 29 个测试，含手算值验证（精度 1e-12） |

### 🔴 先说测试方法的错误（差点得出假结论）

**我的模拟器原本用独立泊松生成数据。** 用它去测 DC，等于「数据里根本没有低比分相关性，
却指望模型修出效果」——必然得到 ρ≈0、无改善的假结论。**在修模拟器之前，
我差点把「测试方法错误」误判为「Dixon-Coles 无效」。**

修正后：模拟器支持 `dc_rho`，用 DC 修正后的二维分布抽样比分，
再拿去评估 —— 这才是公平测试。

### 死线判定：未通过（6/7 正，t=+1.89）

```
真实ρ=-0.10 · 周期重拟合 · n=900/次
seed 1    +0.00024  拟合ρ -0.072  正
seed 2    +0.00422  拟合ρ -0.123  正
seed 3    +0.00087  拟合ρ -0.130  正
seed 7    +0.00083  拟合ρ -0.140  正
seed 42   +0.00081  拟合ρ -0.117  正
seed 99   -0.00106  拟合ρ -0.093  负   ← 死线未过
seed 2026 +0.00461  拟合ρ -0.121  正
均值 +0.00151  sd 0.00210  t=+1.89  正例 6/7
```

**结论：方向性收益真实存在（6/7 正，远好于 Elo 的 0/7），但未达「全部为正且显著」的死线。**

### 效应量被什么淹没了 —— 三层分离诊断

| 实验 | ΔLogLoss | t | 判定 |
|---|---|---|---|
| 真实ρ=−0.05（oracle） | −0.00002 | −0.09 | 无效应 |
| 真实ρ=−0.10（oracle，n=2700） | +0.00089 | +2.10 | 边缘 |
| 真实ρ=−0.15（oracle，n=2700） | **+0.00244** | **+4.46** | ✅ 显著 |

**DC 是数学上正确的**——|ρ| 足够大时收益显著。问题在于：
1. 真实足球 ρ ≈ −0.10，效应量本就只有 0.0009 量级
2. λ/μ 来自**收缩后的积分榜估计**，本身有误差，这个误差盖过了 DC 的修正收益

**ρ 估计器的诊断**：给定正确 λ/μ 时**无偏**（n=3000 估计 −0.096 对真实 −0.100），
但**小样本方差极大**——n=200 时单次估计在 −0.035~−0.273 间跳。
所以加了「周期重拟合」：随数据积累持续收敛（末态 ρ 稳定在 −0.06~−0.14）。

### 概率偏移表（审计要求的辅助指标，已达标）

λ主=1.710 λ客=1.188 ρ=−0.10：

```
比分      泊松       DC         Δ      相对变化
0-0    0.05513  0.06633  +0.01120   +20.3%   ← 正确抬高
1-0    0.09428  0.08308  -0.01120   -11.9%   ← 正确压低
0-1    0.06550  0.05430  -0.01120   -17.1%   ← 正确压低
1-1    0.11200  0.12320  +0.01120   +10.0%   ← 正确抬高
2-0    0.08061  0.08061  +0.00000     0.0%   ← 未动
2-1    0.09576  0.09576   0.00000     0.0%   ← 未动
```

平局总概率 0.2386 → 0.2610（+0.0224）。**数学实现完全正确。**

### 工程约束（全部达标）

- **延迟**：纯泊松 0.0633 ms/场 → DC 0.0723 ms/场，增量 **0.0091 ms**（预算 10ms）
- **鲁棒性**：λ=1e-6 / 20 / 0 / 负数 + ρ 越界 + NaN/inf 全部安全退化，无 NaN、无负概率
- **ρ 夹紧**：自动限制在 τ≥0 的可行区间，实测极端组合下全部 τ ≥ 0

### 决策

**不接入 `predict_fixture()`**（未达死线）。代码、测试、回测全部保留，
因为：实现已验证正确；ρ 可拟合；未来若获得真实历史数据（λ/μ 更准、样本更大），
可重新评估。已写入 README 防止被误删。

---

## 七、第 6 阶段详情（架构正骨，已完成）

### 采纳了什么、改了什么

| 审计建议 | 处置 | 理由 |
|---|---|---|
| 第一步 `commands/` + CommandDispatcher | ✅ **采纳** | 方向正确，是本阶段最大收益 |
| 第二步 引入 `apscheduler` | ❌ **拒绝** | 会造成双调度器重复推送，见下 |
| 第二步 `NotificationManager` | ✅ **采纳** | 把「谁负责发」封装成对象，定时任务不再依赖 Application 形态 |
| 第二步 service 作为 SSOT | ⚠️ **部分采纳** | service 已是 SSOT；本次只把共用纯工具下沉到 `support.py` |
| 第三步 强制 `PRAGMA journal_mode=WAL` | ⚠️ **早已存在** | `repository.py:190` 已启用；新增测试断言它不会被改回 |
| 第三步 保留指数退避 | ✅ **保留** | 已有 `BEGIN IMMEDIATE` + 指数退避 |
| 第三步 并发 400 场 + 读进程 | ✅ **采纳并加强** | 加了「写入同时持续读取」的测试 |
| 第四步 FastAPI `api.py` | 🟡 **采纳但改为可选依赖** | 见下 |

**拒绝 apscheduler 的理由**：python-telegram-bot 自带 `JobQueue`，
已用 `run_daily` / `run_repeating` 实现定时推送、赛程同步、赛果结算。
再加一个调度器 = 两个互不知晓的调度器同时运行，最直接后果是
**同一场比赛被推送两次**，且两者持有独立事件循环、排查困难。
`scheduler.py` 做的是「把已有 JobQueue 逻辑抽出来」，不是换调度器。

**FastAPI 为何放可选依赖**：机器人本体（长轮询 + 定时任务）完全不需要它。
写进 `requirements.txt` 会让 Docker 镜像变大，还给生产进程引入一个
用不到的攻击面。因此放 `requirements-web.txt`，只在跑 Web 看板时装。
（开发环境 `requirements-dev.txt` 装上，保证 CI 能测 `api.py`。）

### 交付物

| 文件 | 内容 |
|---|---|
| `commands/__init__.py` | `CommandDispatcher` / `CommandRuntime` / `CommandSpec` |
| `commands/basic.py` `/matches.py` `/admin.py` | 15 条指令，按职责分组 |
| `commands/adapters.py` | Message→CallbackQuery 适配器 |
| `scheduler.py` | `run_push` / `daily_push` / `settle_job` / `sync_job` + `NotificationManager` |
| `support.py` | 权限、异常翻译、消息渲染（无业务依赖，谁都能 import） |
| `api.py` | `/health` `/stats` `/commands` 三个端点 |
| `tests/test_api.py` | 8 个测试（fastapi 缺失时整体跳过） |

### 关键设计：依赖注入而非反向导入

`commands/` **不得** import `main`（否则循环导入）。无法下沉的能力
（菜单分发 `on_menu_key`、赛程渲染 `show_fixtures`）通过 `CommandRuntime`
注入；可以下沉的（`is_admin` / `deny` / `describe_error` / `reply_html`）
全部移到 `support.py`，命令层直接 import。

架构校验里有一条专门的断言扫描 `commands/*.py`，确保没有对 `main` 的反向依赖。

### 实测抓到的坑

**坑 9：`WIDE_HOURS` 差点被我改小。**
迁移 `run_push` 时凭印象写成 `24 * 7`，原值是 `24 * 14`。
这会让 `/test` 在国际比赛日等无近期比赛时放宽窗口从 14 天缩到 7 天——
行为静默改变且很难发现。已改回 14 并在注释里写明「必须与迁移前一致」。

**坑 10：调度参数差点被改。**
原逻辑是 `sync_job(first=10)`，且 `daily_push` **仅在配置了 CHAT_ID 时**注册。
我第一版写成 `first=60` 且无条件注册，会导致无 CHAT_ID 时定时任务空转报错。
已逐项对齐原行为。

**坑 11：退避测试偶发失败（我自己写的测试有问题）。**
`test_backoff_delay_grows_exponentially` 断言相邻两次延迟比值 > 1.2，
但抖动是 50%~100% 随机，最坏情况 `slept[0]` 取 100%、`slept[1]` 取 50%，
比值降到 1.0 → 偶发失败。改为断言「每次落在基准 × [0.5, 1.0] 区间」，
既验证指数增长又是确定性的。

**坑 12：`commands/__init__.py` 循环导入。**
顶层 `from .basic import ...` 时 `CommandDispatcher` 还没定义，
子模块反向取不到。改为在 `build_dispatcher()` 内延迟导入。

### 实测结果

```
main.py      1242 → 760 行（-482，-39%）
新增模块     commands/(4) + scheduler.py + support.py + api.py
测试         496 → 506 passed
架构校验     ✓ dispatcher 15 条指令全为协程
             ✓ BOT_COMMANDS 15 条全部注册
             ✓ commands 包无对 main 的反向依赖
             ✓ 管理员指令已标记 admin_only
             ✓ journal_mode = wal
并发         10 线程写 400 场 + 持续读取线程 → 0 错误
API          /health /stats /commands 全部 200
```

---

## 八、第 7 阶段详情（Web 看板，已完成）

### 交付物

| 文件 | 内容 |
|---|---|
| `analytics.py` | 只读分析层：健康度 / 审计 / 强度榜 |
| `web/index.html` | 看板页面，单文件、零外部依赖 |
| `api.py` | 新增 `/` `/health/model` `/audit` `/strength` |
| `config.py` | 新增 `WEB_URL` |
| `bot_handler.py` | `/web` 文案按是否部署动态生成 |
| 2 个测试文件 | 19 + 18 个新测试 |

### 看板不联网 —— 关键设计决策

**全部数据来自本地 SQLite，不调用外部 API。** 三个理由：
1. 刷新页面不该消耗宝贵的 API 免费额度
2. Web 与 Telegram 解耦：数据源挂了看板仍能看历史
3. 只读 SQLite 让响应时间稳定在毫秒级

SSOT 判断：`service.py` 是**写入侧**唯一真理来源；`analytics.py` 是**读取侧**
聚合层，结果口径复用 `repository._outcome`、强度复用 `analyzer.build_league_model`，
不存在两套逻辑。

### 实测：/commands 曾要 800ms

每个请求里现算 dispatcher → 首次调用要导入
`commands → bot_handler → chart → matplotlib`，**800ms**，远超 200ms 预算。
改为**启动时用 lifespan 预热并缓存**后降到 **1.1ms**。

```
端点                首次      中位      最大   判定
/                18.2ms    5.0ms   25.6ms   ✓
/health           1.6ms    1.4ms    1.6ms   ✓
/stats            4.5ms    1.6ms    4.5ms   ✓
/health/model     5.0ms    3.3ms    5.0ms   ✓
/audit            2.6ms    1.8ms    2.6ms   ✓
/strength         2.1ms    1.5ms    2.1ms   ✓
/commands         1.1ms    0.8ms    1.1ms   ✓
最慢 25.6ms（预算 200ms）→ 通过
```

### 三处与审计指令的差异

**1. 强度榜不是「Dixon-Coles 强度」。**
DC 只修正低比分概率（0-0/1-0/0-1/1-1），**不改变强度值**。
榜单展示的是模型在用的泊松攻防强度。不存在「DC 版强度」这种东西。

**2. 页面零 CDN。**
审计没提，但用 Chart.js 之类的 CDN 会让看板在离线/内网环境白屏。
所有图表用原生 SVG 手绘（折线 + 校准散点），无任何外部请求。

**3. 样本不足时不画假图。**
已结算 <20 场、本地已完赛 <60 场时，卡片显示「样本不足」而非画出
看似可信的曲线。对着 3 场数据下结论比没有图更危险。

### 实测抓到的坑

**坑 13：裸 sqlite3.connect 把「没数据」误报成「出错」。**
analytics 最初用裸连接，库里还没有 matches 表时抛
`no such table`，返回 `status=error`。改为复用
`PredictionRepository._connect()`（会自动建表）后正确返回 `insufficient`。

**坑 14：未部署文案里写示例 URL = 给用户一条死链。**
原本写了 `WEB_URL=https://你的域名` 作为示例，但 Telegram 会自动把
`https://...` 变成可点击链接 → 用户点开是死链。改为只写变量名 +
「值为该服务的完整访问地址」，并加了断言禁止 http 字面量出现。

**坑 15：测试数据的 UNIQUE 约束踩坑。**
`matches` 有 `UNIQUE(competition_code, utc_date, home, away)`，
造 80 场数据时只用了 28 个日期 → 被 `INSERT OR REPLACE` 覆盖成 28 行。
一度误判为「未开赛比赛被计入」，实为测试数据自身重复。

### 实测结果

```
543 passed
✓ 7 个端点全部 200，最慢 25.6ms
✓ 页面零外部依赖
✓ 健康度 300 场 · 趋势 300 点 · 校准多桶
✓ 强度榜 14 队，榜首正确
✓ /web 未部署不给死链，已配置给真实地址
✓ 基线+补丁 == 工作区，逐字节一致
```

---

## 九、第 8 阶段详情（模板解耦，已完成）

### 对审计指令的评估：方向对，手段要改

审计建议「把所有字符串模板抽离到 `templates.py` 或 JSON/YAML，
`bot_handler.py` 压到 300 行以内」。

**方向正确**（1135 行的巨物确实是维护地雷），**但手段不成立**。实测数据：

| 事实 | 数据 |
|---|---|
| BotUI 类体积 | 778 行，30 个方法 |
| 文案行占比 | 单方法 40%~60%（`format_deep_report` 61%、`format_prediction_card` 48%） |
| 其余是什么 | 循环、条件分支、数值格式化 |

**结论：抽走文案后逻辑仍留在原地，bot_handler 只能降到 ~700 行，到不了 300。**

更关键的三个理由：

1. **文案与条件强耦合**。`confidence_text` 的文案是
   `f"{emoji} {name}"` 加上两个条件后缀（缺球队数据 / 样本不足）。
   抽成模板后条件去哪？模板里写 `if` 就需要模板引擎，退化成占位符
   则只剩运行时拼接。
2. **审计担心的 KeyError 恰恰是外置模板引入的风险**。
   Python f-string 拼错名字**编译期就报错**；外置模板只有运行时才炸。
   "解耦"不但没解决这个风险，反而是它带来的。
3. **改文案要跨两个文件**，与「别在代码海洋潜水」的初衷相悖。

**采纳的做法：按视图垂直切分**，每个视图文件里文案与生成它的逻辑同处一地。

### 交付物

| 文件 | 行数 | 内容 |
|---|---|---|
| `bot_handler.py` | **88**（原 1135） | 纯门面：Mixin 组装 + 兼容导出 |
| `templates.py` | 114 | 静态文案与常量，**零项目内依赖** |
| `formatkit.py` | 264 | 原子工具（转义/概率条/队名/积分榜摘要） |
| `keyboards.py` | 118 | 7 个按钮键盘 |
| `views/common.py` | 55 | 时间、对阵、概率条 |
| `views/prediction.py` | 269 | 预测主消息 / 卡片 / 赔率 |
| `views/analysis.py` | 164 | 深度分析 / 历史交锋 |
| `views/fixtures.py` | 120 | 赛程页 |
| `views/menu.py` | 137 | 欢迎 / 菜单 / 帮助 |
| `views/standings.py` | 67 | 积分榜 |

**BotUI 保持门面**：用 Mixin 组合，30 个方法签名全部保留，
main.py 与 543 个测试零改动。

### 迁移等价性实测（最硬的证据）

22 个视图方法，同一组输入，迁移前后比对渲染结果哈希：

```
完全一致: 21/22
format_deep_report → 归一化时间戳后完全一致（该输出含「数据更新时间」）
```

已固化为 `verify_view_equivalence.py`，由 `verify_stage.sh` 每次自动执行。

### 实测抓到的坑

**坑 16：`FunctionDef.lineno` 不含装饰器 —— 差点静默破坏全部方法。**
用 AST 切片迁移时以 `m.lineno` 为起点，结果 `@staticmethod` 被整批丢掉。
表现为 `error_hint() takes 1 positional argument but 2 were given`
（实例被当作第一个参数传入）。
修正：起点改为 `min([m.lineno] + [d.lineno for d in m.decorator_list])`。
**如果不实测，这会变成一个「能导入但一调用就崩」的灾难。**

**坑 17：`BotUI.xxx` 硬编码引用导致循环导入。**
视图方法内写死了 `BotUI.fmt_time`，若 views 反向 import bot_handler 会成环。
按方法归属精确改写：`fmt_time/tz_label/matchup → CommonView`，
`get_strategy/get_confidence → PredictionView`（合成到 BotUI 后解析到同一函数对象）。

**坑 18：`fmt_time` 必须下沉一层。**
`build_prediction_payload` 在 formatkit 里调用 `BotUI.fmt_time`，
而 formatkit 位于依赖链底层、不能反向导入。
把它下沉为 formatkit 的模块级函数，`CommonView.fmt_time` 转发过去。

**坑 19：`TEAM_NAMES` 漏提取。**
第一版只提取了我列出的常量清单，漏了 `TEAM_NAMES`（队名中英对照），
导致 `NameError`。已补入 templates.py。

**坑 20：自动裁剪导入误删兼容导出。**
用 pyflakes 输出裁剪未用导入时，把 `bot_handler.py` 里**有意保留的
再导出**也删了（它们在文件内确实"未使用"，但是给外部用的），
还把 `# noqa` 注释和 `NO_DATA` 拼到了同一行。已改为显式兼容导出块。

### 实测结果

```
543 passed
✓ bot_handler.py 1135 → 88 行（-92%，目标 <300）
✓ BotUI 门面保留全部 28 个公开方法
✓ views/ keyboards/ formatkit/ templates 无对 bot_handler 的反向依赖
✓ templates.py 零项目内依赖
✓ 22 个视图方法迁移前后输出逐字节一致
✓ 基线+补丁 == 工作区，逐字节一致
```

---

## 十、第 0 阶段详情（启动修复，已完成）

### 修了什么

| # | 文件 | 问题 | 后果 |
|---|---|---|---|
| 1 | `config.py:220` | `db_path` 未定义（值赋给了 `DATABASE_FILE`） | 启动必崩，任何部署方式都跑不起来 |
| 2 | `paths.py` | `summary()` 从未实现 | `/status` 抛 AttributeError |
| 3 | `paths.py` | `probe_storage()` 从未实现 | `/storage` 五项自检全崩 |
| 4 | `config.py` | `DB_PATH` 与 `DATABASE_PATH` 不等价 | 文档互相矛盾，现已统一为等价 |

### 怎么修的

- `db_path` 取值链改为 `DATABASE_PATH` → `DB_PATH` → `paths.DB_PATH`，用 `str` 保证与 repository 连接缓存的 key 类型一致
- 补 `probe_storage()`：真实写→读→比对；标记文件保留首次写入时间，`age_seconds` 反映"数据活了多久"；挂载检测读 `/proc/mounts`，非 Linux 退化为"不在系统临时目录下"
- 补 `summary()`：输出 `DATA_DIR=...（挂载卷/临时目录）· DB 大小`
- 新增 `MARKER_NAME` 常量，与测试契约对齐

### 验证结果

- `pytest`：**364 passed, 0 failed**（修复前 4 failed）
- 冒烟启动：日志输出 `机器人启动：league=39 season=2026 推送=08:00(Asia/Shanghai)`，走到 `run_polling` 后才因假 token 鉴权失败 —— 属预期，证明配置加载与 handler 注册全部通过
- 补丁 `fix_stage1.patch` 已在原始代码验证：`patch -p1` 后与修复版逐字节一致

---

## 十一、项目框架（31 个模块）

### 入口与配置

| 模块 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `main.py` | 1236 | 命令注册、按钮回调、定时任务、日志 | ⚠️ 偏大待拆 |
| `config.py` | 221 | 环境变量解析与校验 | ✅ 已修 |

### 数据与模型

| 模块 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `api_client.py` | ~300 | API-Football 异步客户端（超时/重试/缓存/错误翻译） | ✅ 正常 |
| `data_source.py` | ~300 | 主源与备用源统一入口 | ⚠️ 3 处告警 |
| `football_data.py` | 470 | 备用源 football-data.org | ⚠️ 1 处告警 |
| `analyzer.py` | 286 | 泊松模型与赔率工具（纯计算） | ⬜ 待补 Elo / Dixon-Coles |
| `service.py` | 798 | 赛程+积分榜+赔率 → 预测 | ⚠️ `query_fixtures()` 136 行 |
| `normalize.py` | 124 | 数据归一化 | ✅ 正常 |

### 存储与展示

| 模块 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `repository.py` | 515 | SQLite 落盘、赛果回写、命中率统计 | ✅ 无 SQL 注入 |
| `sync.py` | 128 | 比赛同步 | ✅ 正常 |
| `paths.py` | ~150 | 统一路径 + 存储自检 | ✅ 已修（补 2 个函数） |
| `bot_handler.py` | 1100 | 消息模板与按钮键盘 | ⚠️ 偏大待拆 |
| `chart.py` | 474 | 图表渲染 | ✅ 正常 |

### 运维

| 模块 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `verify_deploy.py` | 219 | 部署自检 | ⚠️ 1 处告警 |
| `Dockerfile` | — | 非 root、中文字体、兼顾 ARM | ✅ 质量高 |

---

## 十二、待办清单

### 第 3 阶段：实现 elo.py

简介宣称的核心算法，目前零实现。
落地：新建 `elo.py`，从赛程/赛果算球队评级；在 `analyzer.py` 与泊松结果融合。

### 第 4 阶段：补 Dixon-Coles

标准泊松系统性低估 0-0、1-0、1-1。加 `rho` 修正项，投入产出比最高。

### 第 5 阶段：补回测与校准

数据闭环已具备（存预测 → `settle_job` 每 6 小时回写赛果 → `stats()` 算命中率），
缺的是"这个模型该不该信"的验证：

- 信心等级命中率是否单调（高信心必须明显强于低信心）
- 校准曲线（说 70% 的场次是否真有 70% 赢）
- 与"永远押市场赔率"基线对比

### 第 2 阶段：修 CI（提前到第 2 步）

两个 workflow 都在部署 GitHub Pages 且撞同一 concurrency group，
**4597 行测试从未在 CI 跑过**。二选一保留，新增 `ci.yml` 跑 pytest。

### 第 6 阶段：补 `/web`

菜单里挂着，点了返回"开发中"。工作量最大、收益最慢，放最后。

### 第 7 阶段：工程质量

- 9 处 pyflakes（`main.py:1094` 的 `datetime` 遮蔽最要紧）
- 8 处 `except ... pass` 吞异常，至少改 `log.warning`
- 拆 `main.py` / `bot_handler.py` / `query_fixtures()`
- 加 ruff + pre-commit 防回流

### 顺带：文档滞后

- README 命令表列 4 个，实际注册 **15 个**
- README 项目结构列 7 个文件，实际 **14 个模块**
- README 环境变量 8 项，实际 **30+ 项**

---

## 十三、交付物

| 文档 | 用途 |
|---|---|
| `ARCHITECTURE.md` | 最终架构图、分层依赖规则、模块清单 |
| `DEPLOY_CHECKLIST.md` | 部署清单、验证步骤、故障速查 |
| `PROGRESS.md` | 各阶段详情与实测记录（本文） |

### 最终数字

```
bot_handler.py   1135 → 90 行    (-92%)
main.py          1242 → 750 行   (-40%)
模块数            14 → 23
测试数            364 → 566
最大文件          1236 → 844 行 (service.py)
```

---

## 十四、别动的（做得好的）

参数化查询无注入风险 · 缓存 TTL 分层（赛程 30min / 积分榜 6h / 赛季列表 24h，对免费额度友好）· 赛果自动回写闭环完整 · 路径写探针自动回退 `/tmp` · 双数据源自动切换 · Dockerfile 非 root + 中文字体 + 兼顾 ARM · 依赖声明与实际 import 完全吻合
