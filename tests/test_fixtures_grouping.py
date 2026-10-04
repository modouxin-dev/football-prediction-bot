"""赛程列表的联赛聚堆排序（不联网，全部使用仿真数据）。

为什么需要这个测试：
    赛程视图是按联赛打印分组标题的——只在联赛切换时打印一次。若只按开赛
    时间排序，五大联赛的比赛会交替出现，同一个联赛标题被反复打印，分组
    等于失效。「接入了多个联赛」这件事在界面上就看不出来了。
"""
from datetime import datetime, timedelta, timezone

from config import load_settings
from service import PredictionService
from tests.sample_data import FakeAPI, fixture

ENV = {"TELEGRAM_TOKEN": "123456:TEST-TOKEN", "RAPID_API_KEY": "k",
       "CHAT_ID": "555", "SEASON": "2026", "ADMIN_ID": "555"}
SETTINGS = load_settings(ENV)

BASE = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _fx(fid, lid, minutes):
    """造一场指定联赛、指定开赛时间的比赛。"""
    fx = fixture(fid, fid * 10, f"Home{fid}", fid * 10 + 1, f"Away{fid}",
                 BASE + timedelta(minutes=minutes))
    fx["league"] = {"id": lid, "name": f"League{lid}", "round": "Regular Season - 1"}
    return fx


def test_same_league_fixtures_stay_together():
    """同一联赛的比赛必须连续排列，不能被别的联赛插进来打断。

    这是分组标题能正常工作的前提：标题只在联赛切换时打印一次，
    一旦同一联赛被拆成两段，界面上就会看到两个重复的联赛标题。
    """
    # 故意让时间交错：英超 0/60 分钟，西甲 30/90 分钟
    # 若只按时间排序 → 英超、西甲、英超、西甲（交替，分组失效）
    mixed = [_fx(1, 39, 0), _fx(2, 140, 30), _fx(3, 39, 60), _fx(4, 140, 90)]
    svc = PredictionService(SETTINGS, FakeAPI({}))
    ordered = svc._sort_fixtures_by_league(mixed)

    lids = [fx["league"]["id"] for fx in ordered]
    assert lids == [39, 39, 140, 140], f"联赛未聚堆：{lids}"


def test_leagues_follow_registered_order():
    """联赛先后按 LEAGUE_ORDER（英超→西甲→德甲→意甲→法甲），不按 ID 大小。"""
    mixed = [_fx(1, 61, 0), _fx(2, 135, 10), _fx(3, 39, 20)]
    svc = PredictionService(SETTINGS, FakeAPI({}))
    lids = [fx["league"]["id"] for fx in svc._sort_fixtures_by_league(mixed)]
    assert lids == [39, 135, 61], f"未按登记顺序：{lids}"


def test_within_league_sorted_by_kickoff():
    """同一联赛内部仍按开赛时间升序——聚堆不等于打乱时间。"""
    same = [_fx(1, 39, 120), _fx(2, 39, 0), _fx(3, 39, 60)]
    svc = PredictionService(SETTINGS, FakeAPI({}))
    ids = [fx["fixture"]["id"] for fx in svc._sort_fixtures_by_league(same)]
    assert ids == [2, 3, 1], f"组内未按时间升序：{ids}"


def test_unknown_league_goes_last_not_dropped():
    """未登记的联赛排最后，但绝不能被丢弃——宁可分组难看也不能丢比赛。"""
    mixed = [_fx(1, 9999, 0), _fx(2, 39, 30)]
    svc = PredictionService(SETTINGS, FakeAPI({}))
    ordered = svc._sort_fixtures_by_league(mixed)
    assert len(ordered) == 2, "比赛被丢了"
    assert ordered[-1]["league"]["id"] == 9999


def test_missing_league_id_does_not_crash():
    """league 字段缺失或 id 不是整数时不能抛异常（数据源脏数据是常态）。"""
    fx = fixture(1, 10, "A", 11, "B", BASE)
    fx["league"] = {}
    svc = PredictionService(SETTINGS, FakeAPI({}))
    assert svc._sort_fixtures_by_league([fx]) == [fx]

    fx2 = fixture(2, 20, "C", 21, "D", BASE)
    fx2["league"] = {"id": "not-a-number"}
    assert len(svc._sort_fixtures_by_league([fx2])) == 1


def test_empty_input_is_safe():
    svc = PredictionService(SETTINGS, FakeAPI({}))
    assert svc._sort_fixtures_by_league([]) == []
    assert svc._sort_fixtures_by_league(None) in (None, [])
