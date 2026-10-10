"""修复版回测系统 —— 接入真实数据源。

历史背景与本次改造
------------------
本文件最初只有 84 行，两处致命缺陷：

  1. ``load_historical_fixtures()`` 里引用了未定义的 ``fixtures`` ——
     实际抛 ``NameError``，并不是"取不到数据"；
  2. ``_evaluate()`` 恒 ``return True`` —— 就算喂进数据，accuracy 也会恒为
     100%，回测结果毫无参考价值。

审计阶段的最小修复只把 ``fixtures`` 定义为空列表，那**不是修复**：它只是让
``NameError`` 变成"无数据"路径，回测依旧不可用。

本次改造把数据源接进仓库**已有**的成熟链路，不新造轮子：:

    league_id ──LEAGUE_ID_TO_CODE──▶ competition_code（39 → "PL"）
    PredictionRepository(db) ──load_corpus──▶ 库内赛果 ∪ 内置 CSV 语料
                                             （按同一场去重、按时间正序）
    WalkForwardBacktester.run(matches) ──▶ 真实指标（log_loss / RPS / Brier / ECE）

评估逻辑委托给 ``backtest.py``：预测第 i 场时**只用第 i 场之前**的数据。
用全部历史算出的强度去评价历史比赛属于前视偏差，会得到虚高的漂亮数字，
上线即失效——这类数字比没有数字更危险。

关于 ``season`` 参数：滚动前进需要历史积累，因此**默认取全部可用赛季**。
若显式指定某个赛季，样本会被裁剪，预热期占比上升、结论更不稳，仅在明确
只想看单赛季时使用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import paths
from backtest import (
    DEFAULT_MIN_HISTORY,
    WalkForwardBacktester,
    compare,
    level_monotonic,
)
from football_data import LEAGUE_ID_TO_CODE

log = logging.getLogger(__name__)


class BacktesterFixed:
    """回测引擎：真实数据源 + 滚动前进（walk-forward）评估。"""

    def __init__(
        self,
        league_id: int = 39,
        season: int | None = None,
        db_path: str | Path | None = None,
        variant: str = "poisson",
        min_history: int | None = None,
    ) -> None:
        """
        :param league_id: 联赛 ID，39=英超（默认）。见 LEAGUE_ID_TO_CODE。
        :param season:    只回测该赛季；None（默认）= 全部可用赛季。
        :param db_path:   SQLite 路径，默认取 paths.DB_PATH。
        :param variant:   "poisson"（默认，与线上口径一致）/ "elo" / "dc"。
        :param min_history: 预热场次数，少于此值不计入评估（默认 30）。
        """
        self.league_id = int(league_id)
        self.season = season
        self.db_path = str(db_path) if db_path is not None else str(paths.DB_PATH)
        self.variant = variant
        self.min_history = (
            DEFAULT_MIN_HISTORY if min_history is None else int(min_history)
        )
        self.matches: list[dict] = []
        self.corpus_info: dict[str, Any] = {}
        self.results: dict[str, Any] = {
            "total_predictions": 0,
            "correct_predictions": 0,
            "predictions": [],
            "accuracy": 0.0,
        }

    # ---- 数据源 ------------------------------------------------------------
    @property
    def competition_code(self) -> str:
        """联赛 ID → 竞赛代码。未知联赛直接报错，不猜。"""
        code = LEAGUE_ID_TO_CODE.get(self.league_id)
        if not code:
            raise ValueError(
                f"不支持的联赛 ID={self.league_id}，仅支持："
                + ", ".join(map(str, sorted(LEAGUE_ID_TO_CODE)))
            )
        return code

    async def load_historical_fixtures(self) -> list[dict]:
        """加载已完赛的历史赛事（库内赛果 ∪ 内置 CSV 语料）。

        返回按开赛时间正序的样本，字段与 ``backtest`` 模块要求一致。
        """
        # 延迟导入：repository 会打开 SQLite，放函数内避免导入本模块就建连接。
        from backtest_corpus import load_corpus
        from repository import PredictionRepository

        repo = PredictionRepository(self.db_path)
        # include_history=True：库内为空时仍能靠内置语料跑通（1140 场真实英超）。
        matches, info = load_corpus(repo, self.competition_code)
        self.corpus_info = info

        if self.season is not None:
            matches = [m for m in matches if m.get("season") == self.season]

        self.matches = matches
        if not matches:
            scope = f"{self.season} 赛季" if self.season is not None else "任何赛季"
            log.warning(
                "未找到 %s（联赛 ID=%s）%s 的已完赛数据：db=%s 语料统计=%s",
                self.competition_code, self.league_id, scope, self.db_path, info,
            )
            return []

        log.info("已加载 %d 场赛事数据（%s=%s，语料统计=%s）",
                 len(matches), self.competition_code, self.league_id, info)
        return matches

    # ---- 回测 --------------------------------------------------------------
    async def backtest(self) -> dict[str, Any]:
        """运行滚动前进回测，返回真实指标。

        无数据时仍抛 ``ValueError``（与改造前一致），但此时是"真的没数据"，
        而不是"代码没接数据源"。
        """
        matches = await self.load_historical_fixtures()
        if not matches:
            raise ValueError("无可用数据进行回测")

        use_elo = self.variant == "elo"
        use_dc = self.variant == "dc"

        # 基线恒为纯泊松：线上 service.py 调 predict_match() 不传 Elo，
        # 即线上跑的就是纯泊松，基线必须与之对齐才有对照意义。
        baseline = WalkForwardBacktester(
            min_history=self.min_history, use_elo=False, use_dc=False,
        ).run(matches)
        challenger_tester = WalkForwardBacktester(
            min_history=self.min_history, use_elo=use_elo, use_dc=use_dc,
        )
        challenger = challenger_tester.run(matches)
        comparison = compare(baseline, challenger)

        n = challenger.n
        hits = challenger.hits
        self.results = {
            "total_predictions": n,
            "correct_predictions": hits,
            # 逐场明细（含预测概率与真实结果），供二次分析
            "predictions": challenger.records,
            "accuracy": challenger.accuracy_rate or 0.0,
        }

        out = dict(self.results)
        out.update({
            # 主判据：准确率太粗，以对数损失 / RPS 为主
            "log_loss": challenger.log_loss,
            "rps": challenger.rps_score,
            "brier": challenger.brier_score,
            "ece": challenger.ece,
            "calibration": challenger.calibration,
            "by_level": challenger.to_dict()["by_level"],
            # 与纯泊松基线的对比结论
            "comparison": comparison,
            "verdict": comparison["verdict"],
            "baseline_levels": level_monotonic(baseline),
            "challenger_levels": level_monotonic(challenger),
            "rho": challenger_tester.rho if use_dc else None,
            "corpus": self.corpus_info,
            "variant": self.variant,
            "min_history": self.min_history,
        })
        return out
