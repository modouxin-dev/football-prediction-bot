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


def _run(repo, args=None):
    sink = []
    ctx = type("C", (), {
        "application": type("A", (), {"bot_data": {"service": _Service(repo)}})(),
        "args": list(args or []),
    })()
    asyncio.run(backtest_cmd(_Update(sink), ctx))
    return "\n".join(sink)


def test_backtest_refuses_small_sample(tmp_path, monkeypatch):
    """已完赛 10 场 < 门槛：只报数字，不给命中率/判定。

    内置历史会补足样本，因此这里显式关掉它，才能测到「拦截」本身；
    「有内置历史时小样本不再拦截」由下一条用例覆盖。
    """
    monkeypatch.setattr("backtest_corpus.available_local_seasons",
                        lambda *a, **k: [])
    repo = _seed(str(tmp_path / "a.db"), 20, 10)
    out = _run(repo)
    assert "样本不足" in out
    assert "还差" in out
    assert "判定" not in out, "小样本上给判定等于给假结论"


def test_backtest_builtin_history_unblocks_small_db(tmp_path):
    """库内只有 10 场时，内置历史应把样本补到门槛以上。

    这是本次改动的目的：以前赛季初必然「样本不足」，现在立刻可评估。
    """
    repo = _seed(str(tmp_path / "a2.db"), 20, 10)
    out = _run(repo)
    assert "样本不足" not in out, "内置历史已补足样本，不该再拦截"
    assert "内置历史" in out, "样本来源应体现内置历史的贡献"
    assert "计入评估" in out


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


# ---- 模型口径一致性：/backtest 默认必须评估线上实际使用的模型 --------------

def test_default_variant_is_poisson_like_online():
    """线上 predict_match 不传 elo_home_advantage → 纯泊松。回测默认必须一致。"""
    from commands.admin import _parse_backtest_variant
    ctx = type("C", (), {})()                      # 完全没有 args 属性
    assert _parse_backtest_variant(ctx)[0] == "poisson"


def test_variant_arg_is_parsed():
    from commands.admin import _parse_backtest_variant
    for raw, want in [("elo", "elo"), ("ELO", "elo"), ("dc", "dc"), ("poisson", "poisson")]:
        ctx = type("C", (), {"args": [raw]})()
        assert _parse_backtest_variant(ctx)[0] == want, raw


def test_invalid_variant_falls_back_to_poisson():
    """非法参数不得炸命令，一律回退到线上口径。"""
    from commands.admin import _parse_backtest_variant
    for raw in ["xxx", "", None]:
        ctx = type("C", (), {"args": [raw]})()
        assert _parse_backtest_variant(ctx)[0] == "poisson", raw


def _synth(n=120, seed=7):
    import random
    rnd = random.Random(seed)
    teams = [f"T{i}" for i in range(8)]
    return [{
        "id": i, "utc_date": f"2026-01-{i % 28 + 1:02d}T15:00", "competition": "PL",
        "home_team_id": teams[i % 8], "away_team_id": teams[(i * 3 + 1) % 8],
        "home_score": rnd.randint(0, 3), "away_score": rnd.randint(0, 3),
        "status": "FT"} for i in range(n)]


def test_run_backtest_default_challenger_equals_baseline():
    """默认 variant 下挑战者就是基线本身——判定取哪一路都不再有歧义。"""
    from backtest import run_backtest
    c = run_backtest(_synth(), min_history=30)["comparison"]
    assert c["baseline"]["log_loss"] == c["challenger"]["log_loss"]
    assert c["baseline"]["n"] == c["challenger"]["n"]


def test_elo_remains_available_explicitly():
    """Elo 保留为显式 challenger，不删除。"""
    from backtest import run_backtest
    r = run_backtest(_synth(), min_history=30, variant="elo")
    assert r["comparison"]["challenger"]["n"] > 0


def test_dc_variant_returns_rho():
    """dc 变体曾因 BacktestReport 无 rho 属性而崩溃（回归锁）。"""
    from backtest import run_backtest
    r = run_backtest(_synth(), min_history=30, variant="dc")
    assert r["rho"] is not None
    assert r["comparison"]["challenger"]["n"] > 0


def test_backtest_default_output_marks_online_parity(tmp_path):
    """默认输出只列纯泊松一行并标明＝线上口径，不出现 Elo 对照行。"""
    repo = _seed(str(tmp_path / "e.db"), 100, 70)
    out = _run(repo)
    assert "（＝线上 /predict 口径）" in out
    assert "泊松+Elo" not in out


def test_variant_found_even_when_pasted_with_a_second_command(tmp_path):
    """手机端常见「/backtest\\n/backtest elo」一起粘贴：变体仍要生效。

    回归锁：过去只认 args[0]，此时 args=['/backtest','elo']，首位不是合法
    变体就静默退回默认，用户以为指定了 elo 却拿到纯泊松。
    """
    repo = _seed(str(tmp_path / "f.db"), 100, 70)
    out = _run(repo, ["/backtest", "elo"])
    assert "当前变体：泊松+Elo" in out
    assert "泊松+Elo" in out


def test_unknown_variant_is_reported_not_silently_ignored(tmp_path):
    """无法识别的参数必须显式告知，不能静默回退成默认值。"""
    repo = _seed(str(tmp_path / "g.db"), 100, 70)
    out = _run(repo, ["xyz"])
    assert "参数未识别" in out
    assert "xyz" in out
    assert "当前变体：纯泊松" in out


def test_explicit_poisson_keeps_single_row(tmp_path):
    """显式 poisson 与默认一致：单行输出、不出现 Elo 对照行。"""
    repo = _seed(str(tmp_path / "h.db"), 100, 70)
    out = _run(repo, ["poisson"])
    assert "（＝线上 /predict 口径）" in out
    assert "泊松+Elo" not in out
