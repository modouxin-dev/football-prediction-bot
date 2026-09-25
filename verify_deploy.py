#!/usr/bin/env python3
"""部署验收脚本：一次跑完部署后的全部关键检查。

用法（在有网络的环境，配置好环境变量后）：

    export FOOTBALL_DATA_API_TOKEN='你的token'
    python3 verify_deploy.py

不打印 Token，只报告每一项是否通过。全部通过退出码 0，否则非 0。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []

DB = os.getenv("VERIFY_DB_PATH") or "/tmp/verify_tmp.db"

SAMPLE = [
    {
        "id": 900001,
        "utcDate": "2030-01-01T20:00:00Z",
        "status": "SCHEDULED",
        "matchday": 1,
        "homeTeam": {"id": 1, "name": "Team A"},
        "awayTeam": {"id": 2, "name": "Team B"},
        "score": {"fullTime": {"home": None, "away": None}},
        "season": {"startDate": "2029-08-01"},
    }
]


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"{ {'PASS':'✅','FAIL':'❌','SKIP':'⏭️'}[status] } {name}"
          + (f" — {detail}" if detail else ""))


def check(name: str, fn) -> None:
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001
        status, detail = FAIL, f"{type(exc).__name__}: {exc}"
    record(name, status, detail)


# ── 1. Token ─────────────────────────────────────────────────────────
def t_token():
    token = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
    if not token:
        return FAIL, "未配置 FOOTBALL_DATA_API_TOKEN"
    return PASS, f"已配置（长度 {len(token)}，不打印内容）"


# ── 2. 真实 HTTP 请求 ────────────────────────────────────────────────
def t_http():
    token = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
    if not token:
        return SKIP, "无 Token，跳过真实请求"
    import football_data

    api = football_data.FootballDataAPI(token=token)
    data = api.get_matches(competition="PL")
    n = len(data.get("matches", [])) if isinstance(data, dict) else len(data)
    return PASS, f"HTTP 200，返回 {n} 场"


# ── 3. 路径 ──────────────────────────────────────────────────────────
def t_paths():
    import paths

    return PASS, f"DATA_DIR={paths.DATA_DIR} DB_PATH={paths.DB_PATH}"


# ── 4. 落盘 ──────────────────────────────────────────────────────────
def t_save():
    import repository

    if os.path.exists(DB):
        os.remove(DB)
    repo = repository.PredictionRepository(DB)
    repo.save_matches("PL", SAMPLE)
    with sqlite3.connect(DB) as conn:
        n = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    return (PASS, f"matches 表 {n} 行") if n == 1 else (FAIL, f"期望 1 行，实为 {n}")


# ── 5. 查询 ──────────────────────────────────────────────────────────
def t_load():
    import repository

    repo = repository.PredictionRepository(DB)
    rows = repo.load_matches("PL", "2030-01-01", "2030-01-01")
    return (PASS, f"查到 {len(rows)} 场") if rows else (FAIL, "历史比赛查不到")


# ── 6. 空窗口提示 ────────────────────────────────────────────────────
def t_empty_window():
    """空窗口必须提示「暂无比赛」，不能说「数据源无数据」。"""
    import asyncio

    import sync as sync_mod

    class FakeSettings:
        league_id = 39

    class FakeService:
        settings = FakeSettings()
        source_label = "football-data.org"

        async def _fetch_fixtures(self, *_a, **_kw):
            return [], 2026, ""

    class FakeRepo:
        def save_matches(self, *_a, **_kw):
            return 0

        def log_sync(self, *_a, **_kw):
            return None

    async def run():
        s = sync_mod.MatchSync(FakeService(), FakeRepo())
        return await s.sync_window(date(2030, 1, 2), date(2030, 1, 2))

    res = asyncio.run(run())
    msg = res.get("message", "")
    ok = ("暂无比赛" in msg) and ("无数据" not in msg)
    return (PASS, msg) if ok else (FAIL, f"提示不当：{msg!r}")


# ── 7. 预测落盘 ──────────────────────────────────────────────────────
def t_prediction():
    import repository

    repo = repository.PredictionRepository(DB)
    pred = SimpleNamespace(
        fixture_id=900001,
        season=2026,
        league="PL",
        home="Team A",
        away="Team B",
        kickoff=datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc),
        model_version="verify-1.0",
        source="verify",
        level={"key": "mid", "result": "主胜"},
        analysis={
            "best_score": 1.2,
            "win_prob": 0.55,
            "draw_prob": 0.25,
            "loss_prob": 0.20,
        },
        inputs={"verify": True},
        created_at=datetime.now(timezone.utc),
    )
    repo.save(pred)
    with sqlite3.connect(DB) as conn:
        n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    return (PASS, f"predictions 表 {n} 行") if n else (FAIL, "预测未落盘")


# ── 8. 赛后回写 + 命中率 ─────────────────────────────────────────────
def t_settle():
    import repository

    repo = repository.PredictionRepository(DB)
    if not repo.settle(900001, 2, 0):
        return FAIL, "回写失败（无匹配记录）"
    st = repo.stats()
    return PASS, f"回写成功 total={st.get('total')} hit={st.get('hit')}"


# ── 9. 挂载卷跨部署保留 ──────────────────────────────────────────────
def t_volume():
    import paths

    marker = paths.DATA_DIR / ".volume_marker"
    try:
        if marker.exists():
            return PASS, f"标记文件已存在（{marker}）— 跨部署保留"
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(date.today().isoformat(), encoding="utf-8")
        return SKIP, "首次写入标记，重部署后再跑一次应显示已存在"
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=" * 60)
    print("部署验收 — football-prediction-bot")
    print("=" * 60)
    check("1. Token 已读取", t_token)
    check("2. 外部 API HTTP 200", t_http)
    check("3. 路径解析", t_paths)
    check("4. 赛程落盘 SQLite", t_save)
    check("5. 历史比赛可查询", t_load)
    check("6. 空窗口提示正确", t_empty_window)
    check("7. 预测记录保存", t_prediction)
    check("8. 赛后比分回写", t_settle)
    check("9. 挂载卷跨部署保留", t_volume)
    print("=" * 60)
    failed = [n for n, s, _ in results if s == FAIL]
    print(f"通过 {sum(1 for _, s, _ in results if s == PASS)} / "
          f"失败 {len(failed)} / 跳过 {sum(1 for _, s, _ in results if s == SKIP)}")
    if failed:
        print("失败项：" + "、".join(failed))
    if os.path.exists(DB):
        os.remove(DB)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
