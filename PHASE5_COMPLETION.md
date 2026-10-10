# ⛔ 已作废：第五阶段完成交付（回测系统 + 数据分析）

> **本文已于 2026-10-11 作废，内容不再可信，请勿据此判断项目状态。**
> 保留文件仅为避免外部链接与历史引用失效。

## 为什么作废

本文宣称「第五阶段 5 项任务 100% 完成、质量 96/100」，但**其中 3 个被交付文件已全部从仓库删除**，另 2 个从未真正接入：

| 本文宣称交付 | 实际情况 |
|---|---|
| `web/backtesting_page.html`（Task 5.2） | **已删除**。零引用，首行注释「添加到 frontend.html」但该文件中「回测」命中 0——从未被合并 |
| `data_analysis.py`（Task 5.3） | **已删除**。零引用，`service.py` 从不导入；本文称「已集成到预测流程」与实测不符 |
| `feature_extractor.py`（Task 5.4） | **已删除**。同上；且无参构造时 `team_stats={}`，所有特征恒为 `0.0` |
| `web/api_backtest.py`（Task 5.5） | **已删除**。零引用 |
| `backtester.py` 准确率 55% | 原实现用 1 场硬编码 Team A/B、概率写死 `0.45/0.32/0.23`；现版本已改为委托 `BacktesterFixed` 走真实语料 |

## 虚报数字 vs 实测

| 本文数字 | 实测数字 |
|---|---|
| 总体准确率 **55%+** | 泊松 **50.99%** / 市场 **53.96%** |
| 主胜 58% | 见 `docs/MARKET_FUSION_EVAL.md` 分档结果 |
| 平局 **35%** | **0%**（模型与市场均 0 场预测平局） |
| 高置信度 **62%** | 分档校准区间见评测文档 |
| 客胜 52% | 见评测文档 |

「回测速度 380 场 < 10 秒」「内存 < 100MB」等性能指标无实测支撑，属估算。

## 当前真实状态

- 回测：`backtester.py` / `backtester_with_db.py` 已委托 `BacktesterFixed`，走 1140 场真实语料；`BacktesterFixed` 另接 `PredictionRepository` + 滚动前进验证
- 回测结论持久化在 `backtest_runs` 表，重启不丢
- 以 `README.md` 与 `docs/MARKET_FUSION_EVAL.md` 为准
