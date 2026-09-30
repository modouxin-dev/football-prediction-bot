"""/backtest 命令（用本地真实赛果回测）。

三条底线：
1. 样本不足时**只报数字、不给结论**；
2. 全程只读本地库，**不碰任何 API 客户端**；
3. 评估场次不含预热场次。
"""
import asyncio

import pytest

from commands.admin import BACKTEST_MIN_TOTAL, backtest_cmd
from repository import PredictionRepository

TEAMS = [f"T{i}" for i in range(8)]


def _seed(db, total, finished):
    """造 total 场比赛（8 队循环对阵），前 finished 场有比分。"""
    repo = PredictionRepository(db)
    conn = repo._connect()
    for i in range(total):
        h, a = TEAMS[i % 8], TEAMS[(i * 3 + 1) % 8]
        if h == a:
            a = TEAMS[(i + 1) % 8]
        done = i < finished
        conn.execute(
            "INSERT OR REPLACE INTO matches (id,competition_code,season,utc_date,status,"
            "home_team_id,home_team_name,away_team_id,away_team_name,home_score,away_score,"
            "source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "PL", 2026, f"2026-{(i // 20) + 1:02d}-{(i % 20) + 1:02d}T15:00",
             "FT" if done else "NS",
             h, h, a, a,
             (i % 3) if done else None, (i % 2) if done else None,
             "t", "x"))
    conn.commit()
    return repo


class _Msg:
    def __init__(self, sink):
        self._sink = sink

    async def edit_text(self, text, **kw):
        self._sink.append(text)

    async def reply_text(self, text, **kw):
        self._sink.append(text)
        return self


class _Chat:
    async def send_action(self, *a, **kw):
        return None


class _Update:
    def __init__(self, sink):
        self.effective_message = _Msg(sink)
        self.effective_chat = _Chat()


class _Service:
    """刻意**不提供** api 属性：一旦回测代码碰了 API 客户端就会 AttributeError。"""

    def __init__(self, repo):
        self.repo = repo
        self.sync = type("S", (), {"competition": "PL"})()


def _run(repo):
    sink = []
    ctx = type("C", (), {
        "application": type("A", (), {"bot_data": {"service": _Service(repo)}})(),
    })()
    asyncio.run(backtest_cmd(_Update(sink), ctx))
    return "\n".join(sink)


def test_backtest_refuses_small_sample(tmp_path):
    """已完赛 10 场 < 门槛：只报数字，不给命中率/判定。"""
    repo = _seed(str(tmp_path / "a.db"), 20, 10)
    out = _run(repo)
    assert "样本不足" in out
    assert "还差" in out
    assert "判定" not in out, "小样本上给判定等于给假结论"


def test_backtest_min_total_matches_gate(tmp_path):
    """门槛常量 = 预热 + 最小评估场数，且与 /backfill 的 60 场口径不冲突。"""
    assert BACKTEST_MIN_TOTAL >= 30 + 20


def test_backtest_runs_on_real_matches(tmp_path):
    """样本够时给出双路对比与评估场数。"""
    repo = _seed(str(tmp_path / "b.db"), 100, 70)
    out = _run(repo)
    assert "计入评估" in out
    assert "纯泊松" in out and "Elo" in out
    assert "样本不足" not in out


def test_backtest_touches_no_api(tmp_path):
    """零额度：service 上没有 api 属性也能跑完（碰了就抛 AttributeError）。"""
    repo = _seed(str(tmp_path / "c.db"), 100, 70)
    out = _run(repo)
    assert out, "回测应当有输出"


def test_backtest_other_league_not_counted(tmp_path):
    """另一个联赛的赛果不得混入（强度榜污染的同类问题）。"""
    repo = _seed(str(tmp_path / "d.db"), 100, 70)
    conn = repo._connect()
    for i in range(500, 560):
        # 日期必须互不相同：库里对「同一天同一对阵」有唯一约束，重复会被 REPLACE 掉
        j = i - 500
        conn.execute(
            "INSERT OR REPLACE INTO matches (id,competition_code,season,utc_date,status,"
            "home_team_id,home_team_name,away_team_id,away_team_name,home_score,away_score,"
            "source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "ELC", 2026, f"2026-{(j // 28) + 6:02d}-{(j % 28) + 1:02d}T15:00", "FT",
             "X", "X", "Y", "Y", 1, 1, "t", "x"))
    conn.commit()
    assert repo.count_finished_matches("PL") == 70
    assert repo.count_finished_matches("ELC") == 60
