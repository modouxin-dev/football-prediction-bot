"""/diag 的多联赛探测：单个联赛当天没球不能再被当成「主源坏了」。

背景：/diag 原先只探主联赛（默认英超）。五大联赛在休赛期和国际比赛日会
整体停摆，而欧冠照常开踢 —— 于是英超无球时诊断显示「赛程请求 ✅ 0 场」，
用户据此反复排查订阅与 key，其实主源一直是活的。本文件锁定三件事：
探测池的构成、三种判定结局、以及「主联赛 0 场但欧冠有球」这个真实场景
必须判成可用。
"""
import asyncio
from types import SimpleNamespace
from commands.admin import (DIAG_PROBE_MAX, DIAG_WIDEN_DAYS, diag_cmd,
                            diag_pool_line, diag_probe_pool, diag_verdict)
from config import load_settings

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)


def run(coro):
    return asyncio.run(coro)


# ── 探测池构成 ────────────────────────────────────────────────

def test_primary_league_is_always_first():
    """主联赛恒排首位：它是每日推送口径，出问题要先看它。"""
    pool = diag_probe_pool(39)
    assert pool[0] == 39


def test_primary_deduped_when_it_is_champions_league():
    """主联赛本身是欧冠时，池子里不能出现两个 2。"""
    pool = diag_probe_pool(2)
    assert pool[0] == 2
    assert len(pool) == len(set(pool))


def test_pool_capped_at_max():
    """探测池有上限：每个联赛一次真实请求，不能无限扩张。"""
    assert len(diag_probe_pool(39)) <= DIAG_PROBE_MAX
    assert len(diag_probe_pool(999)) <= DIAG_PROBE_MAX


def test_unknown_primary_still_probes_others():
    """未登记的联赛作主联赛时，仍要补探已知联赛来判断主源活性。"""
    pool = diag_probe_pool(999)
    assert pool == [999, 2, 140, 78, 135]


# ── 三种判定结局 ──────────────────────────────────────────────

def test_verdict_available_when_any_league_has_matches():
    """任一联赛有比赛 → 主源可用（这正是原先被误判成故障的场景）。"""
    icon, text = diag_verdict([(39, 0, None), (2, 9, None), (140, 0, None)])
    assert icon == "✅"
    assert "主源可用" in text and "9" in text


def test_verdict_failed_when_all_leagues_error():
    """全部报错 → 主源确实不可用。"""
    icon, text = diag_verdict([(39, None, "timeout"), (2, None, "timeout")])
    assert icon == "❌"
    assert "请求失败" in text


def test_verdict_unknown_when_all_empty():
    """全部返回空 → 如实说明无法区分，不替用户下结论。"""
    icon, text = diag_verdict([(39, 0, None), (2, 0, None)])
    assert icon == "⚠️"
    assert "无法区分" in text


def test_verdict_partial_error_is_warning():
    """部分报错、其余为空 → 介于两者之间，不能算可用也不能算全坏。"""
    icon, text = diag_verdict([(39, 0, None), (2, None, "timeout")])
    assert icon == "⚠️"
    assert "部分联赛报错" in text


# ── 摘要行 ────────────────────────────────────────────────────

def test_pool_line_uses_short_names():
    """摘要用短名（欧冠/西甲），全称一行放不下 5 个联赛。"""
    line = diag_pool_line([(39, 0, None), (2, 9, None)])
    assert "英超0" in line and "欧冠9" in line


def test_pool_line_marks_errors():
    """报错的联赛标 ✗，不能显示成 0 —— 0 会让人以为是没比赛。"""
    line = diag_pool_line([(39, None, "timeout")])
    assert "✗" in line


# ── 端到端：主联赛无球但欧冠有球 ────────────────────────────────

class _Primary:
    """按联赛返回固定场数的主源桩。"""

    def __init__(self, counts):
        self.counts = counts
        self.calls = []

    async def get_fixtures(self, league_id, season, date_from, date_to):
        self.calls.append((league_id, date_from, date_to))
        n = self.counts.get(league_id, 0)
        return [{"fixture": {"id": 1000 + league_id * 10 + i}} for i in range(n)]

    async def get_available_seasons(self):
        return [2026]

    async def get_odds(self, fixture_id, fresh=False):
        return [{"bookmakers": []}]

    async def get_injuries(self, fixture_id):
        return []


class _Msg:
    def __init__(self):
        self.text = None

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.text = text
        return None


def _run_diag(counts):
    primary = _Primary(counts)
    router = SimpleNamespace(primary=primary, primary_cooldown_remaining=0,
                             last_error=lambda s: None)
    app = SimpleNamespace(bot_data={"settings": SETTINGS, "api": router},
                          job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []))
    ctx = SimpleNamespace(application=app, user_data={})
    msg = _Msg()
    update = SimpleNamespace(message=msg, effective_message=msg)
    run(diag_cmd(update, ctx))
    return msg.text, primary


def test_main_league_empty_but_champions_league_has_matches():
    """真实事故场景：英超当日 0 场、欧冠 9 场 —— 必须判成主源可用。"""
    text, _ = _run_diag({39: 0, 2: 9})
    assert "主源可用" in text
    assert "9" in text


def test_main_league_empty_does_not_report_zero_as_failure():
    """主联赛 0 场时给出 ⚠️ 而不是 ✅ —— ✅ 会让人以为请求正常拿到 0 场就该如此。"""
    text, _ = _run_diag({39: 0, 2: 9})
    assert "⚠️ 0 场" in text


def test_probe_covers_multiple_leagues():
    """诊断确实请求了多个联赛，而不是只打主联赛一次。"""
    _, primary = _run_diag({39: 0, 2: 9})
    leagues = [c[0] for c in primary.calls]
    assert len(set(leagues)) > 1


def test_odds_probe_falls_back_to_other_league():
    """主联赛无球时，赔率探测改用别的联赛 —— 端点可用性与联赛无关。"""
    text, _ = _run_diag({39: 0, 2: 9})
    assert "跳过探测" not in text


def test_widens_window_when_everything_empty():
    """全网无球时自动扩窗重探，用来区分「今天没球」与「主源异常」。"""
    _, primary = _run_diag({})
    windows = {(c[1], c[2]) for c in primary.calls}
    assert len(windows) >= 2, "应先用当前窗口探一次，全空后再扩窗重探"
    spans = {(t[1] - t[0]).days for t in windows}
    assert max(spans) >= DIAG_WIDEN_DAYS - 1


def test_all_empty_states_unresolvable():
    """扩窗后依然全空：如实写无法区分，不谎报主源故障。"""
    text, _ = _run_diag({})
    assert "无法区分" in text
    assert "❌" not in text.split("主源判定")[1][:40]
