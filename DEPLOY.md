# 部署指南（Railway 之外的落地方案）

本项目是**长轮询常驻进程**，不监听端口。这决定了它对托管平台有三个硬性要求：

| 要求 | 原因 | 不满足的后果 |
|---|---|---|
| 常驻不休眠 | 轮询进程停了就收不到消息 | 机器人下线，命令无响应 |
| 持久卷挂载到 `/data` | SQLite 存预测记录与赛后回写 | 重部署后历史与命中率统计清零 |
| 能直连 `api.telegram.org` | 长轮询要持续访问 Telegram | 国内云直连通率不足 40%，机器人反复掉线 |

> ⚠️ 第三条是隐性门槛：国内云（阿里云/腾讯云等）不适合直接部署，除非额外配代理中转。

---

## 方案对比（2026 现状）

| 方案 | 成本 | 常驻 | 持久化 | 直连 TG | 门槛 |
|---|---|---|---|---|---|
| **Oracle Cloud 免费 ARM** | ¥0 永久 | ✅ | ✅ 200G | ✅ | 需信用卡验证；A1 容量要抢 |
| **Fly.io** | ~$2–5/月 | ✅ | ✅ 卷 | ✅ | 需绑卡，按量计费 |
| **Zeabur** | $5 额度/月 | ✅ | ⚠️ 需付费档 | ✅ | 免费档仅 256MB，有 OOM 风险 |
| Koyeb | 免费 1 个服务 | ✅ | ❌ 免费档不支持卷 | ✅ | 不适合本项目（要落盘） |
| Render | 免费档 | ❌ 15 分钟休眠 | ❌ | ✅ | 免费档不满足常驻 |
| 国内云服务器 | ¥10–30/月 | ✅ | ✅ | ❌ 需代理 | 网络不通，不推荐 |

**结论**：要长期零成本 → Oracle；要最快上线、能接受小额付费 → Fly.io。

---

## 方案 A：Oracle Cloud 免费 ARM（推荐，永久 ¥0）

规格：2 OCPU + 12GB 内存 + 200GB 块存储 + 10TB/月流量，ARM（Ampere A1），**永不收费**。

### 1. 开实例
- 注册 [cloud.oracle.com](https://www.oracle.com/cloud/free/)，信用卡仅用于身份验证（临时预授权，会自动撤销）
- Home Region 一选定终身不可改，亚太建议选 **东京 / 大阪 / 新加坡**
- Compute → Instances → Create，shape 选 `VM.Standard.A1.Flex`，配 **2 OCPU / 12GB**
- 镜像选 **Ubuntu 22.04**（标记 Always Free Eligible）
- 若提示 `Out of host capacity`：换个可用域重试，或用脚本定时重试——ARM 免费资源长期超售

### 2. 开端口
机器人是出站轮询，不需要入站端口。但要在 **Security List** 放行 SSH（22）以便登录。

### 3. 装 Docker 并部署
```bash
ssh ubuntu@<公网IP>

# 安装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 注销重登生效

# 拉代码
git clone https://github.com/modouxin-dev/football-prediction-bot.git
cd football-prediction-bot

# 配环境变量
cp .env.example .env
nano .env        # 填 TELEGRAM_TOKEN / API_FOOTBALL_KEY / CHAT_ID

# 启动
docker compose up -d --build
```

### 4. 常用运维
```bash
docker compose logs -f        # 看日志
docker compose restart        # 重启
docker compose pull && docker compose up -d   # 更新代码后重建
docker compose exec bot ls -la /data          # 确认数据库已落盘
```

数据落在仓库同级 `./data/`，建议定期打包备份：
```bash
tar -czf backup-$(date +%F).tar.gz data/
```

> ⚠️ **ARM 上的一个注意点**：`matplotlib 3.8.4` 在 aarch64 上可能拿不到预编译 wheel 而需本地编译，构建会慢（5–15 分钟）。Dockerfile 已预装 `build-essential`、`libfreetype6-dev`、`libpng-dev`，能自动完成编译。若仍失败，把 `requirements.txt` 里的 matplotlib 升到最新版即可。

> ⚠️ **闲置回收**：Oracle 可能回收 7 天内 CPU/网络/内存利用率均低于 20% 的实例。本项目有每日定时任务与轮询，正常不会触发。

---

## 方案 B：Fly.io（最快上线，约 $2–5/月）

仓库已附 `fly.toml`，无需改代码。

```bash
# 装 CLI
curl -L https://fly.io/install.sh | sh
fly auth login

# 建应用与持久卷（1GB 足够 SQLite 用很久）
fly apps create football-prediction-bot
fly volumes create bot_data --size 1 --region nrt

# 填密钥
fly secrets set TELEGRAM_TOKEN=xxx
fly secrets set API_FOOTBALL_KEY=xxx
fly secrets set CHAT_ID=xxx
fly secrets set ADMIN_ID=xxx

# 部署
fly deploy
```

要点：
- 无 `[[services]]` 配置 → 不会因无流量自动停机，天然常驻
- 卷挂载点 `/data` 与 `docker-entrypoint.sh` 一致
- 内存给到 512MB（matplotlib 画图较吃内存）

```bash
fly logs          # 看日志
fly status        # 看状态
fly ssh console   # 进容器排查
```

---

## 方案 C：Zeabur（中文界面，亚太延迟低）

- 连 GitHub 仓库自动识别 Dockerfile
- 免费档 **256MB 内存** —— matplotlib 画图可能 OOM，**建议直接用 Pro 档**（$5/月起）
- 需挂载持久卷到 `/data`，否则数据不保
- 香港/新加坡节点，亚太访问延迟 <50ms

---

## 部署后验收

不管选哪个平台，上线后统一这样验证：

1. Telegram 发 `/status` → 应回复版本号、赛季、下次推送时间、数据源套餐
2. 发 `/test` → 立即跑一次推送，失败会显示具体原因
3. 检查数据库落盘：容器内 `/data/football.db` 应存在且随运行增长
4. 等一次定时推送（默认每天 08:00 Asia/Shanghai）确认链路完整

**`/status` 有回复** = 启动链路正常。若报 403/429，那是 API-Football 的 Key 问题（未订阅或额度用尽），与部署平台无关，见 README 常见问题。

---

## 从 Railway 迁出的 checklist

- [ ] 在新平台配齐环境变量（对照 `.env.example`）
- [ ] 挂载持久卷到 `/data`
- [ ] 确认**没有**给服务设置 Cron（定时推送由程序内部 job_queue 完成）
- [ ] 部署后 `/status` + `/test` 验收
- [ ] 旧 Railway 服务停机，避免两个实例同时轮询同一个 Token（会 409 冲突）
