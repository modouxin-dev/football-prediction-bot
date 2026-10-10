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
    ) -> None:
        """
        :param season: None（默认）= 全部可用赛季；滚动前进需要历史积累。
        """
        self.league_id = int(league_id)
        self.season = season
        self.db_path = db_path or str(paths.DB_PATH)
        self.variant = variant
        self._impl = BacktesterFixed(
            league_id=self.league_id, season=self.season,
            db_path=self.db_path, variant=self.variant,
        )
        self.results: dict[str, Any] = {
            "total_predictions": 0,
            "correct_predictions": 0,
            "accuracy": 0.0,
            "predictions": [],
            "accuracy_by_type": {},
        }

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
            "predictions": predictions,
            "accuracy_by_type": self._accuracy_by_type(predictions),
            "log_loss": result["log_loss"],
            "rps": result["rps"],
            "brier": result["brier"],
            "ece": result["ece"],
            "verdict": result["verdict"],
            "corpus": result["corpus"],
        }
        return self.results

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
