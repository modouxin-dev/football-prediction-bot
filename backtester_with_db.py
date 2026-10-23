"""带数据库的回测器（兼容层）—— 委托到真实链路。

历史遗留风险（本次修正的原因）
------------------------------
这个文件的欺骗性比 ``backtester.py`` 更强，因为**函数名都在说谎**：

  * ``load_from_db()`` 叫"从数据库加载真实数据"，实际返回 3 场硬编码
    ``Team A / Team C / Team E`` 假比赛；
  * ``predict_result()`` 叫"简单泊松预测"，实际 ``home_avg``/``away_avg``
    是写死的常量，任何对阵都恒返回 ``"home"``。

跑出来是 "准确率 33.33%"，而 ``integrated_backtest_test.py`` 会据此打印
"✅ 回测系统: 通过"——**用 3 场假比赛和一个恒定预测，得出了一个通过结论**。

现改为委托 ``BacktesterFixed``：保留 ``BacktesterWithDB`` 类名与
``run_backtest()`` 的返回结构，但数据源是真库真语料，评估是真滚动前进。
"""

from __future__ import annotations

import logging
from typing import Any

import paths
from backtester_fixed import BacktesterFixed

log = logging.getLogger(__name__)


class BacktesterWithDB:
    """回测器（兼容层）：数据源与评估均委托真实链路。"""

    def __init__(
        self,
        league_id: int = 39,
        season: int | None = None,
        db_path: str | None = None,
        variant: str = "poisson",
        min_history: int | None = None,
        market_weight: float | None = None,
    ) -> None:
        """
        :param season: None（默认）= 全部可用赛季；滚动前进需要历史积累。
        :param min_history: 预热场次数；None = 交给 BacktesterFixed 取默认 30。
        :param market_weight: 市场锚定融合权重 w（0~1）。

        ⚠️ 口径对齐（迁移时补上的关键参数）：
        线上推送是 ``w·市场 + (1−w)·模型`` 的融合口径，**不是纯泊松**。
        本层原先不接受 ``market_weight``，若定时回测改走本类而不透传它，
        回测会静默退化成纯泊松 —— 分数代表不了线上真实表现，
        且与 ``backtest_runs`` 表里历史记录不再可比。
        因此这里必须显式透传给 ``BacktesterFixed``。
        """
        self.league_id = int(league_id)
        self.season = season
        self.db_path = db_path or str(paths.DB_PATH)
        self.variant = variant
        self._impl = BacktesterFixed(
            league_id=self.league_id, season=self.season,
            db_path=self.db_path, variant=self.variant,
            min_history=min_history, market_weight=market_weight,
        )
        self.results: dict[str, Any] = {
            "total_predictions": 0,
            "correct_predictions": 0,
            "accuracy": 0.0,
            "predictions": [],
            "accuracy_by_type": {},
        }

    # ---- 属性代理 ----------------------------------------------------------
    # 调用方（scheduler / 看板）需要从回测器上直接读到这些元信息；
    # 不代理的话要么重复实现一份，要么调用方被迫穿透 _impl 拿私有属性。
    @property
    def competition_code(self) -> str:
        """联赛 ID → 竞赛代码（委托实现，未知联赛照旧报错）。"""
        return self._impl.competition_code

    @property
    def min_history(self) -> int:
        """预热场次数（真实生效值，不是 None）。"""
        return self._impl.min_history

    @property
    def market_weight(self) -> float | None:
        """市场融合权重；None = 纯泊松口径。"""
        return self._impl.market_weight

    # ---- 数据源 ------------------------------------------------------------
    async def load_from_db(self) -> list[dict]:
        """真实加载：库内赛果 ∪ 内置 CSV 语料，按时间正序。"""
        matches = await self._impl.load_historical_fixtures()
        if not matches:
            log.warning("库中无已完赛数据")
        return matches

    # ---- 回测 --------------------------------------------------------------
    async def run_backtest(self) -> dict[str, Any]:
        """运行回测。返回结构不变，数字改为真实计算。"""
        matches = await self.load_from_db()
        if not matches:
            raise ValueError("无可用数据")

        result = await self._impl.backtest()
        index = {str(m.get("fixture_id")): m for m in matches}

        predictions = []
        for rec in result["predictions"]:
            m = index.get(str(rec.get("fixture_id")), {})
            predictions.append({
                "match": f"{m.get('home_team_name', '?')} vs {m.get('away_team_name', '?')}",
                "predicted": rec.get("predicted"),
                "actual": rec.get("actual"),
                "correct": bool(rec.get("hit")),
                "score": f"{m.get('home_score')}-{m.get('away_score')}",
                "level": rec.get("level"),
            })

        self.results = {
            "total_predictions": result["total_predictions"],
            "correct_predictions": result["correct_predictions"],
            "accuracy": result["accuracy"],
            # ↓ 与 accuracy 同批补回：精确比分口径若在这里丢掉，
            # backtest_runs 的 exact_rate 恒为 NULL，看板那列永远显示 "—"，
            # 于是又只剩「命中率」一个数字，口径混淆的问题原样复现。
            "exact_rate": result.get("exact_rate"),
            "exact_n": result.get("exact_n"),
            "exact_hits": result.get("exact_hits"),
            "predictions": predictions,
            "accuracy_by_type": self._accuracy_by_type(predictions),
            "log_loss": result["log_loss"],
            "rps": result["rps"],
            "brier": result["brier"],
            "ece": result["ece"],
            "verdict": result["verdict"],
            "corpus": result["corpus"],
            # ↓ 迁移补回：这些字段 BacktesterFixed 算了，但本层原先丢弃了。
            # save_backtest_run 会把 calibration / by_level 写进 backtest_runs，
            # 丢了它们 = 落库记录的分档校准恒为空，看板"模型是否校准"直接失明。
            "calibration": result.get("calibration"),
            "by_level": result.get("by_level"),
            "rho": result.get("rho"),
            "scheme": result.get("scheme"),
            "market_weight": result.get("market_weight"),
            "min_history": result.get("min_history"),
            "comparison": result.get("comparison"),
            "baseline_levels": result.get("baseline_levels"),
            "challenger_levels": result.get("challenger_levels"),
        }
        return self.results

    async def backtest(self) -> dict[str, Any]:
        """``run_backtest`` 的同义入口。

        定时链路（scheduler）与看板历史代码调用的是 ``backtest()``，
        兼容层如果只留 ``run_backtest()``，迁移时就得改所有调用点，
        或者调用方穿透到 ``_impl``。这里给一个薄别名，语义完全等价。
        """
        return await self.run_backtest()

    @staticmethod
    def _accuracy_by_type(predictions: list[dict]) -> dict[str, dict]:
        """按真实结果类型统计准确率。"""
        by_type: dict[str, dict] = {}
        for key in ("home", "draw", "away"):
            rows = [p for p in predictions if p["actual"] == key]
            if not rows:
                continue
            correct = sum(1 for p in rows if p["correct"])
            by_type[key] = {
                "total": len(rows),
                "correct": correct,
                "accuracy": correct / len(rows),
            }
        return by_type


__all__ = ["BacktesterWithDB"]
