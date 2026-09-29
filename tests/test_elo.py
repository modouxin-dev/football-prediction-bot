"""第 3 阶段：Elo 评分 / Elo ratings.

纯函数要求 100% 可测；引擎部分重点验证幂等性与零和性质。
"""
from elo import (
    DEFAULT_RATING, ELO_CLAMP, HOME_ADVANTAGE, K_BASE, K_NEW_TEAM,
    EloEngine, calculate_elo, elo_multiplier, expected_score,
    goal_diff_multiplier, k_factor_for, outcome_from_score,
)
from repository import PredictionRepository


# ============================================================================
# 一、纯函数
# ============================================================================

def test_expected_score_equal_ratings_is_half():
    assert abs(expected_score(1500.0, 1500.0) - 0.5) < 1e-9


def test_expected_score_monotonic():
    """分差越大，期望胜率越高。"""
    prev = 0.0
    for diff in (0, 50, 100, 200, 400):
        val = expected_score(1500 + diff, 1500)
        assert val > prev
        prev = val


def test_expected_score_400_gap_is_about_91_percent():
    """Elo 的定义：每差 400 分，期望胜率约 0.91。"""
    val = expected_score(1900.0, 1500.0)
    assert 0.90 < val < 0.92


def test_expected_score_symmetric():
    assert abs(expected_score(1600, 1400) + expected_score(1400, 1600) - 1.0) < 1e-9


def test_outcome_from_score():
    assert outcome_from_score(2, 1) == 1.0
    assert outcome_from_score(0, 3) == 0.0
    assert outcome_from_score(1, 1) == 0.5


def test_k_factor_new_team_higher():
    assert k_factor_for(0) == K_NEW_TEAM
    assert k_factor_for(9) == K_NEW_TEAM
    assert k_factor_for(10) == K_BASE
    assert k_factor_for(100) == K_BASE


def test_goal_diff_multiplier():
    assert goal_diff_multiplier(1, 0) == 1.0     # 1 球小胜
    assert goal_diff_multiplier(1, 1) == 1.0     # 平局
    assert goal_diff_multiplier(3, 1) == 1.5     # 2 球
    assert goal_diff_multiplier(4, 1) == 1.75    # 3 球
    assert goal_diff_multiplier(5, 1) == 1.875   # 4 球


def test_goal_diff_multiplier_symmetric():
    assert goal_diff_multiplier(0, 2) == goal_diff_multiplier(2, 0)


def test_calculate_elo_is_zero_sum():
    """Elo 是零和的：一方涨多少，另一方就跌多少。"""
    a, b = calculate_elo(1500.0, 1500.0, 1.0, 20.0)
    assert abs((a - 1500.0) + (b - 1500.0)) < 1e-9
    assert a > 1500.0 > b


def test_calculate_elo_underdog_wins_more():
    """弱队赢球拿到的分，应多于强队赢球拿到的分。"""
    _, weak_gain = calculate_elo(1400.0, 1600.0, 1.0, 20.0)[0], None
    weak_after, strong_after = calculate_elo(1400.0, 1600.0, 1.0, 20.0)
    gain_underdog = weak_after - 1400.0
    fav_after, _ = calculate_elo(1600.0, 1400.0, 1.0, 20.0)
    gain_fav = fav_after - 1600.0
    assert gain_underdog > gain_fav > 0


def test_calculate_elo_draw_favors_lower_rated():
    """平局时低分方小涨、高分方小跌。"""
    hi, lo = calculate_elo(1600.0, 1400.0, 0.5, 20.0)
    assert hi < 1600.0 < lo + 1e-9 or hi < 1600.0
    assert lo > 1400.0
    assert hi < 1600.0


def test_calculate_elo_home_advantage_reduces_gain():
    """加了主场优势后，主队赢球拿的分更少（因为本就被看好）。"""
    no_adv, _ = calculate_elo(1500.0, 1500.0, 1.0, 20.0, home_advantage=0.0)
    with_adv, _ = calculate_elo(1500.0, 1500.0, 1.0, 20.0, home_advantage=HOME_ADVANTAGE)
    assert with_adv < no_adv


def test_calculate_elo_multiplier_amplifies():
    plain, _ = calculate_elo(1500.0, 1500.0, 1.0, 20.0, multiplier=1.0)
    big, _ = calculate_elo(1500.0, 1500.0, 1.0, 20.0, multiplier=1.75)
    assert big > plain


# ---- elo_multiplier（Elo 影响预测的入口） ------------------------------------

def test_elo_multiplier_equal_is_one():
    assert abs(elo_multiplier(1500.0, 1500.0, home_advantage=0.0) - 1.0) < 1e-9


def test_elo_multiplier_stronger_home_above_one():
    assert elo_multiplier(1700.0, 1500.0, home_advantage=0.0) > 1.0


def test_elo_multiplier_stronger_away_below_one():
    assert elo_multiplier(1500.0, 1700.0, home_advantage=0.0) < 1.0


def test_elo_multiplier_home_advantage_helps_home():
    """同样实力，主队因主场优势应得到 >1 的系数。"""
    assert elo_multiplier(1500.0, 1500.0, home_advantage=HOME_ADVANTAGE) > 1.0


def test_elo_multiplier_is_clamped():
    """核心安全设计：极端分差也不能把 λ 带崩。"""
    low, high = ELO_CLAMP
    for diff in (500, 1000, 5000, -500, -5000):
        m = elo_multiplier(1500.0 + diff, 1500.0, home_advantage=0.0)
        assert low <= m <= high


def test_elo_multiplier_symmetric_around_one():
    """主客互换，两系数之和恒为 2 —— 即 λ主+λ客 不变。

    含义：Elo 只在两队之间重新分配进球期望，不改变总进球数，
    因此不会影响大小球（over/under）市场的判断。
    （注意不变量是「和为 2」，不是「积为 1」——后者会让总进球随分差漂移。）
    """
    m1 = elo_multiplier(1650.0, 1500.0, home_advantage=0.0)
    m2 = elo_multiplier(1500.0, 1650.0, home_advantage=0.0)
    assert abs((m1 + m2) - 2.0) < 1e-9


# ============================================================================
# 二、引擎（与 repository 协作）
# ============================================================================

def engine(tmp_path, **kw):
    repo = PredictionRepository(str(tmp_path / "elo.db"))
    return EloEngine(repo=repo, competition="PL", **kw), repo


def test_engine_new_team_starts_at_base(tmp_path):
    eng, _ = engine(tmp_path)
    assert eng.rating_of("1") == DEFAULT_RATING


def test_engine_win_updates_and_persists(tmp_path):
    eng, repo = engine(tmp_path)
    detail = eng.apply_match(1001, "1", "2", 2, 0)
    assert detail["applied"] is True
    assert detail["home"]["after"] > detail["home"]["before"]
    assert detail["away"]["after"] < detail["away"]["before"]
    # 落盘验证
    saved = repo.elo_ratings("PL")
    assert abs(saved["1"] - detail["home"]["after"]) < 1e-9


def test_engine_idempotent_on_repeat(tmp_path):
    """同一场比赛算两次，第二次必须被跳过（幂等性核心）。"""
    eng, repo = engine(tmp_path)
    first = eng.apply_match(1002, "1", "2", 3, 1)
    second = eng.apply_match(1002, "1", "2", 3, 1)
    assert first["applied"] is True
    assert second["applied"] is False
    # 评分不能被第二次调用改变
    assert abs(repo.elo_ratings("PL")["1"] - first["home"]["after"]) < 1e-9
    # elo_log 里每队只能有一条
    assert len(repo.elo_history(1002)) == 2  # 主队一条 + 客队一条


def test_engine_idempotent_across_restart(tmp_path):
    """重启（新建引擎）后重复触发同样要跳过——这是防止评分虚高的关键。"""
    db = tmp_path / "elo.db"
    eng1 = EloEngine(repo=PredictionRepository(str(db)), competition="PL")
    eng1.apply_match(1003, "1", "2", 2, 1)
    after_first = eng1.repo.elo_ratings("PL")["1"]

    eng2 = EloEngine(repo=PredictionRepository(str(db)), competition="PL")
    res = eng2.apply_match(1003, "1", "2", 2, 1)
    assert res["applied"] is False
    assert abs(eng2.repo.elo_ratings("PL")["1"] - after_first) < 1e-9


def test_engine_idempotency_is_not_id_ordering(tmp_path):
    """关键回归：幂等靠集合成员检查，不是 ID 大小比较。

    fixture_id 不是单调时间戳——小 ID 的比赛可能更晚结束（补赛/延期）。
    若用 `id <= last_id` 判断，下面这场会被错误跳过。
    """
    eng, _ = engine(tmp_path)
    eng.apply_match(9000, "1", "2", 1, 0)          # 大 ID 先结束
    later = eng.apply_match(1000, "1", "2", 1, 0)  # 小 ID 后结束
    assert later["applied"] is True, "小 ID 的比赛被错误跳过——幂等判断写成了顺序比较"


def test_engine_dry_run_does_not_write(tmp_path):
    eng, repo = engine(tmp_path)
    eng.apply_match(1004, "1", "2", 2, 0, dry_run=True)
    assert repo.elo_ratings("PL") == {}
    assert repo.elo_is_processed(1004) is False


def test_engine_replay_batch(tmp_path):
    eng, repo = engine(tmp_path)
    matches = [
        {"fixture_id": 1, "home_team_id": "A", "away_team_id": "B",
         "home_score": 2, "away_score": 0},
        {"fixture_id": 2, "home_team_id": "B", "away_team_id": "A",
         "home_score": 1, "away_score": 1},
        {"fixture_id": 3, "home_team_id": "A", "away_team_id": "B",
         "home_score": 0, "away_score": 3},
    ]
    done = eng.replay(matches)
    assert done == 3
    ratings = repo.elo_ratings("PL")
    # B 一胜一平一负，A 一胜一平一负但净胜球吃亏，B 应更高
    assert ratings["B"] > ratings["A"]


def test_engine_replay_skips_incomplete(tmp_path):
    """比分缺失的比赛不能计入。"""
    eng, _ = engine(tmp_path)
    matches = [
        {"fixture_id": 1, "home_team_id": "A", "away_team_id": "B",
         "home_score": None, "away_score": None},
    ]
    assert eng.replay(matches) == 0


def test_engine_replay_is_idempotent(tmp_path):
    """整批重放两次，第二次一场都不该计入。"""
    db = tmp_path / "elo.db"
    matches = [
        {"fixture_id": 1, "home_team_id": "A", "away_team_id": "B",
         "home_score": 2, "away_score": 1},
    ]
    e1 = EloEngine(repo=PredictionRepository(str(db)), competition="PL")
    assert e1.replay(matches) == 1
    e2 = EloEngine(repo=PredictionRepository(str(db)), competition="PL")
    assert e2.replay(matches) == 0


def test_engine_replay_never_raises_on_bad_row(tmp_path):
    """单场数据异常不能中断整批回算。"""
    eng, _ = engine(tmp_path)
    matches = [
        {"fixture_id": 1, "home_team_id": "A", "away_team_id": "B",
         "home_score": "bad", "away_score": 1},
        {"fixture_id": 2, "home_team_id": "A", "away_team_id": "B",
         "home_score": 1, "away_score": 0},
    ]
    assert eng.replay(matches) == 1


def test_engine_rating_bounded(tmp_path):
    """评分不能无限膨胀或缩水。"""
    eng, _ = engine(tmp_path)
    for i in range(60):
        eng.apply_match(2000 + i, "1", "2", 6, 0)
    ratings = eng.repo.elo_ratings("PL")
    assert 1200.0 <= ratings["1"] <= 2400.0
    assert ratings["2"] >= 1200.0
