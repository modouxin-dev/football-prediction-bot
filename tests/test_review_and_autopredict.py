"""每日复盘与无人值守预测的回归测试。

为什么单独成文件：
    复盘是「进步型机器人」的反馈回路，一旦静默失效（不推送 / 归因反了 /
    样本不足却报出 100%），整套自我改进就退化成自嗨。这些用例锁住的是
    「结论必须诚实」这件事，而不只是函数不报错。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from review import MIN_SAMPLE, daily_review, render_review


def _mk_db(tmp_path):
    """建一个最小 predictions 表，避免依赖整个 repository 初始化。"""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE predictions (
        fixture_id TEXT PRIMARY KEY, season INTEGER, league TEXT,
        home TEXT, away TEXT, kickoff TEXT, model_version TEXT, source TEXT,
        level_key TEXT, result TEXT, home_prob REAL, draw_prob REAL,
        away_prob REAL, best_score TEXT, created_at TEXT, inputs TEXT,
        actual_home INTEGER, actual_away INTEGER, settled_at TEXT)""")
    return db, conn


def _insert(conn, fid, *, hp, dp, ap, result, hs, as_, league="英超",
            source="API-Football", days_ago=1):
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO predictions (fixture_id, season, league, home, away, kickoff,"
        " model_version, source, level_key, result, home_prob, draw_prob, away_prob,"
        " best_score, created_at, inputs, actual_home, actual_away, settled_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, 2026, league, "H" + fid, "A" + fid,
         (now - timedelta(days=days_ago)).isoformat(), "v1", source, None,
         result, hp, dp, ap, "2-1", now.isoformat(), "{}", hs, as_,
         now.isoformat()))
    conn.commit()


# ============================================================================
# 一、空状态必须诚实
# ============================================================================

def test_empty_db_returns_empty_not_zero(tmp_path):
    """没有已结算样本时必须报 empty，绝不能报 0% 命中率。

    把「还没跑」显示成 0% 会让人以为模型彻底失灵，
    进而做出错误的调参决策。
    """
    db, _ = _mk_db(tmp_path)
    rv = daily_review(str(db), since_days=7)
    assert rv["status"] == "empty"
    assert rv["settled"] == 0
    assert "命中" not in render_review(rv)


# ============================================================================
# 二、归因正确性
# ============================================================================

def test_high_confidence_more_accurate_is_detected(tmp_path):
    """高信心档确实更准时，必须判定为「模型有自知之明」。"""
    db, conn = _mk_db(tmp_path)
    for i in range(20):  # 高信心 20 场全中
        _insert(conn, f"hi{i}", hp=0.60, dp=0.22, ap=0.18,
                result="主胜", hs=2, as_=1)
    for i in range(20):  # 低信心 20 场全错
        _insert(conn, f"lo{i}", hp=0.36, dp=0.33, ap=0.31,
                result="主胜", hs=1, as_=2)
    conn.close()

    rv = daily_review(str(db), since_days=7)
    assert rv["status"] == "ok"
    assert rv["settled"] == 40
    assert rv["confidence_meaningful"] is True
    assert rv["best_group"]["name"] == "高信心"
    text = render_review(rv)
    assert "有自知之明" in text


def test_confidence_inverted_is_flagged(tmp_path):
    """高信心反而更差时必须告警，不能粉饰成正常。

    这是概率标定出问题的直接证据，掩盖它就等于让模型一直错下去。
    """
    db, conn = _mk_db(tmp_path)
    for i in range(20):
        _insert(conn, f"hi{i}", hp=0.60, dp=0.22, ap=0.18,
                result="主胜", hs=1, as_=2)   # 高信心全错
    for i in range(20):
        _insert(conn, f"lo{i}", hp=0.36, dp=0.33, ap=0.31,
                result="主胜", hs=2, as_=1)   # 低信心全中
    conn.close()

    rv = daily_review(str(db), since_days=7)
    assert rv["confidence_meaningful"] is False
    assert "标定可能有问题" in render_review(rv)


def test_thin_group_not_reported_as_rate(tmp_path):
    """样本不足的分组不得参与最好/最差评选。

    3 场全中 = 100% 会诱使人相信一个根本不存在的规律。
    """
    db, conn = _mk_db(tmp_path)
    # 大组 A：命中率 30%，样本足
    for i in range(30):
        _insert(conn, f"big{i}", hp=0.36, dp=0.33, ap=0.31,
                result="主胜", hs=2 if i < 9 else 1, as_=1 if i < 9 else 2,
                league="英冠")
    # 大组 B：命中率 20%，样本足。
    # 必须有第二个样本充足的组，否则「不评最好」只是因为没有可比的组，
    # 根本没验证到「小组被排除」这件事。
    for i in range(30):
        _insert(conn, f"mid{i}", hp=0.36, dp=0.33, ap=0.31,
                result="主胜", hs=2 if i < 6 else 1, as_=1 if i < 6 else 2,
                league="英超")
    # 小组：全中但只有 3 场
    for i in range(3):
        _insert(conn, f"tiny{i}", hp=0.36, dp=0.33, ap=0.31,
                result="主胜", hs=2, as_=1, league="欧冠")
    conn.close()

    rv = daily_review(str(db), since_days=7)
    tiny = next(g for g in rv["by_league"] if g["name"] == "欧冠")
    assert tiny["thin"] is True
    assert tiny["n"] < MIN_SAMPLE
    # 小组不得当选「最好」
    assert rv["best_group"] is not None, "两个样本充足的组应当能评出最好/最差"
    assert rv["best_group"]["name"] != "欧冠"
    assert rv["worst_group"]["name"] != "欧冠"


# ============================================================================
# 三、基线对照
# ============================================================================

def test_baseline_computed(tmp_path):
    """必须给出基线，否则命中率只是个没有参照的孤零零数字。"""
    db, conn = _mk_db(tmp_path)
    for i in range(10):
        _insert(conn, f"b{i}", hp=0.5, dp=0.25, ap=0.25,
                result="主胜", hs=2, as_=1)
    conn.close()

    rv = daily_review(str(db), since_days=7)
    b = rv["baselines"]
    assert abs(b["always_home"] - 1.0) < 1e-9   # 全是主胜
    assert abs(b["random"] - 1 / 3) < 1e-9
    assert abs(rv["edge_vs_home"]) < 1e-9       # 与基线持平，无优势
    assert "基线" in render_review(rv)


# ============================================================================
# 四、result 非法时不该被算作未命中
# ============================================================================

def test_invalid_result_recovered_from_probs(tmp_path):
    """result 是 pending/NULL 的历史遗留行，应按概率方向恢复。

    否则这些场永远算未命中，整体命中率被悄悄压低，
    让人误以为模型在劣化。
    """
    db, conn = _mk_db(tmp_path)
    for i in range(10):
        _insert(conn, f"p{i}", hp=0.55, dp=0.25, ap=0.20,
                result="pending", hs=2, as_=1)  # 实际主胜，概率也指向主胜
    conn.close()

    rv = daily_review(str(db), since_days=7)
    assert rv["settled"] == 10
    assert rv["accuracy"] == 1.0


# ============================================================================
# 五、窗口过滤
# ============================================================================

def test_window_filters_old_records(tmp_path):
    """窗口外的老记录不进复盘，否则近期劣化会被历史成绩掩盖。"""
    db, conn = _mk_db(tmp_path)
    _insert(conn, "old1", hp=0.5, dp=0.25, ap=0.25,
            result="主胜", hs=2, as_=1, days_ago=30)
    _insert(conn, "new1", hp=0.5, dp=0.25, ap=0.25,
            result="主胜", hs=2, as_=1, days_ago=1)
    _insert(conn, "new2", hp=0.5, dp=0.25, ap=0.25,
            result="主胜", hs=1, as_=2, days_ago=2)
    conn.close()

    rv = daily_review(str(db), since_days=7)
    assert rv["settled"] == 2          # 只有近 7 天那两场
    assert abs(rv["accuracy"] - 0.5) < 1e-9


# ============================================================================
# 六、无人值守预测：源码级契约锁
# ============================================================================

def test_auto_predict_registered_independently_of_chat_id():
    """自动预测必须独立于 CHAT_ID 注册。

    它是数据闭环的地基：不落盘就永远没有命中率样本，
    复盘和校准都成了无米之炊。不能因为没配群号就整个停掉。
    """
    src = open("scheduler.py", encoding="utf-8").read()
    setup = src.split("def setup_scheduler")[1]
    chat_branch = setup.split("if settings.chat_id:")[1]
    assert "auto_predict_job" not in chat_branch


def test_settle_job_moved_out_of_chat_branch():
    """赛果结算不得依赖 CHAT_ID。

    历史缺陷：settle_job 被误放进 chat_id 分支，没配群号时
    一条都不结算，命中率统计因此永久停在 0 场。
    """
    src = open("scheduler.py", encoding="utf-8").read()
    setup = src.split("def setup_scheduler")[1]
    chat_branch = setup.split("if settings.chat_id:")[1]
    assert "settle_job" not in chat_branch


def test_settle_backfill_exists():
    """结算必须能回溯补取隔夜赛果，否则隔夜比赛永远匹配不上比分。"""
    src = open("service.py", encoding="utf-8").read()
    assert "_missing_result_days" in src
    assert "SETTLE_BACKFILL_DAYS" in src


def test_auto_predict_covers_champions_league():
    """欧冠必须纳入无人值守预测。

    用户手动点预测只覆盖当前选中联赛，欧冠因此长期无预测记录，
    看板上整片显示「未预测」。
    """
    src = open("scheduler.py", encoding="utf-8").read()
    assert "AUTO_PREDICT_EXTRA_LEAGUES" in src
    assert "(2,)" in src   # 2 = 欧洲冠军联赛


def test_auto_predict_only_pre_match():
    """自动预测只能算未开赛的比赛。

    给已开赛的比赛补预测等于事后诸葛亮，
    会直接毁掉复盘结论的可信度。
    """
    src = open("service.py", encoding="utf-8").read()
    fn = src.split("async def auto_predict_store")[1].split("async def _predict_one")[0]
    assert "_upcoming" in fn   # _upcoming 内部只保留 PRE_MATCH_STATUS
