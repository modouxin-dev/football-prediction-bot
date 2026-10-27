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
from typing import Any

log = logging.getLogger(__name__)

UPCOMING_DAYS = 30      # 启动 / 定时同步：未来 30 天
RECENT_DAYS = 14        # 赛后同步：最近 14 天
SYNC_INTERVAL_HOURS = 6  # 定时同步间隔


class MatchSync:
    """把外部赛程拉到本地 SQLite，供赛程 / 预测 / 分析直接读取。"""

    def __init__(self, service, repository) -> None:
        self.service = service
        self.repo = repository
        self.last_result: dict[str, Any] = {}

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
                          *, kind: str = "window") -> dict[str, Any]:
        """拉取指定日期窗口的比赛并落盘。

        主源 HTTP 成功但返回 0 场 ≠ 失败，记为「窗口内暂无比赛」，不切备用源。
        只有 HTTP 错误 / 超时 / 解析失败才切换。
        """
        comps = self.competitions
        result: dict[str, Any] = {
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
                               *, kind: str) -> dict[str, Any]:
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
    async def sync_season(self, season: int) -> dict[str, Any]:
        """按赛季整季回填（付费套餐解锁的历史赛季用）。

        与 sync_window 的区别：不带日期范围，只按 league+season 请求，
        否则主源会因套餐限制拒绝历史赛季。

        ⚠️ 幂等：已结束的赛季若本地已有该赛季数据则整季跳过，不发请求。
        历史赛季的赛果不会再变，重复拉取只是白烧额度——实测 5 联赛 × 10 赛季
        一轮 51 次请求，第二轮新增 0 场，是 100% 空转。
        """
        comps = self.competitions
        result: dict[str, Any] = {
            "kind": f"season-{season}",
            "competition": comps[0] if len(comps) == 1 else ",".join(comps),
            "season": season, "date_from": "-", "date_to": "-",
            "http_status": "200", "received": 0, "saved": 0,
            "message": "", "ok": False, "per_league": [],
        }
        for league_id in self.league_ids:
            result["per_league"].append(await self._sync_season_one(league_id, season))
        items = result["per_league"]
        skipped = [i for i in items if i.get("skipped")]
        result["skipped"] = len(skipped)
        self._aggregate(result, empty_message="该赛季暂无数据（可能套餐不支持）")
        # 全部跳过时 received/saved 都是 0，默认文案会误报成「套餐不支持」；
        # 这里改成真实原因，否则看板上「跳过」看起来像失败。
        if skipped and len(skipped) == len(items):
            result["ok"] = True
            result["http_status"] = "200"
            result["message"] = (
                f"赛季 {season} 已结束且本地已有数据，整季跳过"
                f"（历史赛季不会变，不重复消耗额度）")
        elif skipped:
            result["message"] = (
                f"部分联赛已回填跳过（{len(skipped)}/{len(items)}）："
                f"{result['message']}" if result.get("message") else
                f"部分联赛已回填跳过（{len(skipped)}/{len(items)}）")
        self.last_result = result
        return result

    async def _sync_season_one(self, league_id: int, season: int) -> dict[str, Any]:
        """单个联赛的整季回填。"""
        comp = self.competition_for(league_id)
        item = {
            "league_id": int(league_id), "competition": comp,
            "http_status": "200", "received": 0, "saved": 0,
            "message": "", "ok": False,
        }
        # 已结束的赛季：本地有数据就不再回源。
        # 进行中的赛季（当前赛季）绝不跳过——它每轮都可能产生新赛果。
        if _season_is_closed(season, date.today()) and self._season_has_local(comp, season):
            item["ok"] = True
            item["skipped"] = True
            item["message"] = f"赛季 {season} 已结束且本地已有数据，跳过"
            log.info("赛季 %s 回填跳过（联赛 %s）：已有本地数据且赛季已结束",
                     season, league_id)
            return item
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

    def _season_has_local(self, comp: str, season: int) -> bool:
        """本地库里是否已有该联赛该赛季的比赛。

        只用于「跳过判断」，因此读不到就返回 False（走原路径回源），
        绝不能因为查询失败就把还没回填的赛季误判成已回填。
        """
        d0, d1 = _season_window(season)
        try:
            return bool(self.repo.load_matches(str(comp), d0, d1, limit=1))
        except Exception:  # noqa: BLE001
            return False

    # ---- 汇总 -------------------------------------------------------------
    @staticmethod
    def _aggregate(result: dict[str, Any], *, empty_message: str = "当前时间范围内暂无比赛") -> None:
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
    async def sync_upcoming(self, days: int = UPCOMING_DAYS) -> dict[str, Any]:
        """启动 / 定时同步：未来 N 天赛程。"""
        today = datetime.now(timezone.utc).date()
        return await self.sync_window(today, today + timedelta(days=days), kind="upcoming")

    async def sync_recent(self, days: int = RECENT_DAYS) -> dict[str, Any]:
        """赛后同步：最近 N 天比赛，回写最终比分。"""
        today = datetime.now(timezone.utc).date()
        return await self.sync_window(today - timedelta(days=days), today, kind="recent")

    async def sync_full_season(self) -> dict[str, Any]:
        """当前赛季整季回填（约 380 场）。

        原实现走的是 ``sync_window(today-365, today+365)`` —— 带日期范围。
        问题：主源对历史赛季的套餐限制正是按"是否带日期范围"判定的，
        带日期请求会被拒（见 ``sync_season`` 的注释）。名字叫"整季"却
        走窗口，导致 `/backfill` 不带参数时常常一场都拿不到。
        改为委托 ``sync_season``，只按 league+season 请求。
        """
        return await self.sync_season(self.service.season_in_use)

    async def sync_history(self, seasons: list[int] | None = None) -> dict[str, Any]:
        """回填历史赛季（付费套餐解锁）。

        :param seasons: 显式指定赛季列表；None = 向主源查询账号可用赛季。
        返回汇总结果，逐赛季明细在 ``per_season`` 里。
        """
        if seasons is None:
            try:
                seasons = list(await self.service.api.get_available_seasons())
            except Exception as exc:  # noqa: BLE001
                log.warning("查询可用赛季失败：%s", exc)
                seasons = []
        result: dict[str, Any] = {
            "kind": "history", "season": "-", "seasons": 0,
            "received": 0, "saved": 0, "per_season": [], "ok": False,
            "message": "" if seasons else "未能获取可用赛季（可能套餐不支持历史赛季）",
        }
        for season in seasons:
            try:
                r = await self.sync_season(int(season))
                result["per_season"].append({
                    "season": int(season),
                    "received": r.get("received", 0),
                    "saved": r.get("saved", 0),
                    "ok": bool(r.get("ok")),
                    "message": r.get("message", ""),
                })
                result["received"] += int(r.get("received", 0))
                result["saved"] += int(r.get("saved", 0))
            except Exception as exc:  # noqa: BLE001 - 单季失败不影响其余
                log.warning("赛季 %s 回填失败：%s", season, exc)
                result["per_season"].append({
                    "season": int(season), "received": 0, "saved": 0,
                    "ok": False, "message": str(exc)[:200],
                })
        result["seasons"] = len(result["per_season"])
        result["ok"] = result["saved"] > 0
        self.last_result = result
        return result

    async def run_startup(self) -> dict[str, Any]:
        """启动同步：先拉未来赛程，保证机器人起来就有数据可展示。"""
        return await self.sync_upcoming()


def has_fresh_data(repo, competition: str, date_from: date, date_to: date) -> bool:
    """本地库里是否已有该窗口的比赛，避免每次查询都回源。"""
    try:
        return bool(repo.load_matches(str(competition), date_from.isoformat(), date_to.isoformat()))
    except Exception:
        return False


def _season_window(season: int) -> tuple[str, str]:
    """赛季覆盖的日期窗口。

    欧洲主流联赛是跨年制（2024 赛季 = 2024-07 ~ 2025-06），
    取 07-01 ~ 次年 06-30 能完整覆盖，且不与相邻赛季重叠。
    """
    return f"{season}-07-01", f"{season + 1}-06-30"


def _season_is_closed(season: int, today: date) -> bool:
    """该赛季是否已经结束（结束后不会再产生新赛果）。

    7 月及以后视为新赛季已开打，此前仍算上赛季进行中。
    只有「已结束」的赛季才允许跳过——进行中的赛季每轮都可能出新赛果，
    跳过会让本地库停在过去某个时点。
    """
    current_opening = today.year if today.month >= 7 else today.year - 1
    return int(season) < current_opening
