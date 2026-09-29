# Football Prediction Bot — 代码质量审查

审查对象：`modouxin-dev/football-prediction-bot` main 分支（23 commits）
源码 6050 行（12 个 .py）+ 测试 4597 行（19 个测试文件）

---

## 一、致命问题（必须修，否则跑不起来）

### P0-1 `config.py:220` NameError —— 启动必崩

```python
config.py:194    DATABASE_FILE = DB_PATH      # 赋给了这个名字
config.py:220    db_path=db_path,             # 用的是另一个未定义的名字
```

`load_settings()` 是 `main.py:1213` 启动路径的必经调用，执行到第 220 行抛
`NameError: name 'db_path' is not defined`。任何部署方式（Railway / Fly.io /
docker-compose / 本地）都会崩在启动阶段。

修法：把第 194 行改成 `db_path = DB_PATH`，或第 220 行改成 `db_path=DB_PATH`。
建议前者，顺带删掉无用的 `import paths`（第 11 行 `paths` 导入后未使用）。

为什么一直没被发现：`tests/test_config.py` 有多处 `load_settings(BASE)` 调用，
但这些测试在当前 CI 里根本没有被执行——见 P1-1。

---

## 二、CI 问题（CI 红 + 形同虚设）

### P1-1 两个 workflow 都在部署 GitHub Pages，没有任何一个跑 pytest

- `.github/workflows/deploy.yml` 与 `.github/workflows/static.yml` 内容高度重复
- 两者的 `concurrency.group` 都是 `"pages"`，互相抢占，其中一个必定被跳过/失败
- `deploy.yml` 用 `path: .` 上传**整个仓库**（含源码、.git）到 Pages，没有任何静态站点内容，产物无意义
- **全仓库没有一个 job 执行 `pytest`**，4597 行测试完全没在 CI 里跑过

这直接解释了 README 页面上那个 failure 状态，也解释了 P0-1 为什么能活到 main。

建议：
1. 删掉 `deploy.yml`（或删 `static.yml`），只保留一个 Pages workflow；若不需要 Pages，两个都删
2. 新增 `.github/workflows/ci.yml`：`pip install -r requirements-dev.txt` → `pytest`
3. 若确需 Pages，把 `path` 指向真实静态目录（如 `site/` 或 `docs/`）

---

## 三、静态检查问题（pyflakes，13 处）

| 文件:行 | 问题 | 严重度 |
|---|---|---|
| config.py:220 | undefined name `db_path` | 致命 |
| config.py:11 | `import paths` 未使用 | 低 |
| config.py:194 | `DATABASE_FILE` 赋值后未使用 | 低 |
| data_source.py:23 | `FootballDataError` 导入未使用 | 低 |
| data_source.py:24 | `is_fallback_id` 导入未使用 | 低 |
| data_source.py:130 | `optional` 赋值后未使用 | 低 |
| football_data.py:175 | f-string 缺少占位符 | 低 |
| main.py:35 | `DataSourceError` 导入未使用 | 低 |
| main.py:40 | `MODE_UPCOMING` 导入未使用 | 低 |
| main.py:391 | `tz` 赋值后未使用 | 低 |
| main.py:1094 | 重复定义 `datetime`，覆盖第 16 行导入 | 中 |
| verify_deploy.py:14 | `json` 导入未使用 | 低 |
| （另有 6 处 `except ... pass` 吞异常） | 见 P3-3 | 中 |

`main.py:1094` 的函数内 `from datetime import datetime` 会遮蔽模块级同名导入，
若该文件后面还有代码依赖模块级的 `datetime`，行为会随调用顺序变化，建议改名或移除。

---

## 四、结构与可维护性

### P3-1 文件偏大

| 文件 | 行数 |
|---|---|
| main.py | 1236 |
| bot_handler.py | 1100 |
| service.py | 798 |
| repository.py | 515 |
| chart.py | 474 |

`main.py` 同时承担命令注册、按钮回调、定时任务、日志配置，
`bot_handler.py` 1100 行几乎全是消息模板字符串。建议按职责拆：
`handlers/`（命令与回调）、`formatters/`（模板）、`scheduler.py`。

### P3-2 超长函数（> 60 行，共 12 个）

重点关注：

- `service.py:450 query_fixtures()` 136 行 —— 最长的函数，混合了查询、过滤、分页
- `main.py:751 on_chart()` 105 行
- `bot_handler.py:1004 format_fixtures_page()` 93 行
- `bot_handler.py:478 format_prediction_card()` 83 行
- `chart.py:226 match_card()` 83 行
- `config.py:148 load_settings()` 74 行

`query_fixtures` 和 `load_settings` 建议优先拆分，前者每加一个筛选条件就更难改，
后者是配置入口，出错影响全局。

### P3-3 6 处 `except ... pass` 静默吞异常

异常被吞掉后，线上出问题（数据源超时、解析失败）在日志里完全无痕，
排查时只能靠猜。至少改成 `log.warning(...)`。

---

## 五、做得好的地方

- **无 SQL 注入风险**：`repository.py` 全部使用参数化查询，未发现字符串拼接 SQL
- **测试量充足**：4597 行测试对 6050 行源码，且 `tests/` 覆盖到 config、analyzer、
  api_client、repository、service、menu、chart 等各层，还带 `sample_data.py` 做仿真响应
- **配置集中且容错友好**：`config.py` 统一解析校验，缺变量时抛带明确变量名的
  `ConfigError`；`PUSH_TIME` 兼容 `08:00 / 8 / 0800` 多种写法；`SEASON` 过期会自动降级
- **路径自动回退**：`paths.py` 对每个目录做写探针，不可写时回退到 `/tmp`，
  避免挂载卷缺失直接崩
- **数据源有备用**：主源 API-Football 失败可切 football-data.org，可关可配
- **依赖声明干净**：`requirements.txt` 5 个依赖与实际 import 完全吻合，无冗余、无缺失
- **README 与 DEPLOY.md 完整**，含 403/429/套餐不支持等常见故障的排查说明

---

## 六、修复优先级

| 优先级 | 事项 | 工作量 |
|---|---|---|
| P0 | 修 `config.py` 的 `db_path` NameError | 1 行 |
| P1 | 加 pytest CI，删重复的 Pages workflow | 小 |
| P2 | 清理 12 处静态检查告警 + 6 处吞异常 | 小 |
| P3 | 拆分 `main.py` / `bot_handler.py`，拆 `query_fixtures()` | 中 |
