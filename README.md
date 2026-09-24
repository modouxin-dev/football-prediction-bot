# ⚽ Football Prediction Bot

Telegram 足球量化预测机器人：用泊松模型估算比赛结果概率，并对照市场赔率找出「价值偏差」，每天定时推送，并支持按钮查看深度分析 / 历史交锋 / 赔率对比。部署在 Railway 上。

> ⚠️ 模型只基于进球数据估算，**不构成投注建议**。市场赔率通常比简单模型更准，模型与市场差距大时，先怀疑模型。

## 功能与命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/start` `/help` | 欢迎信息、命令说明 | 所有人 |
| `/test` | 立即生成并推送一次预测（与定时任务同一路径，失败会显示具体原因） | 管理员 |
| `/status` | 版本、赛季、下次推送时间、数据源套餐与今日请求数 | 管理员 |

推送消息下方的按钮会**在原消息上切换视图**：📈 预测 · 🔍 深度分析（比分 Top5、大小球、双方进球、球队强度）· 📊 历史交锋 · 💰 赔率对比 · 🔄 刷新赔率。

## 环境变量

完整示例见 [`.env.example`](.env.example)。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | ✅ | @BotFather 提供的 token |
| `CHAT_ID` | 推送必填 | 接收推送的聊天；未设置时只停用定时推送 |
| `RAPID_API_KEY` / `API_FOOTBALL_KEY` | ✅ 二选一 | RapidAPI 订阅 / api-football.com 官方直连（设置后优先） |
| `ADMIN_ID` | | 管理员用户 ID，逗号分隔。默认等于私聊的 `CHAT_ID` |
| `LEAGUE_ID` / `SEASON` | | 默认 39（英超）/ 按日期推算的当前赛季 |
| `PUSH_TIME` / `TIMEZONE` | | 默认 `08:00` / `Asia/Shanghai`。也兼容 `SCHEDULED_HOUR`、`SCHEDULED_MINUTE` |
| `MAX_MATCHES` / `LOOKAHEAD_HOURS` | | 每次最多 3 场 / 只推未来 36 小时内开赛的比赛 |
| `LOG_LEVEL` | | 默认 `INFO` |

`DB_PATH`、`MONGODB_URI` 目前没有被使用，可以在 Railway 里删除。

## 部署到 Railway

1. 服务连接到本仓库的 `main` 分支，构建方式为 Dockerfile。
2. 在 Railway 的 Variables 里填上面的环境变量。
3. **不要**给服务设置 Cron Schedule：这是长轮询的常驻进程，定时推送由程序内部完成。
4. 推送到 `main` 后 Railway 会自动构建并部署。部署后在 Telegram 里发送 `/status` 检查，再发 `/test` 验证整条链路。

## 本地运行与测试

```bash
pip install -r requirements-dev.txt
cp .env.example .env   # 填好之后
python main.py
pytest                 # 不联网，使用仿真的 API 响应
```

## 模型说明

- 用积分榜（一次请求）得到每支球队的主/客场场均进球与失球，除以联赛平均得到攻防强度，并向 1.0 收缩（相当于补 5 场平均水平的先验比赛），避免赛季初样本太少。
- `λ主 = 主队主场进攻 × 客队客场防守 × 联赛主队场均进球`，`λ客` 同理；比分矩阵覆盖 0–10 球并归一化，汇总得到胜平负、最可能比分、大小球、双方进球概率。
- 赔率取各博彩公司「胜平负」盘口的中位数。**价值偏差 = 模型概率 − 1/赔率**，大于 0 等价于期望收益为正；超过 5% 标记为 Value Bet。
- 尚未考虑：伤停、赛程密度、Dixon-Coles 低比分修正、近期状态权重。

## 常见问题

- **`HTTP 403`**：RapidAPI 通常表示这个 Key 没有订阅 API-Football，或填错了 Key。到 RapidAPI 控制台确认订阅，或改用官方直连并设置 `API_FOOTBALL_KEY`。
- **`HTTP 429`**：请求太频繁或当日额度用尽。免费套餐额度很小，程序已对赛程、积分榜、赔率做了缓存。
- **提示套餐不支持该赛季**：免费套餐可能不开放当前赛季，需要升级套餐。
- **收不到定时推送**：发 `/status` 看「下次推送」时间；进程重启会重新计时，错过的时间点不会补发。

## 项目结构

```
main.py         入口：命令、按钮、定时任务、日志
config.py       环境变量解析与校验
api_client.py   API-Football 异步客户端（超时、重试、缓存、错误翻译）
analyzer.py     泊松模型与赔率工具（纯计算）
service.py      赛程 + 积分榜 + 赔率 → 预测
bot_handler.py  消息模板与按钮键盘
tests/          单元测试
```
