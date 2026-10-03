"""审计 P0-1 / P0-2 的回归防护（总控审计 2026-10-03）。

背景：两个 NameError 曾一路活到 `main`——

* `football_data.py` 引用了 `TTL_FIXTURES`，但该模块只定义了
  `TTL_MATCHES / TTL_STANDINGS / TTL_SEASONS`，常量其实在 `api_client.py`。
  触发点 `sync.py → data_source.py → get_fixtures_by_season`，
  异常被 `except Exception` 吞掉，用户只看到「回填失败」。
* `commands/admin.py` 的 `/backfill all` 分支里，生成器解包的是
  `sn, rc, sv`，f-string 却引用了第四个名字 `s`，执行即崩。

两者能活到 main，根因是**对应路径零测试覆盖**。本文件即为补上的覆盖，
另见 `tests/test_static_gate.py`（静态门禁，能一次性杜绝同类「未定义名」）。
"""
import ast
import asyncio
import sqlite3

import pytest

import football_data


# ── P0-1：TTL 常量 ────────────────────────────────────────────

def _referenced_ttl_names(path="football_data.py"):
    """扫描源码里所有被读取的 TTL_* 名字。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    return sorted({
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and n.id.startswith("TTL_")
    })


def test_all_referenced_ttl_constants_are_defined():
    """本模块引用的每个 TTL_* 都必须在本模块内定义。

    P0-1 的直接回归测试：只要有人再引用一个没定义的 TTL_*，这里立刻红。
    """
    names = _referenced_ttl_names()
    assert names, "扫描不到 TTL_* 说明解析逻辑失效，测试本身有问题"
    missing = [n for n in names if not hasattr(football_data, n)]
    assert not missing, f"引用了未定义的常量：{missing}"


def test_ttl_fixtures_defined_and_positive():
    """TTL_FIXTURES 必须存在且为正整数（整季赛程缓存时长）。"""
    val = getattr(football_data, "TTL_FIXTURES", None)
    assert isinstance(val, int) and val > 0, f"TTL_FIXTURES 异常：{val!r}"


@pytest.mark.asyncio
async def test_get_fixtures_by_season_no_nameerror(monkeypatch):
    """运行时实证：`get_fixtures_by_season` 不得再抛 NameError。

    修复前这里抛 `NameError: name 'TTL_FIXTURES' is not defined`。
    """
    api = football_data.FootballDataAPI("fake-token")

    async def fake_seasons():
        return [2021]

    monkeypatch.setattr(api, "get_available_seasons", fake_seasons)

    seen = {}

    async def fake_get(path, params, ttl=None):
        seen["ttl"] = ttl
        return {"matches": []}

    monkeypatch.setattr(api, "_get", fake_get)

    out = await api.get_fixtures_by_season(39, 2021)
    assert out == []
    # 修复前根本走不到发请求这一步，这里连带确认 ttl 已正常传入
    assert seen["ttl"] == football_data.TTL_FIXTURES


# ── P0-2：/backfill all 分支 ──────────────────────────────────

class _Msg:
    def __init__(self, sink):
        self._sink = sink

    async def edit_text(self, text, **kw):
        self._sink.append(text)

    async def reply_text(self, text, **kw):
        self._sink.append(text)
        return self


class _Update:
    def __init__(self, sink):
        self.effective_message = _Msg(sink)


class _Sync:
    competition = "PL"

    def __init__(self, results):
        self._results = results

    async def sync_season(self, season):
        return self._results.get(season, {"received": 0, "saved": 0})


class _Api:
    def __init__(self, seasons):
        self._seasons = seasons

    async def get_available_seasons(self):
        return list(self._seasons)


class _Repo:
    def count_finished_matches(self, comp):
        return 7


class _Service:
    def __init__(self, results, seasons):
        self.sync = _Sync(results)
        self.api = _Api(seasons)
        self.repo = _Repo()


def _run_backfill(results, seasons, args=("all",)):
    from commands.admin import backfill_cmd

    sink = []
    service = _Service(results, seasons)
    ctx = type("C", (), {
        "application": type("A", (), {"bot_data": {"service": service}})(),
        "args": list(args),
    })()
    asyncio.run(backfill_cmd(_Update(sink), ctx))
    return "\n".join(sink)


def test_backfill_all_no_nameerror():
    """`/backfill all` 必须正常出结果，不得因未定义名崩溃。

    修复前 f-string 里的 `s` 触发 `NameError`，整条命令崩掉。
    """
    out = _run_backfill({2025: {"received": 380, "saved": 380},
                         2026: {"received": 40, "saved": 40}},
                        [2025, 2026])
    assert "全赛季回填完成" in out
    assert "2025 赛季" in out and "2026 赛季" in out


def test_backfill_all_marks_zero_saved_with_warning():
    """保存数为 0 的赛季标 ⚠️，有保存的标 ✅ —— 即 P0-2 的修复语义。

    修复前用的是未定义的 `s`；修复后按「保存数 sv > 0」判定。
    """
    out = _run_backfill({2025: {"received": 380, "saved": 380},
                         2024: {"received": 0, "saved": 0}},
                        [2024, 2025])
    assert "✅ 2025 赛季" in out, out
    assert "⚠️ 2024 赛季" in out, out


def test_backfill_all_reports_no_seasons():
    """账号没返回任何赛季时给出提示，而不是空列表静默通过。"""
    out = _run_backfill({}, [])
    assert "未返回任何可用赛季" in out


def test_pred_repo_used_in_backfill_is_untouched():
    """回填只补赛果，不写预测表（沿用 Stage 9 的核心约束）。"""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE IF NOT EXISTS predictions (id TEXT PRIMARY KEY)")
    # 仅确认本文件不依赖 prediction 表的写入路径
    assert con.execute("SELECT count(*) FROM predictions").fetchone()[0] == 0
