# CI 与分支保护

## 改了什么

| 文件 | 变更 |
|---|---|
| `.github/workflows/ci.yml` | **新增**：跑测试 + 启动冒烟 |
| `.github/workflows/deploy.yml` | **删除**：与 static.yml 重复的 Pages 部署 |
| `.github/workflows/static.yml` | 上传范围从 `.`（整个仓库）改为 `site/` |
| `site/index.html` | **新增**：Pages 实际可访问的页面 |

### 为什么删 deploy.yml

原来 `deploy.yml` 与 `static.yml` **都在部署 GitHub Pages**，且 `static.yml` 的
concurrency group 是 `"pages"`。两者同时触发时互相抢占，其中一个必定失败——
这就是 README 页上那个 failure 状态的来源。

### 为什么改上传范围

原配置 `path: '.'` 会把**整个仓库**（含源码、配置）发布到 Pages，
而仓库里根本没有静态页面，产物无人可访问，却把源码全公开了。
现在只发布 `site/` 目录。

## concurrency 组已分开

| workflow | group | cancel-in-progress |
|---|---|---|
| `ci.yml` | `ci-${{ github.ref }}` | `true`（新推送取消旧测试，省时长） |
| `static.yml` | `pages` | `false`（部署不中断） |

## 测试内容

- Python **3.10 与 3.12** 双版本矩阵（`fail-fast: false`，任一失败都能看到）
- `pip install -r requirements-dev.txt` → `python -m pytest -q`
- 启动冒烟：导入 `main`/`paths`/`config` 并加载配置，断言 `db_path` 非空。
  这一步专门用来拦截「配置层改动导致部署后必崩」——本项目此前正是栽在这类问题上

测试不联网，用 `tests/sample_data.py` 的仿真 API 响应。

## ⚠️ 必须知道：GitHub Actions 拦不住 Railway

这是配置分支保护前要理解的**关键限制**：

- Railway 自己监听 `main` 分支并构建，**不会**等待 GitHub Actions 的检查结果
- 也就是说，即使 CI 红了，Railway 仍会把坏代码部署上去

因此「每次 Push 必须通过测试才能触发部署」这句话，对 Railway 只能这样实现：

**用分支保护把坏代码挡在 main 之外** —— 代码进不了 main，Railway 就拉不到。

### 配置步骤

1. 仓库 **Settings → Branches → Add branch ruleset**（或 Add classic branch rule）
2. Branch name pattern：`main`
3. 勾选 **Require status checks to pass**
4. 在检查列表里勾选 `test (py3.10)` 与 `test (py3.12)`
5. 建议同时勾选 **Require branches to be up to date before merging**
6. 保存

配好之后，PR 必须等两个版本的测试全绿才能合并，合并后 Railway 才会部署。

> 如果习惯直接 push 到 main（不经 PR），分支保护的 status check 仍会运行，
> 但不会阻止 push。要真正拦住，需要开 **Require a pull request before merging**。

## 本地复现 CI

```bash
pip install -r requirements-dev.txt
python -m pytest -q

# 等价于 CI 里的启动冒烟
TELEGRAM_TOKEN=1:fake RAPID_API_KEY=fake DATA_DIR=/tmp/smoke \
  python -c "import main, config; s=config.load_settings(); assert s.db_path; print(s.db_path)"
```

## 每阶段 DoD（Definition of Done）

按协作约定，每个改动阶段的完成标准：

1. 代码改动有对应测试
2. `pytest` 全绿（本机 + CI 双版本）
3. **README 同步更新**（命令、环境变量、项目结构）
4. 配置项改动同步 `.env.example`
5. 涉及存储的改动，明确 Volume 挂载要求
