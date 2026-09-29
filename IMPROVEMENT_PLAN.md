# Football Prediction Bot — 改进计划

基于 main 分支（23 commits，源码 6050 行 + 测试 4597 行）实测审查。
按「先能跑 → 再准 → 再好用 → 再好看」排序。

---

## 一、先让它跑起来（阻塞项）

### 1. `config.py:220` NameError —— 现在任何部署都启动失败

第 194 行把值赋给了 `DATABASE_FILE`，第 220 行却用未定义的 `db_path`，
而 `load_settings()` 是 `main.py:1213` 启动的必经调用。改一行即可：

```python
db_path = DB_PATH          # 原：DATABASE_FILE = DB_PATH
```

顺手删掉第 11 行未使用的 `import paths`。

### 2. CI 里没有 pytest，4597 行测试从未被执行

`.github/workflows/` 下 `deploy.yml` 与 `static.yml` **都在部署 GitHub Pages**，
且 concurrency group 同为 `"pages"` 互相抢占 —— 这是 README 页上 failure 的来源，
也是上面这个致命 bug 能活到 main 的原因。

建议：`deploy.yml` 与 `static.yml` 二选一保留（不需要 Pages 就都删），
新增 `ci.yml` 只做 `pip install -r requirements-dev.txt && pytest`。

### 3. 文档自相矛盾

- README 说「DB_PATH、MONGODB_URI 目前没有被使用」—— 这句对 `DB_PATH` 是**错的**。
  `.env.example` 同时声明「config.py 也接受 DB_PATH，与 DATABASE_PATH 等价」，
  但实际 `paths.py:30` 只读 `DATABASE_PATH`，`DB_PATH` 从未被读取。
  两条说明互斥，必须统一（建议统一到 `DATABASE_PATH`）。
- README 环境变量表只有 8 项，`.env.example` 实际有 30+ 项，
  备用数据源、赛季策略、存储路径三组全部缺失。

---

## 二、让预测更准（模型层）

### 4. Elo 评分完全没实现 —— 但仓库描述宣称有

仓库简介写的是「Poisson distribution **and Elo ratings**」，
全仓库 grep `elo` **零命中**（只有 `below` 等无关词）。
要么补实现，要么改简介，别让简介骗人。

### 5. 泊松模型的已知短板（README 已自陈，但没排期）

- **无 Dixon-Coles 低比分修正** —— 标准泊松系统性低估 0-0、1-0、1-1，
  这是学术界最常见的修正项，投入产出比最高，建议优先做
- **无近期状态权重** —— `/analysis` 已经在展示近期状态，但没进模型
- **无伤停、无赛程密度** —— 依赖数据源，优先级可低

### 6. 收缩强度写死，没有调参入口

`analyzer.py:18 PRIOR_GAMES = 5`。这个值直接决定赛季初的预测稳定性，
却既不能通过环境变量配置，也没有任何回测来证明 5 是最优的。

### 7. 缺回测与校准 —— 最该补的一环

现在存了预测、也每 6 小时自动回写赛果（`settle_job`），`stats()` 也算总命中率，
但这只能告诉你「蒙对了多少」，不能告诉你「模型该不该信」。

建议补：
- **按信心等级看命中率是否单调** —— 标「高信心」的场次命中率应该明显高于「低信心」，
  如果不是，说明信心分级是失效的
- **校准曲线** —— 模型说 70% 的场次，实际是否真的 70% 赢
- **与「永远押市场赔率」的基线对比** —— 打不赢基线，模型就没有存在价值

这件事比继续优化模型更重要：先证明有效，再谈优化。

---

## 三、工程与可维护性

### 8. 文件与函数偏大

| 文件 | 行数 | 说明 |
|---|---|---|
| main.py | 1236 | 命令注册 + 按钮回调 + 定时任务 + 日志，四件事混在一起 |
| bot_handler.py | 1100 | 几乎全是消息模板字符串 |
| service.py | 798 | `query_fixtures()` 单个函数 136 行 |

建议拆出 `handlers/`、`formatters/`、`scheduler.py`。
`query_fixtures()` 优先拆 —— 每加一个筛选条件就更难改。

### 9. 6 处 `except ... pass` 静默吞异常

线上数据源超时、解析失败时日志完全无痕，出问题只能靠猜。至少改 `log.warning`。

### 10. 12 处静态检查告警（pyflakes）

重点一个：`main.py:1094` 函数内 `from datetime import datetime` 遮蔽了第 16 行的
模块级同名导入，后续代码行为会随调用顺序变化。其余为未使用导入/变量。

### 11. 没有 lint / 格式化工位

无 ruff、无 black、无 pre-commit。加一个 `ruff` + pre-commit 就能让 8/9/10 三类问题
不再回流，成本极低。

---

## 四、功能与产品

### 12. 只支持单个联赛

`LEAGUE_ID` 是单个值。想同时看英超 + 西甲只能再部署一个实例。
改成 `LEAGUE_IDS`（逗号分隔）成本不高，但收益明显。

### 13. `/web` 是空头支票

`format_help()` 里明写「网页端　浏览器查询入口（规划中）」，点了只会返回
「功能开发中」。要么实现，要么把菜单项摘掉。

### 14. `agent-terminal/` 目录游离

内含 `gateway/`、`worker/`、`secrets/`、`deploy.sh`，与主 bot 无代码关联
（`secrets/` 只有 .gitignore，无泄露风险，这点做得对）。
要么写清它的定位，要么移出仓库，否则新人根本看不懂。

### 15. 中英混排不统一

消息模板中英混排（`📅 今日赛程 / Fixtures`），界面语言没有统一策略。

---

## 五、做得好的地方（别改坏了）

- **无 SQL 注入**：`repository.py` 全参数化查询
- **缓存 TTL 设计合理**：赛程 30min、积分榜 6h、赔率 15min、H2H/form 12h、
  赛季列表 24h —— 对免费套餐的小额度很友好
- **赛果自动回写**：`settle_job` 每 6 小时跑一次，预测-结果闭环完整
- **路径写探针 + 自动回退**：挂载卷缺失时回退 `/tmp`，不直接崩
- **双数据源**：主源挂了自动切 football-data.org
- **Dockerfile 质量高**：非 root、装中文字体、兼顾 ARM 编译、注释解释每个决策
- **依赖声明干净**：5 个依赖与实际 import 完全吻合
- **免责声明到位**：「模型与市场差距大时，先怀疑模型」—— 这个态度是对的

---

## 六、建议执行顺序

| 顺序 | 事项 | 成本 | 收益 |
|---|---|---|---|
| 1 | 修 `db_path` NameError | 1 行 | 从「跑不起来」到「能跑」 |
| 2 | 加 pytest CI | 小 | 防止 P0 再次进 main |
| 3 | 统一 DB_PATH / 补 README 变量表 | 小 | 消除配置误导 |
| 4 | 补回测与校准曲线 | 中 | 验证模型到底有没有用 |
| 5 | Dixon-Coles 低比分修正 | 中 | 最直接的准确度提升 |
| 6 | Elo：实现或改简介 | 中/1 行 | 名实相符 |
| 7 | 加 ruff + pre-commit | 小 | 让 8/9/10 不再回流 |
| 8 | 拆分 main.py / bot_handler.py | 中 | 长期可维护性 |
| 9 | 多联赛支持 | 中 | 功能扩展 |
