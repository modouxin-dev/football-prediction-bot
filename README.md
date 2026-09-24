# ⚽ Football Prediction Bot - 足球量化预测机器人

一个基于 **泊松分布** 的足球比赛量化预测 Telegram 机器人，提供每日重点赛事分析、赔率评估和历史对阵数据。

## 🎯 核心功能

### 1. **量化预测分析** 
- 基于泊松分布的数学模型
- 实时计算胜平负概率
- 预测最可能的比分
- 预期进球数 (xG) 分析

### 2. **赔率价值评估**
- 将模型概率与市场赔率对比
- 识别高价值投注机会 (Value Bet)
- 支持多个博彩公司赔率对比

### 3. **历史数据分析**
- H2H 历史对阵统计
- 球队进攻防守强度计算
- 赔率走势追踪

### 4. **自动推送**
- 每日定时推送 (可配置时区)
- 选择性推送前 N 场重点赛事
- Telegram 消息分享和讨论

### 5. **交互式界面**
- 内联按钮快速查询
- 深度分析报告
- 错误处理和友好提示

---

## 📋 需求

- Python 3.10+
- Telegram Bot Token (从 [BotFather](https://t.me/botfather) 获取)
- RapidAPI Football API Key (免费获取 [https://rapidapi.com/api-sports/api/api-football](https://rapidapi.com/api-sports/api/api-football))
- Docker (可选，用于容器部署)

---

## 🚀 快速开始

### 1. 本地安装和运行

```bash
# 克隆仓库
git clone https://github.com/modouxin-dev/football-prediction-bot.git
cd football-prediction-bot

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt

# 复制环境模板并填入配置
cp .env.example .env
# 编辑 .env，填入你的 API Key 和 Bot Token

# 运行机器人
python main.py
```

### 2. Docker 部署

```bash
# 构建镜像
docker build -t football-bot .

# 运行容器
docker run -d \
  -e TELEGRAM_TOKEN=your_token \
  -e RAPID_API_KEY=your_key \
  -e LEAGUE_ID=39 \
  -e SEASON=2024 \
  -e CHAT_ID=your_chat_id \
  -e TIMEZONE=UTC \
  --name football-bot \
  football-bot
```

### 3. Railway 部署

```bash
# 使用 Railway CLI
railway up

# 或在 Railway 仪表板配置环境变量后自动部署
```

---

## ⚙️ 配置

编辑 `.env` 文件配置以下参数：

```env
# 必需：Telegram Bot Token
TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# 必需：RapidAPI Football API Key
RAPID_API_KEY=your_rapidapi_key_here

# 可选：足球联赛 ID (默认=39 英超)
LEAGUE_ID=39
# 常见联赛代码：
#   39 = 英超 (Premier League)
#   140 = 西甲 (La Liga)
#   135 = 意甲 (Serie A)
#   78  = 德甲 (Bundesliga)
#   61  = 法甲 (Ligue 1)

# 可选：赛季 (默认=2024)
SEASON=2024

# 必需：接收消息的 Telegram Chat ID
CHAT_ID=1234567890

# 可选：时区 (默认=UTC)
TIMEZONE=UTC
# 示例：Asia/Shanghai, Europe/London, America/New_York
```

### 获取 Chat ID

1. 将机器人添加到群组或直接聊天
2. 给机器人发送任意消息
3. 访问 `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. 查找 `chat.id` 字段

---

## 💬 Telegram 命令

- `/start` - 显示欢迎信息和功能介绍
- `/test` - 手动测试推送（立即生成预测）
- `/help` - 显示命令列表和预测解读指南
- `/status` - 查看机器人运行状态和配置

### 内联按钮功能
消息中的按钮提供快速操作：
- 🔍 **深度分析** - 显示比分概率矩阵和统计信息
- 📊 **H2H对阵** - 历史对阵统计（近10场）
- 📉 **赔率走势** - 多个博彩公司赔率对比
- 🔄 **刷新数据** - 重新获取最新数据

---

## 📊 预测解读指南

### 核心指标

#### 1. **胜平负概率** 
基于泊松分布计算的历史概率分布

#### 2. **预期比分** (Most Likely Score)
概率矩阵中最可能发生的比分

#### 3. **预期进球数** (Expected Goals / xG)
- λ_home = 主队进攻强度 × 客队防守强度 × 联赛平均进球数
- λ_away = 客队进攻强度 × 主队防守强度 × 联赛平均进球数

#### 4. **价值度** (Value Bet)
```
Value = 模型概率 - 隐含概率
      = P(Model) - 1/赔率

Value > 5%  → 推荐投注
Value > 10% → 强烈推荐
```

### 信心指数
- ⭐⭐⭐⭐⭐ (极高) - 概率 > 75%
- ⭐⭐⭐⭐ (高) - 概率 > 65%
- ⭐⭐⭐ (中等) - 概率 > 50%
- ⭐⭐ (低) - 概率 > 35%
- ⭐ (极低) - 概率 ≤ 35%

### 建议策略
| 条件 | 建议 |
|------|------|
| Value > 10% | 🚀 强势主胜推荐 |
| Value > 5% | 👍 主胜价值推荐 |
| Win Prob > 65% | ✅ 主队不败 (1X) |
| Win Prob > 50% | 📊 主队微弱优势 |
| Draw Prob > 40% | 🤝 平局可能性大 |
| 其他 | ⚠️ 观望/小注 |

---

## 🔧 开发指南

### 项目结构
```
football-prediction-bot/
├── main.py              # 机器人主入口，事件处理
├── analyzer.py          # 量化分析引擎（泊松分布）
├── api_client.py        # RapidAPI 数据接口封装
├── bot_handler.py       # Telegram 消息格式化
├── requirements.txt     # Python 依赖
├── Dockerfile           # Docker 容器配置
├── .env.example         # 环境变量模板
├── README.md            # 本文件
└── bot.log              # 运行日志 (自动生成)
```

### 主要类和方法

#### `MatchAnalyzer`
```python
# 计算预测结果
analysis = analyzer.calculate_prediction(
    home_stats={'attack': 1.2, 'defense': 0.8},
    away_stats={'attack': 1.1, 'defense': 1.0}
)
# 返回：{'win_prob', 'draw_prob', 'loss_prob', 'best_score', 'lambda_*', ...}

# 评估赔率价值
value = analyzer.analyze_value(model_prob=0.55, odds=2.0)
```

#### `FootballAPI`
```python
# 获取赛程
fixtures = api.get_fixtures(league_id=39, season=2024)

# 获取球队统计
stats = api.get_statistics(team_id=33, league_id=39, season=2024)

# 获取H2H历史
h2h = api.get_h2h(home_team_id=33, away_team_id=8)

# 获取赔率
odds = api.get_odds(fixture_id=123456)
```

#### `BotUI`
```python
# 格式化预测消息
msg = ui.format_prediction(
    match_data={'league': '英超', 'home': 'Arsenal', ...},
    analysis={...},
    value_bet=0.08
)

# 获取策略建议
strategy = ui.get_strategy(analysis, value_bet)
```

---

## 🐛 常见问题

### Q: 机器人没有发送消息
**A:** 
1. 检查 TELEGRAM_TOKEN 是否正确（使用 `/getMe` 测试）
2. 确认 CHAT_ID 正确（查看 getUpdates 返回）
3. 检查 bot.log 日志文件查看错误

### Q: 赔率数据为空
**A:**
- RapidAPI 的赔率数据仅部分比赛有效
- 某些赛事没有赔率覆盖
- 高级账户获取更多赔率源

### Q: API 频率限制
**A:**
- 免费层有请求限制
- 设置合理的缓存时间 (默认 1 小时)
- 考虑升级 API 计划

### Q: 如何修改推送时间？
**A:** 编辑 `main.py` 的定时任务配置：
```python
scheduler.add_job(
    send_daily_prediction,
    'cron',
    hour=8,      # 改为想要的小时 (0-23)
    minute=0,    # 改为想要的分钟
    ...
)
```

---

## 📈 改进计划

- [ ] 数据库持久化（记录历史预测准确率）
- [ ] Web 仪表板（可视化分析报告）
- [ ] 多语言支持
- [ ] 用户偏好设置（自定义推送时间、联赛）
- [ ] 高级分析（进攻防守热区、关键球员数据）
- [ ] 实时赔率监控和警报
- [ ] Webhook 回调功能完整实现

---

## 📜 许可证

MIT License - 详见 LICENSE 文件

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📧 联系方式

- GitHub Issues: 提交 Bug 报告和功能请求
- 讨论：在 GitHub Discussions 中交流

---

**最后更新**：2024年9月24日  
**维护者**：modouxin-dev

