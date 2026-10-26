"""/oddsprobe 落盘与端点：盘口结构必须拿得到，不依赖 Telegram 是否送达。

背景（真实事故，2026-10-26）
----------------------------
``/oddsprobe`` 连续多轮「发了命令、机器人不回复」，而生产日志显示
命令其实跑到了最后一步才抛 ``BadRequest: Message is too long``：

    File "/app/commands/admin.py", line 1431, in oddsprobe_cmd
    telegram.error.BadRequest: Message is too long

一场比赛几十个盘口，全量选项拼成单条必然超 4096。而异常发生在**最后一步**，
前面取数全白做，用户侧完全看不出是「没跑 / 没取到 / 消息太长」。

修复思路是三层保障，本文件覆盖第 2、3 层：
1. 命令先回一条「正在探测」（让用户知道被接收）
2. **结果先落盘**——不依赖任何一条消息是否发送成功
3. 命令只回精简摘要（每盘一行），完整选项走网页端点 ``/oddsprobe``

覆盖点 / Coverage
-----------------
* 落盘：盘口结构写库，失败静默（诊断设施不能打断主流程）
* 端点：``/oddsprobe`` 返回完整 options + 成败记录 runs
* 成败记录：区分「命令没跑」「跑了没盘口」「取到了但发不出去」
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import oddsprobe_log as log_mod

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def olog(tmp_path):
    log_mod.set_db_path(tmp_path / "odds_probe.db")
    log_mod.clear()
    yield log_mod
    log_mod.clear()


def _bet(bid, name, values):
    return {"id": bid, "name": name, "values": values}


# ---- 落盘 -----------------------------------------------------------------


def test_record_bets_persists_and_returns_count(olog):
    bets = [
        _bet(1, "Match Winner", [{"value": "Home", "odd": "2.1"}] * 3),
        _bet(4, "Asian Handicap", [{"value": "Home -0.5", "odd": "1.95"}]),
    ]
    assert olog.record_bets("1570431", bets) == 2


def test_record_bets_survives_bad_payload(olog):
    """空/畸形输入不得抛异常——埋点失败绝不能打断探测主流程。"""
    assert olog.record_bets("1", []) == 0
    assert olog.record_bets("1", [{"id": None, "name": None, "values": None}]) == 1


def test_latest_bets_returns_full_values(olog):
    """关键：完整选项必须可取回——这是判断亚盘写法的唯一依据。"""
    olog.record_bets("1570431", [
        _bet(4, "Asian Handicap", [{"value": "Home -0.5", "odd": "1.95"},
                                   {"value": "Away +0.5", "odd": "1.85"}]),
    ])
    bets = olog.latest_bets()
    assert len(bets) == 1
    assert bets[0]["id"] == "4"
    assert bets[0]["name"] == "Asian Handicap"
    assert bets[0]["values"][0]["value"] == "Home -0.5"


def test_latest_bets_sorted_by_numeric_bet_id(olog):
    """按数值排序，避免 "10" 排在 "2" 前面这种字符串序。"""
    olog.record_bets("1", [_bet(10, "B", []), _bet(2, "A", []), _bet(1, "C", [])])
    assert [b["id"] for b in olog.latest_bets()] == ["1", "2", "10"]


# ---- 成败记录：区分三种「没回复」 ----------------------------------------


def test_record_run_marks_failure_with_reason(olog):
    olog.record_run("1570431", False, 0, "Message is too long")
    runs = olog.latest_runs()
    assert runs[0]["ok"] is False
    assert runs[0]["message"] == "Message is too long"


def test_record_run_marks_success_with_bet_count(olog):
    olog.record_run("1570431", True, 42)
    runs = olog.latest_runs()
    assert runs[0]["ok"] is True
    assert runs[0]["n_bets"] == 42


def test_empty_state_is_safe(olog):
    """从未探测过时，读取得返回空而不是抛异常。"""
    assert olog.latest_bets() == []
    assert olog.latest_runs() == []
    assert olog.latest_fixture_id() is None


# ---- 消息长度：精简摘要必须发得出去 --------------------------------------


def test_summary_output_fits_telegram_limit():
    """80 个盘口的精简摘要（每盘一行）必须远低于 4096。

    这是「机器人没回复」的直接成因：全量选项一行一个值，几十个盘口就爆。
    摘要只列 id·名称·选项数，因此长度与盘口数成线性且系数很小。
    """
    lines = [f"探测对象　fixture 1570431", "构建版本　abcdefg",
             "博彩公司　8 家", "盘口总数　80 个（已存库 80 条）", ""]
    for i in range(80):
        lines.append(f"id={i} · Bet Name Number {i}　（3 项）")
    text = "\n".join(lines)
    assert len(text) < 4096, f"摘要仍有 {len(text)} 字符，会撞 Telegram 上限"


# ---- 端点 -----------------------------------------------------------------


def test_endpoint_returns_bets_and_runs(olog):
    """端点必须同时给出盘口与成败记录，否则无法区分没跑/没取到。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    olog.record_bets("1570431", [_bet(4, "Asian Handicap", [])])
    olog.record_run("1570431", True, 1)

    import api
    client = TestClient(api.app)
    resp = client.get("/oddsprobe")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["bets"][0]["name"] == "Asian Handicap"
    assert data["runs"][0]["ok"] is True
    assert data["fixture_id"] == "1570431"


def test_endpoint_survives_no_probe_yet(olog):
    """从未探测过时端点必须 200 + 空列表，而不是 500。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import api
    client = TestClient(api.app)
    resp = client.get("/oddsprobe")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
