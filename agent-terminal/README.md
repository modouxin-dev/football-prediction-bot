# agent-terminal：封闭式测试部署链路

原则：**Telegram 只下发受限任务，沙盒只碰白名单仓库，产物先进 PR，人工审核后再合并。**
不做任意文件外传。

## 链路

```text
Telegram 指令
    ↓  网关：chat_id 白名单 + 命令整串精确匹配
任务队列（固定任务名，不接受参数/路径/仓库）
    ↓
一次性沙盒容器（只读根 + tmpfs + cap_drop ALL + 非 root）
    ↓  只读克隆源仓库
测试 → 秘密扫描 → 打包
    ↓  推 staging 临时分支 sandbox/<RUN_ID>
GitHub PR（人工审核后合并）
```

## 目录

```text
agent-terminal/
├── docker-compose.yml      最小权限编排（read_only / tmpfs / cap_drop ALL）
├── deploy.sh               一键部署与自检
├── gateway/
│   ├── app.py              Webhook 网关（只校验与派发，不执行 shell）
│   └── allowlist.yaml      命令 / 仓库 / 出口域名 / 回执字段白名单
├── worker/
│   ├── Dockerfile          一次性沙盒镜像
│   ├── run_task.sh         固定流程：克隆 → 测试 → 扫描 → 打包 → 推 PR
│   └── ci/
│       ├── test.sh         pytest，只回传统计行
│       └── secret-scan.sh  凭据扫描，命中即失败
└── secrets/.gitignore      凭据目录不入库
```

## 环境变量

```text
GH_TOKEN              仅运行时注入，不写入 URL / 日志 / 产物
TG_ALLOWED_CHAT_IDS   逗号分隔的 chat_id，缺失则拒绝全部请求
TELEGRAM_TOKEN        Webhook 注册用（可选）
WEBHOOK_URL           HTTPS 地址（可选）
```

部署前自检（只校验存在性，不打印值）：

```bash
bash agent-terminal/deploy.sh
```

## 安全边界

- 未配置 `TG_ALLOWED_CHAT_IDS` 时拒绝一切请求（fail-closed）。
- 命令必须整串精确匹配：`/test --repo=evil` 一律丢弃。
- 源/目标仓库硬编码在 `run_task.sh`，不接受任何用户输入。
- 凭据只从环境变量读取，克隆与推送均用 `http.extraHeader`，不拼进 URL。
- 回执只含 `status / commit / tests / secret_scan / pr_url`，源码与路径不外传。
- 容器 `read_only` + `tmpfs` + `cap_drop: ALL` + `no-new-privileges`，用完即弃。

## 自测结论

| 项 | 结果 |
|---|---|
| 网关安全测试 | 8 passed |
| 秘密扫描（含假凭据） | 命中并 exit 1 |
| 秘密扫描（干净仓库） | clean，exit 0 |
| 完整测试套件 | 358 passed |
