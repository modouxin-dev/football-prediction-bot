"""回测语料（库内赛果 + 内置历史 CSV）合并的测试。

重点验证三件事，缺一不可：
1. 内置历史真的并进来了（样本量级提升）；
2. 队 id 与库内统一（否则走前回测接不上历史，等于白合并）；
3. 同一场比赛不会被算两次（重复计数会让 Log Loss 失真）。
"""

from __future__ import annotations

from backtest import run_backtest
from backtest_corpus import fixture_to_match, known_team_ids, load_corpus
from football_data_uk import available_local_seasons
from repository import PredictionRepository


def _seed(repo, *, fixture_id: int, date: str, home_id: str, home_name: str,
          away_id: str, away_name: str, hs: int, as_: int,
          competition: str = "PL", season: int = 2023) -> None:
    """直接插一行已完赛比赛（绕过 API，纯数据准备）。"""
    conn = repo._connect()
    conn.execute(
        "INSERT INTO matches (id, competition_code, season, utc_date, status, "
        "matchday, home_team_id, home_team_name, away_team_id, away_team_name, "
        "home_score, away_score, source, raw_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fixture_id, competition, season, date, "FINISHED", 1,
         home_id, home_name, away_id, away_name, hs, as_,
         "test", None, "2026-01-01T00:00:00"),
    )
    conn.commit()


def _find(matches, date_prefix: str, home_id: str, away_id: str):
    for m in matches:
        if (m.get("utc_date") or "").startswith(date_prefix) \
                and str(m["home_team_id"]) == str(home_id) \
                and str(m["away_team_id"]) == str(away_id):
            return m
    return None


# ---- 1. 内置历史并入 -------------------------------------------------------

def test_builtin_history_files_exist():
    """前置条件：三个赛季 CSV 必须真的在镜像里，否则后面全是空谈。"""
    seasons = available_local_seasons()
    assert seasons == [2023, 2024, 2025], f"内置赛季异常: {seasons}"


def test_history_merged_into_empty_db(tmp_path):
    """库内为空时，语料应全部来自内置历史。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, info = load_corpus(repo, "PL")

    assert info["db"] == 0
    assert info["history"] > 1000, f"内置历史太少: {info['history']}"
    assert info["total"] == info["history"]
    assert info["seasons"] == [2023, 2024, 2025]


def test_every_row_has_scores(tmp_path):
    """回测器会跳过无比分的行；若大量缺失，样本量就是虚的。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, _ = load_corpus(repo, "PL")

    assert matches, "语料为空"
    for m in matches:
        assert isinstance(m["home_score"], int)
        assert isinstance(m["away_score"], int)
        assert m["home_team_id"] and m["away_team_id"]


# ---- 2. 队 id 统一（合并的价值所在）---------------------------------------

def test_csv_team_id_reuses_db_id(tmp_path):
    """CSV 的 "Man City" 必须复用库内 "Manchester City FC" 的 id。

    否则同一支球队在语料里有两个 id，走前回测会当成两支不同的队，
    历史完全接不上——合并就白做了。
    """
    repo = PredictionRepository(str(tmp_path / "c.db"))
    # 先造一条库内记录，让反查表知道这两队的官方 id（日期错开，避免去重）
    _seed(repo, fixture_id=900001, date="2023-05-01T12:00:00Z",
          home_id="328", home_name="Burnley FC",
          away_id="65", away_name="Manchester City FC", hs=0, as_=1)

    known = known_team_ids(repo)
    assert known.get("Burnley FC") == "328"
    assert known.get("Manchester City FC") == "65"

    matches, _ = load_corpus(repo, "PL")
    row = _find(matches, "2023-08-11", "328", "65")
    assert row is not None, "内置 CSV 里 2023-08-11 的 Burnley vs Man City 未找到"
    assert row["home_team_id"] == "328"
    assert row["away_team_id"] == "65"


def test_unknown_team_falls_back_to_slug(tmp_path):
    """库里没有的球队 → 退回 slug 兜底，不能崩。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, _ = load_corpus(repo, "PL")
    # 不配反查表时也应能正常解析（全走 slug）
    assert any(str(m["home_team_id"]).startswith("fduk-") for m in matches)


# ---- 3. 去重 ---------------------------------------------------------------

def test_same_match_not_counted_twice(tmp_path):
    """库内与 CSV 覆盖同一场时，只保留库内那行。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    _seed(repo, fixture_id=900002, date="2023-08-11T20:00:00Z",
          home_id="328", home_name="Burnley FC",
          away_id="65", away_name="Manchester City FC", hs=0, as_=3)

    matches, info = load_corpus(repo, "PL")
    rows = [m for m in matches
            if (m.get("utc_date") or "").startswith("2023-08-11")
            and str(m["home_team_id"]) == "328"
            and str(m["away_team_id"]) == "65"]

    assert len(rows) == 1, f"同一场被算了 {len(rows)} 次"
    assert rows[0]["source"] == "db", "应保留库内那行（真实 API 数据更全）"


# ---- 4. 排序与开关 ---------------------------------------------------------

def test_sorted_by_time(tmp_path):
    """走前回测要求时间正序，乱序会让「用之前的历史」变成前视偏差。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, _ = load_corpus(repo, "PL")
    dates = [m.get("utc_date") or "" for m in matches]
    assert dates == sorted(dates), "语料未按时间正序"


def test_history_can_be_disabled(tmp_path):
    """include_history=False 应退化为旧的「只有库内」。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    _seed(repo, fixture_id=900003, date="2023-08-11T20:00:00Z",
          home_id="328", home_name="Burnley FC",
          away_id="65", away_name="Manchester City FC", hs=0, as_=3)

    matches, info = load_corpus(repo, "PL", include_history=False)
    assert info["total"] == 1
    assert info["history"] == 0


# ---- 5. 端到端：回测器真能吃这份语料 --------------------------------------

def test_backtester_runs_on_corpus(tmp_path):
    """合并出来的语料必须能被回测器直接消费，否则只是好看的数字。"""
    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, info = load_corpus(repo, "PL")

    result = run_backtest(matches)
    n = result["comparison"]["challenger"]["n"]

    assert n > 1000, f"评估样本太少: {n}"
    assert result["comparison"]["challenger"]["log_loss"] is not None


def test_fixture_to_match_skips_unplayed():
    """未赛（无比分）的行必须被跳过，不能混进样本。"""
    fx = {
        "id": "1", "utcDate": "2023-08-11T20:00:00Z", "competition": "PL",
        "homeTeam": {"id": "1", "name": "A"}, "awayTeam": {"id": "2", "name": "B"},
        "score": {"fullTime": {"home": None, "away": None}},
    }
    assert fixture_to_match(fx) is None


# ============================================================================
# 多联赛语料（五大联赛合并）
# ============================================================================
# 背景：生产 LEAGUE_IDS 覆盖英超/西甲/意甲/德甲/法甲，但回测语料长期只有
# 英超（DEFAULT_DIV="E0"）。于是"西甲的回测结论"其实是英超的结论——
# 模型可信度存在覆盖盲区。本组用例保证合并后每个分区都真的贡献了样本。

from football_data_uk import DIV_TO_COMPETITION  # noqa: E402
import football_data_uk as _fdu  # noqa: E402
from backtest_corpus import (DEFAULT_DIVS, available_divs,  # noqa: E402
                             div_for_competition)

CSV_HEADER = "Date,HomeTeam,AwayTeam,FTHG,FTAG"


def _write_csv(d, div, season, n=6):
    """在临时目录写一份最小可用的历史 CSV（n 场）。"""
    rows = [CSV_HEADER]
    for i in range(n):
        day = f"{10 + i // 3:02d}/{season % 100 + 0:02d}/{season}" \
            if False else f"{10 + i:02d}/08/{season}"
        rows.append(f"{day},TeamA{i},TeamB{i},{i % 3},{i % 2}")
    (d / f"{div}_{season}.csv").write_text("\n".join(rows) + "\n",
                                           encoding="utf-8")


def test_default_divs_cover_five_leagues():
    """五联赛集合必须齐，且西甲是 SP1 而不是 E1。

    E1 是英冠（ELC）。把它当西甲不会报错，只会静默取回英冠赛果，
    回测结论随之失真——这是本条用例存在的唯一理由。
    """
    assert list(DEFAULT_DIVS) == ["E0", "SP1", "I1", "D1", "F1"]
    assert DIV_TO_COMPETITION["SP1"] == "PD"    # 西甲
    assert DIV_TO_COMPETITION["I1"] == "SA"     # 意甲
    assert DIV_TO_COMPETITION["D1"] == "BL1"    # 德甲
    assert DIV_TO_COMPETITION["F1"] == "FL1"    # 法甲
    assert DIV_TO_COMPETITION["E1"] == "ELC"    # 英冠 ≠ 西甲
    assert "E1" not in DEFAULT_DIVS


def test_div_for_competition_roundtrip():
    assert div_for_competition("PL") == "E0"
    assert div_for_competition("PD") == "SP1"
    assert div_for_competition("SA") == "I1"
    assert div_for_competition("BL1") == "D1"
    assert div_for_competition("FL1") == "F1"
    assert div_for_competition("不存在的联赛") == "E0"   # 兜底不炸


def test_available_divs_only_counts_real_files(tmp_path, monkeypatch):
    """只声明支持没用：data/history 里没有 CSV 的分区不算数。"""
    monkeypatch.setattr(_fdu, "history_dir", lambda: tmp_path)
    _write_csv(tmp_path, "E0", 2024)
    assert available_divs() == ["E0"]
    _write_csv(tmp_path, "SP1", 2024)
    assert available_divs() == ["E0", "SP1"]


def test_multi_div_merges_every_league(tmp_path, monkeypatch):
    """五个分区都真的并进来了——每个分区样本数都必须 > 0。"""
    monkeypatch.setattr(_fdu, "history_dir", lambda: tmp_path)
    for div in DEFAULT_DIVS:
        _write_csv(tmp_path, div, 2024, n=6)

    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, info = load_corpus(repo, "", divs=list(DEFAULT_DIVS))

    assert info["by_div"], "多联赛未产生分区统计"
    assert len(info["by_div"]) == 5, f"分区数不足: {info['by_div']}"
    for div in DEFAULT_DIVS:
        assert info["by_div"].get(div, 0) == 6, \
            f"{div} 样本缺失: {info['by_div']}"
    # 五联赛各自的 competition_code 都要出现，否则去重键会互相碰撞
    assert set(info["competitions"]) == {
        DIV_TO_COMPETITION[d] for d in DEFAULT_DIVS}
    assert info["total"] == 30


def test_multi_div_no_cross_league_dedup(tmp_path, monkeypatch):
    """同日同对手但在不同联赛，不能被当成同一场去重掉。

    CSV 的稳定 id 由 (日期, 主队, 客队) 生成，不含联赛；
    去重键必须带上 competition，否则跨联赛的同名对阵会互相吞掉。
    """
    monkeypatch.setattr(_fdu, "history_dir", lambda: tmp_path)
    for div in ("E0", "SP1"):
        (tmp_path / f"{div}_2024.csv").write_text(
            CSV_HEADER + "\n10/08/2024,SameA,SameB,1,0\n", encoding="utf-8")

    repo = PredictionRepository(str(tmp_path / "c.db"))
    matches, info = load_corpus(repo, "", divs=["E0", "SP1"])

    assert info["total"] == 2, f"跨联赛被误去重: {info}"
    assert info["by_div"] == {"E0": 1, "SP1": 1}


def test_single_div_keeps_backward_compat(tmp_path, monkeypatch):
    """显式传 div 时行为与改造前一致（旧调用不受影响）。"""
    monkeypatch.setattr(_fdu, "history_dir", lambda: tmp_path)
    for div in DEFAULT_DIVS:
        _write_csv(tmp_path, div, 2024, n=6)

    repo = PredictionRepository(str(tmp_path / "c.db"))
    _, info = load_corpus(repo, "PL", div="E0")
    assert info["by_div"] == {"E0": 6}
    assert info["total"] == 6


def test_by_div_is_observable_not_claimed(tmp_path, monkeypatch):
    """``by_div`` 必须反映真实入库样本，而不是"声称支持"的联赛清单。

    这条用例同时是验收命令 ``pytest -k by_div`` 的落点：
    多联赛断言原先都写在 test_multi_div_* 里，``-k by_div`` 一条都选不中，
    验收跑出来是 "28 deselected"，看起来像通过、实际什么都没验。
    补一条名字里带 by_div 的用例，让验收命令真的跑到东西。

    关键语义：显式请求了五个分区时，``by_div`` 必须**把缺数据的分区也列成 0**，
    而不是悄悄略过——"SP1: 0" 本身就是盲区的证据，藏起来才会让人误以为
    五联赛都验过了。（若改用自动模式 available_divs()，则只列真有 CSV 的分区。）
    """
    monkeypatch.setattr(_fdu, "history_dir", lambda: tmp_path)

    # 阶段一：只放英超 CSV（当前 data/history 的真实状态）
    _write_csv(tmp_path, "E0", 2024, n=6)
    repo = PredictionRepository(str(tmp_path / "c.db"))
    _, info = load_corpus(repo, "", divs=list(DEFAULT_DIVS))
    assert info["by_div"] == {"E0": 6, "SP1": 0, "I1": 0, "D1": 0, "F1": 0}, \
        f"盲区未暴露（应为 0 而非缺失）: {info['by_div']}"

    # 阶段二：补齐另四个联赛后，五个分区都要真的出现且非零
    for div in ("SP1", "I1", "D1", "F1"):
        _write_csv(tmp_path, div, 2024, n=6)
    repo2 = PredictionRepository(str(tmp_path / "c2.db"))
    _, info2 = load_corpus(repo2, "", divs=list(DEFAULT_DIVS))
    assert len(info2["by_div"]) == 5, f"分区数不足: {info2['by_div']}"
    assert info2["total"] == 30
