"""赛程同步：外部 API 负责拉取，SQLite 负责保存。

架构原则（不要让 Telegram 命令直接依赖外部 API）：

    football-data.org → 后端同步模块 → /data/football.db
                                          ↓
                             赛程 / 历史 / 预测 / Telegram

同步分三种：
- 启动同步：拉未来 30 天赛程
- 定时同步：每 6 小时更新未来赛程与状态
- 赛后同步：更新最近 14 天比赛，落最终比分

每次同步都记录：数据源、联赛、日期范围、HTTP 状态码、返回数量、保存数量。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

UPCOMING_DAYS = 30      # 启动 / 定时同步：未来 30 天
RECENT_DAYS = 14        # 赛后同步：最近 14 天
SYNC_INTERVAL_HOURS = 6  # 定时同步间隔


class MatchSync:
    """把外部赛程拉到本地 SQLite，供赛程 / 预测 / 分析直接读取。"""

    def __init__(self, service, repository) -> None:
        self.service = service
        self.repo = repository
        self.last_result: dict = {}

    @property
    def competition(self) -> str:
        """联赛代码：39 → PL。用于本地库分区，避免不同联赛数据混淆。"""
        try:
            from football_data import _code_for

            return _code_for(self.service.settings.league_id)
        except Exception:
            return str(self.service.settings.league_id)

    async def sync_window(self, date_from: date, date_to: date,
                          *, kind: str = "window") -> dict:
        """拉取指定日期窗口的比赛并落盘。

        主源 HTTP 成功但返回 0 场 ≠ 失败，记为「窗口内暂无比赛」，不切备用源。
        只有 HTTP 错误 / 超时 / 解析失败才切换。
        """
        comp = self.competition
        result = {
            "kind": kind, "competition": comp,
            "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "http_status": "-", "received": 0, "saved": 0,
            "message": "", "ok": False,
        }
        try:
            fixtures, season, _note = await self.service._fetch_fixtures(date_from, date_to)
        except Exception as exc:  # noqa: BLE001 - 同步失败不能拖垮启动
            result["http_status"] = type(exc).__name__
            result["message"] = str(exc)[:200]
            self.repo.log_sync(
                self.service.source_label, comp,
                result["date_from"], result["date_to"],
                result["http_status"], 0, 0, result["message"],
            )
            log.warning("同步失败[%s]：%s", kind, exc)
            self.last_result = result
            return result

        result["http_status"] = "200"
        result["received"] = len(fixtures)
        if not fixtures:
            result["message"] = "当前时间范围内暂无比赛"
            result["ok"] = True  # 0 场不是错误
            self.repo.log_sync(
                self.service.source_label, comp,
                result["date_from"], result["date_to"],
                "200", 0, 0, result["message"],
            )
            self.last_result = result
            return result

        saved = self.repo.save_matches(
            comp, fixtures,
            source=self.service.source_label,
            http_status="200",
            message=kind,
        )
        result["saved"] = saved
        result["ok"] = saved > 0
        log.info("同步完成[%s]：%s %s~%s 收到 %d 保存 %d",
                 kind, comp, result["date_from"], result["date_to"],
                 len(fixtures), saved)
        self.last_result = result
        return result

    async def sync_upcoming(self, days: int = UPCOMING_DAYS) -> dict:
        """启动 / 定时同步：未来 N 天赛程。"""
        today = datetime.now(timezone.utc).date()
        return await self.sync_window(today, today + timedelta(days=days), kind="upcoming")

    async def sync_recent(self, days: int = RECENT_DAYS) -> dict:
        """赛后同步：最近 N 天比赛，回写最终比分。"""
        today = datetime.now(timezone.utc).date()
        return await self.sync_window(today - timedelta(days=days), today, kind="recent")

    async def sync_full_season(self) -> dict:
        """整季历史：一次拉全量赛季赛程并落盘（不要每次查询都请求 380 场）。"""
        today = datetime.now(timezone.utc).date()
        return await self.sync_window(
            today - timedelta(days=365), today + timedelta(days=365), kind="full-season"
        )

    async def run_startup(self) -> dict:
        """启动同步：先拉未来赛程，保证机器人起来就有数据可展示。"""
        return await self.sync_upcoming()


def has_fresh_data(repo, competition: str, date_from: date, date_to: date) -> bool:
    """本地库里是否已有该窗口的比赛，避免每次查询都回源。"""
    try:
        return bool(repo.load_matches(str(competition), date_from.isoformat(), date_to.isoformat()))
    except Exception:
        return False
