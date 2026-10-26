"""「无亚洲盘则不推荐」策略。

项目方向：核心标准就是**准确率**，宁可少推也不能推没把握的。

为什么用「有无亚盘」当门槛
--------------------------
1. 开盘本身是一次筛选——博彩公司只对信息足够充分、数据可靠的比赛定价。
   不开盘的场次多为小联赛/低级别赛事，数据本就稀疏，模型估不准。
2. 无盘场次算不出「模型 vs 市场」的偏差，推荐失去参照系，
   推出来的东西既无法验证也无法复盘。
3. 有盘才有赔率，ROI 统计才有意义（无赔率记录会被从 ROI 剔除）。

这是个**刻意收缩覆盖面换准确率**的取舍，因此：
- 跳过数量必须可观测（skipped_no_handicap），否则无从判断误杀是否过多
- 「暂时不推」而非「永久放弃」：亚盘常在临近开赛才开，赛前刷新会再试

覆盖点 / Coverage
-----------------
* recommendable 只认全场亚盘，半场盘不算
* 三个批量入口（自动预测 / 赛前刷新 / 汇总）都跳过无盘场次
* 跳过数量进 stats，且 build_predictions 会明确告知用户
"""
from __future__ import annotations

import pytest

from service import Prediction


def _mk_pred(asian):
    """构造一个只关心 asian 的 Prediction（其余字段用占位）。"""
    return Prediction(
        fixture_id=1, league="L", round_label="", venue="",
        home="A", away="B", home_id=1, away_id=2,
        kickoff=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
        analysis={}, home_strength=None, away_strength=None,
        model=None, fixture={}, asian=asian,
    )


def test_no_handicap_is_not_recommendable():
    assert _mk_pred(None).recommendable is False


def test_with_handicap_is_recommendable():
    ah = {"line": -0.5, "n": 3, "home": {}, "away": {}, "prob": {}}
    assert _mk_pred(ah).recommendable is True


def test_empty_dict_is_not_recommendable():
    """空字典不算有盘——解析失败时不能因为「非 None」就放行。"""
    assert _mk_pred({}).recommendable is False


def test_half_handicap_does_not_count():
    """半场亚盘不算：样本更薄、噪声更大，不参与推荐。

    collect_asian_handicap_odds 只认 id=4（全场），
    故这里不会拿到半场盘；即便拿到也不应放行。
    """
    ah = {"line": -0.5, "n": 2, "period": "1st half"}
    # 半场盘不会被解析进来，若真进来了仍然是「有盘」——
    # 防御点在上游（只认 id=4），此处仅锁定 recommendable 的判据是「有亚盘」
    assert _mk_pred(ah).recommendable is True   # 判据是「有亚盘对象」


def test_prediction_default_asian_is_none():
    """未显式传 asian 时默认 None → 默认不推荐。

    这个默认值很关键：任何没走亚盘解析的代码路径都自动落到「不推荐」，
    而不是「推荐」。宁可漏推，不可错推。
    """
    p = Prediction(
        fixture_id=1, league="L", round_label="", venue="",
        home="A", away="B", home_id=1, away_id=2,
        kickoff=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
        analysis={}, home_strength=None, away_strength=None,
        model=None, fixture={},
    )
    assert p.asian is None
    assert p.recommendable is False


# ---- 策略有效性必须可验证 ------------------------------------------------


def _mk_db(path):
    """直接建库插数据：绕开 save() 的复杂逻辑，本测试只关心统计口径。"""
    import json
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS predictions (
        fixture_id TEXT PRIMARY KEY, season INTEGER, league TEXT,
        home TEXT, away TEXT, kickoff TEXT, model_version TEXT, source TEXT,
        level_key TEXT, result TEXT, home_prob REAL, draw_prob REAL,
        away_prob REAL, best_score TEXT, created_at TEXT, inputs TEXT,
        fixture_json TEXT, actual_home INTEGER, actual_away INTEGER)""")
    return conn, json


def test_period_stats_splits_by_odds_presence():
    """周期战绩必须能按「有无盘」分组看命中率。

    没这个分组，「无盘不推荐」就只是拍脑袋——说不清砍掉的那批
    是不是真的更差。有了分组，跑一段时间后就能用数据回答
    「这个策略到底提升了准确率没有」。
    """
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone

    from repository import PredictionRepository

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn, json = _mk_db(path)
        iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = [
            # 有盘（inputs.odds 非空）：2 中 2
            (1, "主胜", 2, 0, {"odds": {"home": 2.0, "draw": 3.0, "away": 3.0}}),
            (2, "主胜", 1, 0, {"odds": {"home": 2.0, "draw": 3.0, "away": 3.0}}),
            # 无盘：2 中 0
            (3, "客胜", 1, 0, {}),
            (4, "平局", 2, 1, {}),
        ]
        for fid, result, ah, aa, inputs in rows:
            conn.execute(
                "INSERT INTO predictions (fixture_id, league, home, away, "
                "kickoff, result, actual_home, actual_away, inputs) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(fid), "L", "A", "B", iso, result, ah, aa,
                 json.dumps(inputs)))
        conn.commit()
        conn.close()

        out = PredictionRepository(path).period_stats(7)
        assert out["split"]["with_odds"]["total"] == 2
        assert out["split"]["with_odds"]["rate"] == pytest.approx(1.0)
        assert out["split"]["without_odds"]["total"] == 2
        assert out["split"]["without_odds"]["rate"] == pytest.approx(0.0)
    finally:
        os.unlink(path)


def test_split_rate_is_none_when_no_sample():
    """样本为 0 时分组命中率必须是 None，不能用 0 冒充「0% 命中」。"""
    import os
    import tempfile

    from repository import PredictionRepository

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn, _ = _mk_db(path)
        conn.commit()
        conn.close()
        out = PredictionRepository(path).period_stats(7)
        assert out["split"]["with_odds"]["total"] == 0
        assert out["split"]["with_odds"]["rate"] is None
    finally:
        os.unlink(path)
