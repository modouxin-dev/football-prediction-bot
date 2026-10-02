"""内置历史数据（data/history/*.csv）加载器的测试。

这些 CSV 随镜像打包，是线上唯一不依赖出口网络的历史数据来源。
测试重点：能读出来、结构能被 repository 直接吃、赔率去水正确。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

import football_data_uk as fduk

EXPECTED_SEASONS = (2023, 2024, 2025)
EXPECTED_PER_SEASON = 380


# --------------------------------------------------------------------------
# 目录与可用性
# --------------------------------------------------------------------------
def test_history_dir_exists():
    assert fduk.history_dir().is_dir()


def test_available_seasons():
    got = fduk.available_local_seasons()
    assert set(EXPECTED_SEASONS).issubset(set(got))


def test_missing_season_returns_empty():
    """不存在的赛季必须返回空列表而不是抛异常（调用方按无数据降级）。"""
    assert fduk.load_local(1800) == []


def test_missing_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(fduk, "history_dir", lambda: tmp_path)
    assert fduk.load_local(2023) == []
    assert fduk.available_local_seasons() == []


# --------------------------------------------------------------------------
# 解析结果
# --------------------------------------------------------------------------
def test_each_season_has_full_fixtures():
    for season in EXPECTED_SEASONS:
        fx = fduk.load_local(season)
        assert len(fx) == EXPECTED_PER_SEASON, f"{season} 场次不对"


def test_load_all_count_and_order():
    allfx = fduk.load_local_all()
    assert len(allfx) == EXPECTED_PER_SEASON * len(EXPECTED_SEASONS)
    dates = [f["utcDate"] for f in allfx]
    assert dates == sorted(dates), "合并后必须按时间升序"


def test_fixture_shape():
    """结构必须复用备用源的扁平格式，repository 才能直接吃。"""
    fx = fduk.load_local(2023)[0]
    for key in ("id", "utcDate", "status", "homeTeam", "awayTeam", "score", "competition"):
        assert key in fx
    assert fx["homeTeam"]["id"] and fx["awayTeam"]["id"]
    assert fx["status"] == "FINISHED"
    assert isinstance(fx["score"]["fullTime"]["home"], int)


def test_all_finished():
    assert all(f["status"] == "FINISHED" for f in fduk.load_local_all())


# --------------------------------------------------------------------------
# 赔率
# --------------------------------------------------------------------------
def test_all_rows_have_odds():
    fx = fduk.load_local_all()
    missing = [f for f in fx if not f.get("odds")]
    assert not missing, f"{len(missing)} 场缺少赔率"


def test_market_probs_sum_to_one():
    """去水后三项概率之和必须精确为 1，否则和市场比 LogLoss 会系统性偏差。"""
    for f in fduk.load_local_all():
        probs = f["marketProbabilities"]
        assert abs(sum(probs.values()) - 1.0) < 1e-9


def test_market_probs_are_positive():
    for f in fduk.load_local_all():
        assert all(v > 0 for v in f["marketProbabilities"].values())


# --------------------------------------------------------------------------
# 数据文件本身
# --------------------------------------------------------------------------
def test_data_files_committed():
    d = fduk.history_dir()
    for season in EXPECTED_SEASONS:
        p = d / f"E0_{season}.csv"
        assert p.is_file(), f"缺少 {p.name}"
        assert p.stat().st_size > 10_000


def test_no_future_rows_beyond_season():
    """每份 CSV 的日期必须落在该赛季区间内，防止串季。"""
    for season in EXPECTED_SEASONS:
        fx = fduk.load_local(season)
        years = {f["utcDate"][:4] for f in fx}
        assert years <= {str(season), str(season + 1)}, f"{season} 出现越界年份 {years}"
