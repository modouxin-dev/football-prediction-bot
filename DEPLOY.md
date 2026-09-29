# 部署指南（Railway 之外的落地方案）

本项目是**长轮询常驻进程**，不监听端口。这决定了它对托管平台有三个硬性要求：

| 要求 | 原因 | 不满足的后果 |
|---|---|---|
| 常驻不休眠 | 轮询进程停了就收不到消息 | 机器人下线，命令无响应 |
| **`/data` 必须真实持久化** | SQLite 存预测记录与赛后回写 | 重部署后历史与命中率统计清零 |
| 能直连 `api.telegram.org` | 长轮询要持续访问 Telegram | 国内云直连通率不足 40%，机器人反复掉线 |

> ⚠️ 第三条是隐性门槛：国内云（阿里云/腾讯云等）不适合直接部署，除非额外配代理中转。

---

## 数据持久化：为什么它决定了平台选择

机器人把**每条预测**写入 `predictions` 表，赛后回写真实比分（`settle`），并据此统计命中率。**这些数据只存在于 `/data/football.db` 这一个文件里**——卷没了，历史就全没了。

实测结论（模拟进程重启，全新实例从磁盘读回）：

```
[运行 1] 写 3 条预测 → 回评 2 场 → total=2 hit=1 rate=0.5
[重启]   销毁所有进程内对象
[运行 2] 全新实例读取 → total=2 hit=1 rate=0.5，待回评 1 场
[运行 2 续] 继续结算遗留场次 → total=3 hit=1 rate=0.33
✅ 历史与命中率跨重启完整保留，并可继续累积
```

**代码层没问题，风险全在部署层**。三个必须注意的点：

1. **挂载整个 `/data` 目录，不是单个 db 文件**。数据库是 WAL 模式，会生成 `football.db-wal`、`football.db-shm`，必须与主库一起持久化，否则可能丢最近未合并的写入。
2. **bind mount 比 named volume 更安全**。本项目的 `docker-compose.yml` 用 `./data:/data`，`docker compose down` 不会删除数据；只有手动删宿主机目录才会丢。
3. **不要选不支持卷的平台**（如 Koyeb 免费档），否则每次部署都是一次清零。

### 备份

`backup_db.sh` 提供一致性热备（`sqlite3 .backup`，无 CLI 时降级为 `VACUUM INTO`），可在服务运行时安全执行：

```bash
# 手动
./backup_db.sh                     # 备份到 /data/backups，保留最近 14 份
DB_PATH=/data/football.db KEEP=30 ./backup_db.sh /path/to/dir

# 每日自动（宿主机 cron）
0 4 * * * cd /path/to/repo && ./backup_db.sh ./data/backups >> ./data/backup.log 2>&1

# 或用 compose 的备份服务
docker compose --profile backup up -d
```

已实测：备份文件可正常打开、数据完整，保留份数策略生效（连续跑 5 次 + KEEP=3 → 只留 3 份）。

> 仅挂载卷仍不够稳妥——宿主机磁盘故障会一并丢失。**建议定期把备份同步到别处**（Oracle 有 20GB 免费对象存储，其他平台可用 `rclone` 传到任意对象存储）。

---

## 方案对比（2026 现状）

| 方案 | 成本 | 常驻 | 持久化 | 直连 TG | 门槛 |
|---|---|---|---|---|---|
| **Oracle Cloud 免费 ARM** | ¥0 永久 | ✅ | ✅ 200G 块存储 | ✅ | 需信用卡验证；A1 容量要抢 |
| **Fly.io** | ~$2–5/月 | ✅ | ✅ 持久卷 | ✅ | 需绑卡，按量计费 |
| **Zeabur** | $5 额度/月 | ✅ | ⚠️ 需付费档 | ✅ | 免费档仅 256MB，有 OOM 风险 |
| Koyeb | 免费 1 个服务 | ✅ | ❌ 免费档不支持卷 | ✅ | ❌ 排除：数据无法持久化 |
| Render | 免费档 | ❌ 15 分钟休眠 | ❌ | ✅ | ❌ 排除：会休眠 |
| 国内云服务器 | ¥10–30/月 | ✅ | ✅ | ❌ 需代理 | ❌ 排除：网络不通 |

**结论**：要长期零成本 → Oracle；要最快上线、能接受小额付费 → Fly.io。两者都能满足"重启不清零"。

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
3. 发 **`/storage`** → 存储自检：写入/读取/数据库/挂载/最后写入时间五项
4. 检查数据库落盘：容器内 `/data/football.db` 应存在且随运行增长
5. 等一次定时推送（默认每天 08:00 Asia/Shanghai）确认链路完整

**`/status` 有回复** = 启动链路正常。若报 403/429，那是 API-Football 的 Key 问题（未订阅或额度用尽），与部署平台无关，见 README 常见问题。

### 验证"重启不清零"（关键一步）

持久化到底生效没有，**只靠看文件存在是不够的**，必须真的重启一次：

```bash
# 1) 部署后先记下当前统计
docker compose exec bot python -c "
from repository import PredictionRepository as R
print(R('/data/football.db').stats()['total'], '场已结算')
"

# 2) 彻底重建容器（模拟重启 + 重新部署）
docker compose down
docker compose up -d --build

# 3) 再查一次：数字应当 ≥ 上次，而不是回到 0
docker compose exec bot python -c "
from repository import PredictionRepository as R
print(R('/data/football.db').stats()['total'], '场已结算')
"
```

同理，用 `/storage` 也能验：部署后跑一次，重新部署后再跑一次——**第二次仍能看到第一次的标记文件及其时间**，即证明卷生效（这是该命令的设计用途）。

> 如果第 3 步数字回到 0，说明 `/data` 没挂上真实持久卷，历史数据只在容器可写层里，重建即丢。此时请检查平台的卷配置。

---

## 从 Railway 迁出的 checklist

- [ ] 在新平台配齐环境变量（对照 `.env.example`）
- [ ] 挂载持久卷到 `/data`（**整个目录**，不能只挂 db 文件）
- [ ] 确认**没有**给服务设置 Cron（定时推送由程序内部 job_queue 完成）
- [ ] 部署后 `/status` + `/test` + `/storage` 验收
- [ ] **重建容器后复查统计数字未清零**（见上一步）
- [ ] 配好定期备份（cron 或 `--profile backup`）
- [ ] 旧 Railway 服务停机，避免两个实例同时轮询同一个 Token（会 409 冲突）
