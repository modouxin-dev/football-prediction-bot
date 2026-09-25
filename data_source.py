"""统一数据服务：主源 API-Football + 备用源 football-data.org 的切换调度。

Telegram Handler 和业务代码只依赖本模块暴露的方法，不直接调用具体数据源，
因此「换源」对上层是透明的。

切换规则（DATA_SOURCE_MODE=auto）：
  1. 先请求主源 API-Football；
  2. 成功且返回有效数据 → 使用主源；
  3. 主源出现 401/403/429、errors 非空、响应为空、赛季不可用 → 自动切备用源；
  4. 备用源也失败 → 抛出明确错误（两个源的原因都保留，但不泄露任何 Token）；
  5. 绝不返回伪造数据。

主源连续失败会进入冷却（默认 10 分钟），冷却期内直接走备用源，
避免浪费 API-Football 免费层宝贵的 100 次/天额度。
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from football_data import FootballDataAPI, FootballDataError
from normalize import is_fallback_id

log = logging.getLogger(__name__)

PRIMARY_COOLDOWN = 10 * 60  # 主源失败后，冷却多久内直接走备用源

# 可选数据：免费层天然可能没有（无赔率、无交锋、无近期战绩），
# 取不到时返回空是正常降级，不能当成「两个源都失败」抛出错误。
OPTIONAL_METHODS = {"get_odds", "get_h2h", "get_team_form"}


class DataSourceError(RuntimeError):
    """主源与备用源均不可用。消息中不含任何 Token。"""


class DataSourceRouter:
    """按规则在主/备数据源之间调度，对外暴露与 FootballAPI 一致的接口。"""

    def __init__(
        self,
        primary: Any,
        fallback: FootballDataAPI | None = None,
        *,
        mode: str = "auto",
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.mode = (mode or "auto").lower()
        self._source = "api-football"  # 当前实际使用的数据源
        self._primary_down_until = 0.0
        self._last_errors: dict[str, str] = {}  # 各源最近一次失败原因（用于提示）
        self.last_switch: dict | None = None     # 最近一次降级：源/方法/原因/时间

    # ---- 状态 ---------------------------------------------------------------
    @property
    def source(self) -> str:
        """当前实际使用的数据源标识。"""
        return self._source

    @property
    def source_label(self) -> str:
        return "API-Football" if self._source == "api-football" else "football-data.org"

    @property
    def using_fallback(self) -> bool:
        return self._source == "football-data"

    @property
    def fallback_available(self) -> bool:
        return self.fallback is not None and self.mode in ("auto", "football-data")

    def last_error(self, source: str) -> str | None:
        return self._last_errors.get(source)

    def _fallback_blocked(self) -> bool:
        return not self.fallback_available

    def _mark_primary_down(self, reason: str, method: str = "") -> None:
        self._primary_down_until = time.monotonic() + PRIMARY_COOLDOWN
        self._last_errors["api-football"] = str(reason)[:200]
        # 记录降级事件：为什么切、什么时候切的
        self.last_switch = {
            "from": "api-football",
            "to": "football-data",
            "method": method,
            "reason": str(reason)[:200],
            "at": time.time(),
        }

    def _primary_cooling(self) -> bool:
        return time.monotonic() < self._primary_down_until

    # ---- 请求日志 ------------------------------------------------------------
    def _log_request(self, source: str, method: str, outcome: str,
                     *, count: int | None = None, elapsed: float,
                     league: Any = None, day: Any = None, status: str = "ok") -> None:
        """每次请求记一条结构化日志：数据源、方法、联赛、日期、状态、返回数量、耗时。

        用户侧只看到友好提示；技术细节完整保留在日志里，便于事后排查。
        """
        log.info(
            "请求｜源=%s 方法=%s 联赛=%s 日期=%s 状态=%s 数量=%s 耗时=%.0fms",
            source, method,
            league if league is not None else "-",
            day if day is not None else "-",
            status, "null" if count is None else count, elapsed * 1000,
        )
        if outcome != "ok":
            log.debug("请求详情｜源=%s 方法=%s 结果=%s", source, method, outcome)

    def _context_kwargs(self, method: str, args: tuple) -> dict:
        """从调用参数里提取联赛与日期，用于日志上下文（取不到就记 '-'）。"""
        ctx: dict = {}
        if method in ("get_fixtures", "get_standings") and args:
            ctx["league"] = args[0]
        if method == "get_fixtures" and len(args) >= 4:
            ctx["day"] = f"{args[2]}~{args[3]}"
        return ctx

    # ---- 调度核心 ------------------------------------------------------------
    async def _run(self, method: str, *args, **kwargs) -> Any:
        """依次尝试主源、备用源；空数据视为需要切换的信号（可选数据除外）。

        可选数据（赔率/交锋/近期战绩）在两个源都没有时返回空列表，
        由上层显示「暂无赔率」等提示，而不是中断整条预测流程。
        """
        optional = method in OPTIONAL_METHODS
        tried_primary = False
        ctx = self._context_kwargs(method, args)
        if self.mode in ("auto", "api-football") and not self._primary_cooling():
            tried_primary = True
            started = time.monotonic()
            try:
                result = await getattr(self.primary, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 主源任何异常都触发切换
                self._log_request("api-football", method, "failed", elapsed=time.monotonic() - started,
                                  status=type(exc).__name__, **ctx)
                log.warning("主数据源 %s 失败（%s），准备切换备用源", method, type(exc).__name__)
                self._mark_primary_down(exc, method)
            else:
                if result:  # 有数据才认为主源可用
                    self._source = "api-football"
                    self._last_errors.pop("api-football", None)
                    self._log_request("api-football", method, "ok",
                                      count=len(result) if hasattr(result, "__len__") else None,
                                      elapsed=time.monotonic() - started, **ctx)
                    return result
                self._log_request("api-football", method, "empty",
                                  count=0, elapsed=time.monotonic() - started, **ctx)
                log.warning("主数据源 %s 返回空数据，尝试备用源", method)

        if self._fallback_blocked():
            if tried_primary:
                raise DataSourceError(
                    f"主数据源失败：{self.last_error('api-football') or '未知原因'}；"
                    "且未配置可用的备用数据源（需要 FOOTBALL_DATA_API_TOKEN）。"
                )
            raise DataSourceError("未配置可用的数据源。")

        started = time.monotonic()
        try:
            result = await getattr(self.fallback, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._log_request("football-data", method, "failed", elapsed=time.monotonic() - started,
                              status=type(exc).__name__, **ctx)
            self._last_errors["football-data"] = str(exc)[:200]
            primary_reason = self.last_error("api-football")
            raise DataSourceError(
                "主数据源与备用数据源均不可用。\n"
                f"主源：{primary_reason or '未尝试或已恢复'}\n"
                f"备用源：{str(exc)[:200]}"
            ) from None

        # 备用源请求成功（未抛异常）
        self._source = "football-data"
        self._last_errors.pop("football-data", None)
        count = len(result) if hasattr(result, "__len__") else None
        self._log_request("football-data", method, "ok" if result else "empty",
                          count=count, elapsed=time.monotonic() - started,
                          status="empty" if not result else "ok", **ctx)
        if not result:
            # 备用源正常响应但无数据 = 该时段确实没有比赛/数据，不是故障。
            # 主源的失败原因仍保留在 _last_errors 中，供上层提示（如赛季不可用）。
            log.info("备用数据源 %s 返回空数据（该时段确实没有数据）", method)
        return result

    # ---- 对外接口（与 FootballAPI 同名） ---------------------------------------
    async def get_fixtures(self, league_id: int, season: int, date_from: date, date_to: date) -> list[dict]:
        return await self._run("get_fixtures", league_id, season, date_from, date_to)

    async def get_standings(self, league_id: int, season: int) -> list[dict]:
        return await self._run("get_standings", league_id, season)

    async def get_team_form(self, team_id, season: int, last: int = 5) -> list[dict]:
        return await self._run("get_team_form", team_id, season, last)

    async def get_h2h(self, home_id, away_id, last: int = 5) -> list[dict]:
        return await self._run("get_h2h", home_id, away_id, last)

    async def get_odds(self, fixture_id, fresh: bool = False) -> list[dict]:
        return await self._run("get_odds", fixture_id, fresh)

    async def get_available_seasons(self) -> list[int]:
        return await self._run("get_available_seasons")

    async def get_account_status(self) -> dict:
        return await self._run("get_account_status")

    async def aclose(self) -> None:
        for client in (self.primary, self.fallback):
            close = getattr(client, "aclose", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                    pass
