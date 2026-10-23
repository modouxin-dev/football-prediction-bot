"""只推最有把握的 N 场（TopN）+ 赛程查找兜底。

两条性质必须长期锁住：

1. 推送场次被砍到 3~7 时，**被砍掉的原因要写进 note**。
   否则用户看到「今天 3 场」无从判断是当天只有 3 场，还是被筛掉了 27 场。
2. 赛程缓存过期后，用户点旧卡片必须还能打开。
   Prediction.fixture 保存了原始数据快照，只要算过一次就不该再报
   「该场比赛已不在今日赛程中」——这是实测出现过的真 bug。
"""

import dataclasses
from datetime import timedelta

import pytest

from config import load_settings
from service import PredictionService
from tests.sample_data import NOW, FakeAPI, default_fixtures, fixture

ENV = {"TELEGRAM_TOKEN": "t", "RAPID_API_KEY": "k", "CHAT_ID": "1",
       "SEASON": "2026", "TIMEZONE": "Asia/Shanghai"}
SETTINGS = load_settings(ENV)


def run(coro):
    import asyncio
    return asyncio.run(coro)


def _service(settings=SETTINGS, n_matches=2):
    """造一个已跑过预测的 service：_store 里有快照，可测缓存过期场景。"""
    api = FakeAPI({2026: [fixture(900 + i, 1, f"Home{i}", 1, f"Away{i}",
                                  NOW + timedelta(hours=3 + i)) for i in range(n_matches)]})
    svc = PredictionService(settings, api)
    preds = run(svc.build_predictions(now=NOW))
    return svc, preds


def _scaled(preds, n):
    """把少量真实预测复制成 n 场，把握度从低到高排列（下标越大越有把握）。"""
    out = []
    for i in range(n):
        base = preds[i % len(preds)]
        # 步长 0.03：20 场最高 0.87，始终是合法概率（0~1），不会造出 >1 的假数据
        conf = round(0.30 + i * 0.03, 4)
        out.append(dataclasses.replace(
            base, fixture_id=1000 + i,
            analysis={**base.analysis,
                      "win_prob": conf,
                      "draw_prob": round((1 - conf) / 2, 4),
                      "loss_prob": round((1 - conf) / 2, 4)}))
    return out


# ---- TopN：上限 / 下限 / 排序 -------------------------------------------------

def test_topn_default_caps_at_seven_and_keeps_the_most_confident():
    svc, preds = _service()
    pool = _scaled(preds, 20)
    keep = svc.select_top_predictions(pool)
    assert len(keep) == 7                       # 上限默认 7
    confs = [svc.confidence_of(p) for p in keep]
    assert confs == sorted(confs, reverse=True)  # 降序，留下的都是把握度最高的
    assert confs[0] == pytest.approx(0.30 + 19 * 0.03)   # 全局最高被保留
    assert confs[-1] == pytest.approx(0.30 + 13 * 0.03)  # 第 7 高，其余被砍


def test_topn_truncation_is_visible_in_note():
    """被砍掉多少、为什么砍，必须让用户看见。"""
    svc, preds = _service()
    svc.select_top_predictions(_scaled(preds, 20))
    assert "20" in svc.last_note and "7" in svc.last_note
    assert "只推送把握度最高的" in svc.last_note


def test_topn_keeps_everything_when_pool_smaller_than_cap():
    svc, preds = _service()
    pool = _scaled(preds, 5)     # 在 3~7 区间内，不砍
    keep = svc.select_top_predictions(pool)
    assert len(keep) == 5
    assert svc.last_note is None  # 没砍就不该有「已筛选」提示，避免误导


def test_topn_does_not_invent_matches_when_pool_below_floor():
    """当天不足 3 场就全推，绝不为了凑下限虚构场次。"""
    svc, preds = _service()
    keep = svc.select_top_predictions(_scaled(preds, 2))
    assert len(keep) == 2


def test_topn_respects_configured_bounds():
    bounds = dataclasses.replace(SETTINGS, topn_min=2, topn_max=4)
    svc, preds = _service(bounds)
    assert len(svc.select_top_predictions(_scaled(preds, 20))) == 4


def test_topn_can_be_disabled_without_touching_callers():
    off = dataclasses.replace(SETTINGS, topn_enabled=False)
    svc, preds = _service(off)
    assert len(svc.select_top_predictions(_scaled(preds, 20))) == 20


def test_topn_confidence_floor_drops_weak_picks_and_says_why():
    strict = dataclasses.replace(SETTINGS, topn_min_confidence=0.60)
    svc, preds = _service(strict)
    keep = svc.select_top_predictions(_scaled(preds, 20))
    # 把握度 >=0.60 的共 10 场（i=10..19），按上限只留最高的 7 场
    assert all(svc.confidence_of(p) >= 0.60 for p in keep)
    assert len(keep) == 7


def test_topn_reports_when_nothing_clears_the_floor():
    strict = dataclasses.replace(SETTINGS, topn_min_confidence=0.99)
    svc, preds = _service(strict)
    keep = svc.select_top_predictions(_scaled(preds, 20))
    assert keep == []                       # 宁缺毋滥：不达门槛就不推
    assert "把握度门槛" in svc.last_note


# ---- 赛程查找兜底 -------------------------------------------------------------

def test_clicking_a_stale_card_still_works_from_stored_snapshot():
    """回归：缓存过期 + 重新拉取为空时，旧卡片仍可点开。

    赛程缓存 TTL 只有 10 分钟，用户隔一段时间再点就会撞上「不在今日赛程」。
    只要这场算过一次，Prediction.fixture 里就有原始数据，不该再报错。
    """
    svc, preds = _service()
    target = preds[0]

    async def empty_today(*_a, **_k):
        return []          # 模拟缓存过期且当天已无该场

    svc.get_today_fixtures = empty_today
    got = run(svc.predict_fixture(target.fixture_id, None))
    assert got.fixture_id == target.fixture_id


def test_unknown_fixture_still_raises_keyerror():
    """彻底没见过的 ID 仍要抛 KeyError，让上层给出「不在今日赛程」提示。"""
    svc, _ = _service()

    async def empty_today(*_a, **_k):
        return []

    svc.get_today_fixtures = empty_today
    with pytest.raises(KeyError):
        run(svc.predict_fixture(999999, None))


def test_analyze_fixture_also_benefits_from_snapshot_fallback():
    svc, preds = _service()
    target = preds[0]

    async def empty_today(*_a, **_k):
        return []

    svc.get_today_fixtures = empty_today
    report = run(svc.analyze_fixture(target.fixture_id, None))
    assert report["home"] == target.home


def test_snapshot_fallback_does_not_mask_api_failures():
    """兜底只补「找不到赛程」，不能把 API 故障也吞成静默成功。"""
    svc, preds = _service()
    target = preds[0]

    async def broken_today(*_a, **_k):
        raise RuntimeError("boom")

    svc.get_today_fixtures = broken_today
    with pytest.raises(RuntimeError):
        run(svc.predict_fixture(999999, None))
