"""/sotprobe 探测命令的离线测试。

上一版只探测 1 场，且「返回空」也会把该场永久标记为已探测。这在真实
环境里是污染：端点其实可用、只是字段名对不上时，一场空响应就会让
所有比赛再也不会被重新探测，等字段修好也拿不到数据。

本文件把「多场采样 + 只在端点被证实可用时才落标记」这两条钉死。
全部用例离线，不发起网络请求。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from commands.admin import (
    SOT_PROBE_DEFAULT,
    SOT_PROBE_MAX,
    _sot_probe_count,
    sot_probe_cmd,
    sot_probe_verdict,
)
from repository import PredictionRepository


# ---- 场数解析 ---------------------------------------------------------------

def test_default_count_when_no_args():
    assert _sot_probe_count(None) == SOT_PROBE_DEFAULT
    assert _sot_probe_count([]) == SOT_PROBE_DEFAULT


def test_numeric_arg_is_used():
    assert _sot_probe_count(["3"]) == 3


def test_count_is_clamped():
    assert _sot_probe_count(["0"]) == 1
    assert _sot_probe_count(["99"]) == SOT_PROBE_MAX


def test_invalid_arg_falls_back_to_default():
    assert _sot_probe_count(["abc"]) == SOT_PROBE_DEFAULT


# ---- 结论判定 ---------------------------------------------------------------

def _r(*statuses):
    return [{"status": s, "id": i} for i, s in enumerate(statuses)]


def test_verdict_ok_when_any_match_parsed():
    verdict, usable = sot_probe_verdict(_r("ok", "empty", "empty"))
    assert usable is True
    assert "✅" in verdict


def test_verdict_field_mismatch_when_response_has_no_shot_field():
    """有响应但字段名对不上 ≠ 端点不可用，要提示补字段而不是放弃。"""
    verdict, usable = sot_probe_verdict(_r("nofield", "nofield", "empty"))
    assert usable is False
    assert "字段名不匹配" in verdict


def test_verdict_empty_means_endpoint_may_be_unavailable():
    verdict, usable = sot_probe_verdict(_r("empty", "empty"))
    assert usable is False
    assert "返回空" in verdict


def test_verdict_error_when_all_requests_fail():
    verdict, usable = sot_probe_verdict(_r("error", "error"))
    assert usable is False
    assert "❌" in verdict


def test_verdict_on_no_results():
    verdict, usable = sot_probe_verdict([])
    assert usable is False


# ---- 端到端：标记策略 -------------------------------------------------------

def _payload(home_sot, away_sot, *, home_id=33, away_id=34,
             shot_type="Shots on Goal"):
    return [
        {"team": {"id": home_id, "name": "Home"},
         "statistics": [{"type": "Shots off Goal", "value": 5},
                        {"type": shot_type, "value": home_sot}]},
        {"team": {"id": away_id, "name": "Away"},
         "statistics": [{"type": "Shots off Goal", "value": 9},
                        {"type": shot_type, "value": away_sot}]},
    ]


class _Primary:
    """按 fixture_id 返回预设结果；None 表示空响应，异常表示请求失败。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[int] = []

    async def get_fixture_statistics(self, fixture_id):
        self.calls.append(fixture_id)
        got = self.mapping.get(fixture_id)
        if isinstance(got, Exception):
            raise got
        return got


class _Message:
    def __init__(self):
        self.sent: list[str] = []

    async def reply_text(self, text, **_kw):
        self.sent.append(text)


def _run(repo, primary, args=None, n_matches=3):
    msg = _Message()
    update = SimpleNamespace(message=msg, effective_message=msg)
    service = SimpleNamespace(repo=repo, sync=SimpleNamespace(competition="PL"))
    router = SimpleNamespace(primary=primary)
    app = SimpleNamespace(bot_data={"api": router, "service": service,
                                    "settings": SimpleNamespace()})
    context = SimpleNamespace(application=app, args=args or [])
    asyncio.run(sot_probe_cmd(update, context))
    assert msg.sent, "命令应当回复一条消息"
    return msg.sent[0]


def _repo_with_matches(tmp_path, n=3):
    repo = PredictionRepository(str(tmp_path / "t.db"))
    rows = []
    for i in range(1, n + 1):
        rows.append({
            "id": i, "utcDate": f"2026-09-2{i}T19:00:00Z",
            "status": "FT", "matchday": 6,
            "homeTeam": {"id": 33, "name": "H"},
            "awayTeam": {"id": 34, "name": "A"},
            "score": {"fullTime": {"home": 1, "away": 1}},
            "season": {"startDate": "2026-08-01"},
        })
    repo.save_matches("PL", rows)
    return repo


def test_all_empty_does_not_mark_anything(tmp_path):
    """核心防污染：端点未被证实可用时，一场都不许落标记。

    否则一次空响应就把所有比赛永久排除，字段修好后也拿不到数据。
    """
    repo = _repo_with_matches(tmp_path, 3)
    text = _run(repo, _Primary({1: None, 2: None, 3: None}))

    assert "返回空" in text
    assert "未写标记" in text
    # 三场都还在待探测队列里，下次修好字段仍会重新请求
    assert {m["id"] for m in repo.finished_without_stats("PL")} == {1, 2, 3}


def test_field_mismatch_does_not_mark_anything(tmp_path):
    """有响应但字段名对不上，同样不许落标记——等补上字段名再探测。"""
    repo = _repo_with_matches(tmp_path, 2)
    payload = [
        {"team": {"id": 33}, "statistics": [{"type": "Corners", "value": 5}]},
        {"team": {"id": 34}, "statistics": [{"type": "Corners", "value": 9}]},
    ]
    text = _run(repo, _Primary({1: payload, 2: payload}), n_matches=2)

    assert "字段名不匹配" in text
    assert "Corners" in text  # 把真实字段名打出来，便于补识别列表
    assert "未写标记" in text
    assert {m["id"] for m in repo.finished_without_stats("PL")} == {1, 2}


def test_partial_success_saves_values_and_marks_the_rest(tmp_path):
    """端点被证实可用后：有值的写值，没有值的标记为该场无统计。"""
    repo = _repo_with_matches(tmp_path, 3)
    text = _run(repo, _Primary({1: _payload(7, 3), 2: None, 3: None}))

    assert "✅" in text
    got = {m["id"]: (m["home_sot"], m["away_sot"])
           for m in repo.latest_finished("PL", limit=3)}
    assert got[1] == (7, 3)
    assert got[2] == (None, None)
    # 端点可用已证实，无统计的场可以标记，避免每天重复拉取
    assert repo.finished_without_stats("PL") == []


def test_error_on_every_request_reports_unavailable(tmp_path):
    repo = _repo_with_matches(tmp_path, 2)
    text = _run(repo, _Primary({1: RuntimeError("403"), 2: RuntimeError("403")}),
                n_matches=2)
    assert "不可用" in text
    assert "未写标记" in text


def test_no_finished_match_reports_instead_of_crashing(tmp_path):
    repo = PredictionRepository(str(tmp_path / "t.db"))
    text = _run(repo, _Primary({}))
    assert "还没有已完场" in text


def test_probe_count_limits_requests(tmp_path):
    """/sotprobe 2 只应请求 2 场，避免一次吃掉过多额度。"""
    repo = _repo_with_matches(tmp_path, 5)
    primary = _Primary({i: None for i in range(1, 6)})
    _run(repo, primary, args=["2"], n_matches=5)
    assert len(primary.calls) == 2
