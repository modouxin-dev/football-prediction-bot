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

多联赛：逐联赛拉取、按联赛分区保存（西甲比赛不会存进英超分区），
单个联赛失败不影响其他联赛。
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

    # ---- 联赛 / 分区 ------------------------------------------------------
    @property
    def league_ids(self) -> tuple[int, ...]:
        """全部启用联赛。老配置没有 league_ids 字段时退化为单联赛。"""
        s = self.service.settings
        ids = getattr(s, "league_ids", None)
        if not ids:
            return (int(s.league_id),)
        return tuple(int(i) for i in ids)

    def competition_for(self, league_id: int) -> str:
        """指定联赛的本地库分区代码：39 → PL。"""
        try:
            from football_data import _code_for

            return _code_for(int(league_id))
        except Exception:
            return str(int(league_id))

    @property
    def competitions(self) -> tuple[str, ...]:
        """全部启用联赛对应的分区代码。"""
        return tuple(self.competition_for(i) for i in self.league_ids)

    @property
    def competition(self) -> str:
        """主联赛的分区代码。用于本地库分区，避免不同联赛数据混淆。"""
        return self.competition_for(self.service.settings.league_id)

    # ---- 窗口同步 ---------------------------------------------------------
    async def sync_window(self, date_from: date, date_to: date,
                          *, kind: str = "window") -> dict:
        """拉取指定日期窗口的比赛并落盘。

        主源 HTTP 成功但返回 0 场 ≠ 失败，记为「窗口内暂无比赛」，不切备用源。
        只有 HTTP 错误 / 超时 / 解析失败才切换。
        """
        comps = self.competitions
        result = {
            "kind": kind,
            "competition": comps[0] if len(comps) == 1 else ",".join(comps),
            "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "http_status": "200", "received": 0, "saved": 0,
            "message": "", "ok": False, "per_league": [],
        }
        for league_id in self.league_ids:
            result["per_league"].append(
                await self._sync_window_one(league_id, date_from, date_to, kind=kind)
            )
        self._aggregate(result)
        self.last_result = result
        return result

    async def _sync_window_one(self, league_id: int, date_from: date, date_to: date,
                               *, kind: str) -> dict:
        """单个联赛的窗口同步：拉取 → 按该联赛分区保存。"""
        comp = self.competition_for(league_id)
        item = {
            "league_id": int(league_id), "competition": comp,
            "http_status": "200", "received": 0, "saved": 0,
            "message": "", "ok": False,
        }
        try:
            fixtures, _season, _note = await self.service._fetch_fixtures(
                date_from, date_to, league_id=league_id
            )
        except Exception as exc:  # noqa: BLE001 - 同步失败不能拖垮启动
            item["http_status"] = type(exc).__name__
            item["message"] = str(exc)[:200]
            self.repo.log_sync(
                self.service.source_label, comp,
                date_from.isoformat(), date_to.isoformat(),
                item["http_status"], 0, 0, item["message"],
            )
            log.warning("同步失败[%s][联赛 %s]：%s", kind, league_id, exc)
            return item

        item["received"] = len(fixtures or [])
        if not fixtures:
            item["message"] = "当前时间范围内暂无比赛"
            item["ok"] = True  # 0 场不是错误
            self.repo.log_sync(
                self.service.source_label, comp,
                date_from.isoformat(), date_to.isoformat(),
                "200", 0, 0, item["message"],
            )
            return item

        saved = self.repo.save_matches(
            comp, list(fixtures),
            source=self.service.source_label,
            http_status="200",
            message=kind,
        )
        item["saved"] = saved
        item["ok"] = saved > 0
        log.info("同步完成[%s]：%s(联赛 %s) %s~%s 收到 %d 保存 %d",
                 kind, comp, league_id, date_from.isoformat(),
                 date_to.isoformat(), len(fixtures), saved)
        return item

    # ---- 赛季回填 ---------------------------------------------------------
    async def sync_season(self, season: int) -> dict:
        """按赛季整季回填（付费套餐解锁的历史赛季用）。

        与 sync_window 的区别：不带日期范围，只按 league+season 请求，
        否则主源会因套餐限制拒绝历史赛季。
        """
        comps = self.competitions
        result = {
            "kind": f"season-{season}",
            "competition": comps[0] if len(comps) == 1 else ",".join(comps),
            "season": season, "date_from": "-", "date_to": "-",
            "http_status": "200", "received": 0, "saved": 0,
            "message": "", "ok": False, "per_league": [],
        }
        for league_id in self.league_ids:
            result["per_league"].append(await self._sync_season_one(league_id, season))
        self._aggregate(result, empty_message="该赛季暂无数据（可能套餐不支持）")
        self.last_result = result
        return result

    async def _sync_season_one(self, league_id: int, season: int) -> dict:
        """单个联赛的整季回填。"""
        comp = self.competition_for(league_id)
        item = {
            "league_id": int(league_id), "competition": comp,
            "http_status": "200", "received": 0, "saved": 0,
            "message": "", "ok": False,
        }
        try:
            fixtures = await self.service.api.get_fixtures_by_season(
                int(league_id), season
            )
        except Exception as exc:  # noqa: BLE001
            item["http_status"] = type(exc).__name__
            item["message"] = str(exc)[:200]
            self.repo.log_sync(
                self.service.source_label, comp, "-", "-",
                item["http_status"], 0, 0, item["message"],
            )
            log.warning("赛季 %s 回填失败（联赛 %s）：%s", season, league_id, exc)
            return item

        item["received"] = len(fixtures or [])
        if not fixtures:
            item["message"] = "该赛季暂无数据（可能套餐不支持）"
            item["ok"] = True
            self.repo.log_sync(
                self.service.source_label, comp, "-", "-",
                "200", 0, 0, item["message"],
            )
            return item

        saved = self.repo.save_matches(
            comp, list(fixtures),
            source=self.service.source_label,
            http_status="200",
            message=f"season-{season}",
        )
        item["saved"] = saved
        item["ok"] = saved > 0
        log.info("赛季 %s 回填（联赛 %s）：收到 %d 保存 %d",
                 season, league_id, len(fixtures), saved)
        return item

    # ---- 汇总 -------------------------------------------------------------
    @staticmethod
    def _aggregate(result: dict, *, empty_message: str = "当前时间范围内暂无比赛") -> None:
        """把 per_league 汇总成顶层 received / saved / ok / message。

        部分联赛失败时整体仍算成功（只要有联赛存进去），
        全部失败才算失败——避免单个联赛套餐不支持拖垮整次同步。
        """
        items = result.get("per_league") or []
        result["received"] = sum(i.get("received", 0) for i in items)
        result["saved"] = sum(i.get("saved", 0) for i in items)
        failed = [i for i in items if not i.get("ok")]

        if failed and len(failed) == len(items):
            result["http_status"] = failed[0].get("http_status", "-")
            result["message"] = failed[0].get("message", "同步失败")
            result["ok"] = False
        elif failed:
            result["http_status"] = "200"
            result["message"] = (
                f"部分联赛失败（{len(failed)}/{len(items)}）："
                f"{failed[0].get('message', '')[:120]}"
            )
            result["ok"] = result["saved"] > 0
        elif result["received"] == 0:
            result["http_status"] = "200"
            result["message"] = empty_message
            result["ok"] = True
        else:
            result["http_status"] = "200"
            result["message"] = ""
            result["ok"] = result["saved"] > 0

    # ---- 常用窗口 ---------------------------------------------------------
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
