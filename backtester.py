"""回测兼容层 —— 委托到真实链路。

历史遗留风险（本次修正的原因）
------------------------------
本文件原本是**假数据回测**，两个致命问题叠加：

  1. ``load_historical_fixtures()`` 返回 1 场硬编码比赛（"Team A" vs "Team B"）；
  2. ``predict_fixture()`` 返回硬编码概率 ``0.45 / 0.32 / 0.23``，与比赛无关。

后果是它能跑通、还能打印出"回测完成: 100.00%"。这比直接报错危险得多——
**看起来像成功的假数字，会被当成模型有效的证据**。

现改为委托 ``BacktesterFixed``（真实数据源 + 滚动前进评估）：
保留 ``Backtester`` 类名、async 接口与返回结构，历史调用方无需改动即可
拿到真实数字。

关于被删除的 ``predict_fixture()``：
    单场预测在滚动前进里无法独立成立——第 i 场的强度必须来自第 i 场
    **之前**的数据，脱离上下文单算一场就是前视偏差。因此该接口不再提供，
    评估统一走 ``BacktesterFixed.backtest()``。留着它只会重新引入假数字。
"""

from __future__ import annotations

import logging
from typing import Any

import paths
from backtest import WalkForwardBacktester, compare
from backtester_fixed import BacktesterFixed
from football_data import LEAGUE_ID_TO_CODE

log = logging.getLogger(__name__)


class Backtester:
    """回测器（兼容层）：数据源与评估均委托真实链路。"""

    def __init__(
        self,
        league_id: int = 39,
        season: int | None = None,
        db_path: str | None = None,
        variant: str = "poisson",
    ) -> None:
        """
        :param season: None（默认）= 全部可用赛季。滚动前进依赖历史积累，
            只取单赛季会让预热期占比上升、结论变不稳；明确只想看单赛季时才传。
        """
        self.league_id = int(league_id)
        self.season = season
        self.db_path = db_path or str(paths.DB_PATH)
        self.variant = variant
        # 复用已接好数据源的实现，避免两套逻辑各自漂移
        self._impl = BacktesterFixed(
            league_id=self.league_id, season=self.season,
            db_path=self.db_path, variant=self.variant,
        )
        self.results: dict[str, Any] = {
            "total_predictions": 0,
            "correct_predictions": 0,
            "predictions": [],
            "accuracy": 0.0,
            "accuracy_by_result_type": {},
        }

    # ---- 数据源 ------------------------------------------------------------
    async def load_historical_fixtures(self) -> list[dict]:
        """加载真实历史赛事（库内赛果 ∪ 内置 CSV 语料）。"""
        matches = await self._impl.load_historical_fixtures()
        if not matches:
            log.warning("无历史数据，回测无法进行")
        return matches

    # ---- 回测 --------------------------------------------------------------
    async def backtest(self) -> dict[str, Any]:
        """运行回测，返回结构与改造前一致，但数字是真实计算出来的。"""
        matches = await self.load_historical_fixtures()
        if not matches:
            log.warning("无历史数据,回测失败")
            return self.results

        result = await self._impl.backtest()

        # fixture_id → 队名/比分，用于还原逐场明细的可读字段
        index = {str(m.get("fixture_id")): m for m in matches}

        predictions = []
        for rec in result["predictions"]:
            m = index.get(str(rec.get("fixture_id")), {})
            predictions.append({
                "fixture_id": rec.get("fixture_id"),
                "date": (m.get("utc_date") or "")[:10],
                "match": f"{m.get('home_team_name', '?')} vs {m.get('away_team_name', '?')}",
                "actual_score": f"{m.get('home_score')}-{m.get('away_score')}",
                "predicted_result": rec.get("predicted"),
                "actual_result": rec.get("actual"),
                "is_correct": bool(rec.get("hit")),
                "confidence": rec.get("prob"),
                "level": rec.get("level"),
            })

        self.results = {
            "total_predictions": result["total_predictions"],
            "correct_predictions": result["correct_predictions"],
            "predictions": predictions,
            "accuracy": result["accuracy"],
            "accuracy_by_result_type": self._accuracy_by_type(predictions),
            # 主判据：准确率太粗，以对数损失 / RPS 为准
            "log_loss": result["log_loss"],
            "rps": result["rps"],
            "brier": result["brier"],
            "ece": result["ece"],
            "verdict": result["verdict"],
        }

        log.info("回测完成: %d/%d (%.2f%%)，log_loss=%.4f",
                 self.results["correct_predictions"],
                 self.results["total_predictions"],
                 self.results["accuracy"] * 100,
                 self.results["log_loss"] or 0.0)
        return self.results

    @staticmethod
    def _accuracy_by_type(predictions: list[dict]) -> dict[str, dict]:
        """按真实结果类型（home/draw/away）统计准确率。"""
        by_type: dict[str, dict] = {}
        for result_type in ("home", "draw", "away"):
            rows = [p for p in predictions if p["actual_result"] == result_type]
            if not rows:
                continue
            correct = sum(1 for p in rows if p["is_correct"])
            by_type[result_type] = {
                "total": len(rows),
                "correct": correct,
                "accuracy": correct / len(rows),
            }
        return by_type

    # ---- 汇总 --------------------------------------------------------------
    def get_summary(self) -> dict:
        """获取回测总结。"""
        return {
            "season": self.season,
            "league_id": self.league_id,
            "total_predictions": self.results["total_predictions"],
            "correct_predictions": self.results["correct_predictions"],
            "accuracy": f"{self.results['accuracy']:.2%}",
            "log_loss": self.results.get("log_loss"),
            "verdict": self.results.get("verdict"),
            "accuracy_by_result_type": {
                k: f"{v['accuracy']:.2%}"
                for k, v in self.results["accuracy_by_result_type"].items()
            },
        }

    def get_confidence_analysis(self) -> dict:
        """按信心等级统计命中率——等级若无效，'高信心'就只是随机标签。"""
        buckets: dict[str, dict] = {}
        for p in self.results["predictions"]:
            key = p.get("level") or "unknown"
            slot = buckets.setdefault(key, {"n": 0, "hit": 0})
            slot["n"] += 1
            slot["hit"] += 1 if p["is_correct"] else 0
        return {
            k: {"total": v["n"], "correct": v["hit"],
                "accuracy": v["hit"] / v["n"] if v["n"] else None}
            for k, v in buckets.items()
        }


__all__ = ["Backtester", "WalkForwardBacktester", "compare", "LEAGUE_ID_TO_CODE"]
