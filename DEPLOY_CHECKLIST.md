# 部署清单 / Deployment Checklist

按顺序走一遍，每步都要有可观测的结果。**跳过任何一步都可能静默失败**——
这个项目绝大多数问题的症状都是「部署成功但什么都不推送」。

## 一、部署前（本地）

- [ ] `pip install -r requirements-dev.txt`
- [ ] `pytest` → 必须 **566 passed, 0 failed**
- [ ] `bash verify_stage.sh` → 结论必须是「通过」
- [ ] 确认 `.env` 里三个必需变量已填：`TELEGRAM_TOKEN` `CHAT_ID` `RAPID_API_KEY`（或 `API_FOOTBALL_KEY`）

## 二、Railway 服务配置

- [ ] 服务连接到仓库 **main** 分支，构建方式 **Dockerfile**
- [ ] 挂载 **Volume 到 `/data`**（不是 `/data/football.db`）
      > 必须挂整个目录：WAL 模式会生成 `football.db-wal` 和 `football.db-shm`，
      > 只挂 db 文件会丢最近未合并的写入。
- [ ] **不要**设置 Cron Schedule —— 这是长轮询常驻进程，定时推送由程序内部调度

## 三、环境变量

### 必填

| 变量 | 说明 |
|---|---|
| `TELEGRAM_TOKEN` | @BotFather 提供 |
| `CHAT_ID` | 私聊填用户 ID；群组为负数；频道填 `@频道名` |
| `RAPID_API_KEY` 或 `API_FOOTBALL_KEY` | 二选一，同时填时优先后者（官方直连） |

### 强烈建议

| 变量 | 说明 |
|---|---|
| `ADMIN_ID` | 管理员 ID，逗号分隔。不填时私聊的 CHAT_ID 自动成为管理员 |
| `FOOTBALL_DATA_API_TOKEN` | 备用数据源 Token（免费），主源挂掉时接管 |
| `DATA_DIR=/data` | 与 Volume 挂载点一致 |

### 可选

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LEAGUE_ID` | 39（英超） | 140西甲 / 135意甲 / 78德甲 / 61法甲 / 2欧冠 |
| `SEASON` | 按日期推算 | 2026-27 赛季填 2026 |
| `PUSH_TIME` / `TIMEZONE` | 08:00 / Asia/Shanghai | 也兼容 `SCHEDULED_HOUR` + `SCHEDULED_MINUTE` |
| `MAX_MATCHES` / `LOOKAHEAD_HOURS` | 3 / 36 | 每次推送场数、只看未来多少小时开赛的 |
| `LOG_LEVEL` | INFO | |
| `WEB_URL` | 空 | 网页看板地址，配置后 `/web` 给出可点击链接 |
| `SEASON_MODE` / `ALLOW_SEASON_FALLBACK` | auto / true | 赛季不可用时的降级策略 |

> `DB_PATH` 与 `DATABASE_PATH` 等价（后者优先）。两者都不填时用 `paths.DB_PATH`。
> `MONGODB_URI` 未被使用，可以从 Railway 删掉。

完整清单见 `.env.example`（30+ 项，README 的表格是精简版）。

## 四、部署后验证（**必做**，顺序不能乱）

1. [ ] 发 `/status`
      - 版本号是否为刚推的 commit（`RAILWAY_GIT_COMMIT_SHA` 前 7 位）
      - 「下次推送」时间是否正确
      - 「数据源连通：✅」
      - 若显示「⚠️ 按日期应为 XXXX」→ `SEASON` 填错了
2. [ ] 发 `/test`
      - 应收到真实预测消息
      - 若失败，**消息里会写明具体原因**（403 / 429 / 套餐不支持该赛季…）
      - 若显示「没有可推送的比赛」→ 窗口内确实无比赛，属正常
3. [ ] 发 `/storage`
      - **五项自检全绿**，特别是「Volume 挂载：已生效」
      - 首次执行显示「未确认」是正常的
4. [ ] **重新部署一次**，再发 `/storage`
      - 标记文件的写入时间**仍在** → Volume 确实生效
      - 时间重置 → 数据在重启后丢失，检查 Volume 挂载路径

> 第 4 步是唯一能证明持久化的证据。跳过它，等到某天发现历史预测全没了才回头查，成本高得多。

## 五、Web 看板（可选）

- [ ] 安装可选依赖：`pip install -r requirements-web.txt`
- [ ] 启动：`python -m uvicorn api:app --host 0.0.0.0 --port 8000`
- [ ] 设置 `WEB_URL=https://你的域名`
- [ ] 浏览器打开，确认三张卡片都有数据（模型健康度 / 强度榜 / 预测审计）
- [ ] `curl $WEB_URL/health` → `persistent: true`

> FastAPI **不在** `requirements.txt` 里：机器人本体不需要它，装进去只会让镜像变大、
> 多一个用不到的攻击面。

## 六、常见故障速查

| 症状 | 原因 | 处理 |
|---|---|---|
| HTTP 403 | Key 没订阅 API-Football，或填错 Key | 到 RapidAPI 控制台确认订阅，或改用官方直连 |
| HTTP 429 | 请求太频繁 / 当日额度用尽 | 免费套餐额度很小；程序已做分层缓存（赛程30min / 积分榜6h） |
| 提示套餐不支持该赛季 | 免费套餐可能不开放当前赛季 | 升级套餐，或调低 `SEASON` |
| 收不到定时推送 | 进程重启会重新计时，错过的点不补发 | 发 `/status` 看下次推送时间 |
| 「⚠️ 不是持久卷」 | Volume 没挂到 `/data` | 见第四节第 4 步 |
| `/web` 说未部署 | 没设 `WEB_URL` | 按第五节配置 |

## 七、上线后的观察项

- [ ] 攒够 **20 场以上**已结算预测后，看一次看板的「模型健康度」
      - 校准曲线是否贴近对角线
      - 信心等级命中率是否单调（🟢高 > 🟡中 > 🔴低）
- [ ] 攒够 **60 场以上**本地赛果后，「强度榜」才会显示（样本不足时故意不画）
- [ ] 有 **几百场真实赛果**后，可重跑一次回测评估 Dixon-Coles 是否值得接入
      - `python backtest_cli.py --db /data/football.db`
      - 当前结论：模拟数据下未达显著性死线，**暂不接入**

## 八、发布门禁（DoD）

每次改动合并到 main 前：

- [ ] 全量 `pytest` 通过
- [ ] CI 双版本（3.10 / 3.12）绿灯
- [ ] **README / ARCHITECTURE.md 同步更新**（代码改了文档不改 = 给未来埋雷）
- [ ] 若改动涉及配置或存储，补充 `/storage` 验证步骤

> GitHub Actions **拦不住 Railway**：Railway 自己监听 main 构建，不等 CI 结果。
> 真正能拦住的是**分支保护**——配置 Settings → Branches → main →
> `Require status checks to pass`，勾选 `test (py3.10)` 与 `test (py3.12)`。
> 进不了 main，Railway 就拉不到坏代码。
