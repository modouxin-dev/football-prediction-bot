"""队名埋点测试 / Team-name telemetry tests.

重点不是「收录了多少队」，而是**埋点机制本身**是否真的在工作。
历史教训一：上一版 26 条用例全绿，但清空缩写展开表后依然全绿——
因为用例全都同时被「直接收录」命中，测的是收录而不是机制，
机制哪怕变成死代码也无人知晓。

历史教训二：曾因 grep `_UNMATCHED` 未命中就断言「埋点不存在」，
实际它叫 `note_unmatched`，只是当时本地副本是旧版。搜索式结论必须
回读真实文件确认，否则会重复造轮子甚至回退既有功能。

所以这里每个用例都刻意使用**未收录的名字**（如 Zzz Unknown FC），
确保命中的是机制而非收录表。

    Focus is not "how many teams are covered" but whether the **mechanism**
    works. Lesson 1: 26 green cases stayed green with the abbreviation table
    emptied — they all also matched direct entries. Lesson 2: asserting
    "telemetry does not exist" from a grep miss was wrong; it was named
    `note_unmatched`. Every case here uses **untranslated names** on purpose.
"""
from __future__ import annotations

import pytest

import teammiss


@pytest.fixture
def miss_db(tmp_path):
    """每个用例一个独立埋点库，互不污染。"""
    teammiss.set_db_path(tmp_path / "miss.db")
    teammiss.clear()
    yield teammiss
    teammiss.clear()


# —— 持久化层（teammiss 模块）——

def test_record_stores_unknown_name(miss_db):
    """未收录队名被记下来。"""
    assert miss_db.count() == 0
    miss_db.record("Zzz Unknown FC")
    assert miss_db.count() == 1
    assert miss_db.recent(10)[0][0] == "Zzz Unknown FC"


def test_record_dedupes_same_name(miss_db):
    """同一名字重复记录不产生重复行（一级缓存短路）。"""
    for _ in range(5):
        miss_db.record("Zzz Unknown FC")
    assert miss_db.count() == 1


def test_record_ignores_blank(miss_db):
    """空值/空白不入库，避免把 None 当队名。"""
    miss_db.record(None)
    miss_db.record("")
    miss_db.record("   ")
    assert miss_db.count() == 0


def test_recent_orders_by_last_seen(miss_db):
    """recent 按末次出现倒序，最新出现的排前面。

    为什么需要 rowid 作二级键：last_seen 只精确到秒，同秒内多条记录
    的排序会退化成不确定，必须靠插入序兜底。
    """
    miss_db.record("Aaa First")
    miss_db.record("Bbb Second")
    miss_db.record("Ccc Third")
    names = [r[0] for r in miss_db.recent(10)]
    assert names[0] == "Ccc Third"
    assert len(names) == 3


def test_recent_respects_limit(miss_db):
    """limit 参数生效，不返回全部。"""
    for i in range(10):
        miss_db.record(f"Team {i:02d}")
    assert len(miss_db.recent(3)) == 3


def test_clear_resets(miss_db):
    """clear 同时清库与内存缓存，之后可重新记录同一个名字。"""
    miss_db.record("Zzz Unknown FC")
    miss_db.clear()
    assert miss_db.count() == 0
    miss_db.record("Zzz Unknown FC")
    assert miss_db.count() == 1


def test_record_never_raises_on_bad_path(tmp_path):
    """路径不可用时埋点静默失败，绝不打断渲染主流程。"""
    teammiss.set_db_path("/proc/definitely/not/writable/miss.db")
    teammiss.record("Zzz Unknown FC")
    assert teammiss.recent(5) == []


# —— 机制层（formatkit 集成）：名字必须未收录 ——

def test_note_unmatched_persists(miss_db):
    """note_unmatched 会落盘，而不只是留在内存。

    这是本轮的核心增量：远端原本只有内存 set，重启即丢、跨进程看不到。
    """
    import formatkit

    formatkit.clear_unmatched()
    formatkit.note_unmatched("Zzz Unknown FC")
    assert miss_db.count() == 1


def test_unmatched_export_prefers_persisted(miss_db):
    """导出优先取持久化库：内存 set 重启归零，不能代表历史漏网名单。"""
    import formatkit

    formatkit.clear_unmatched()
    miss_db.record("Ppp Persisted One")
    assert "Ppp Persisted One" in formatkit.unmatched_team_names(limit=50)


def test_team_short_name_records_miss(miss_db):
    """未收录队名走 team_short_name 时触发埋点。

    核心用例：名字刻意不在 TEAM_NAMES 里，命中与否完全取决于埋点机制。
    """
    import formatkit

    formatkit.clear_unmatched()
    raw = "Zzz Unknown FC"
    out = formatkit.team_short_name(raw)
    assert out == raw          # 未收录时原样返回，绝不猜中文名
    assert miss_db.count() == 1


def test_known_team_not_recorded(miss_db):
    """已收录队名不产生埋点——否则每次渲染都写库，列表页会拖垮性能。"""
    import formatkit

    formatkit.clear_unmatched()
    assert formatkit.team_short_name("Manchester City FC") == "曼城"
    assert miss_db.count() == 0


def test_team_name_records_miss(miss_db):
    """team_name（双语）未命中时同样埋点。"""
    import formatkit

    formatkit.clear_unmatched()
    raw = "Qqq Another Unknown"
    assert formatkit.team_name(raw, bilingual=True) == raw
    assert miss_db.count() == 1


def test_bilingual_off_still_records(miss_db):
    """关闭双语时若仍未收录，也要埋点。

    注意：bilingual=False 会原样返回英文，但这不等于「未收录」——
    因此必须在 cn 为空的判定分支里埋点，而不是在返回 raw 时埋点。
    """
    import formatkit

    formatkit.clear_unmatched()
    raw = "Rrr Third Unknown"
    assert formatkit.team_name(raw, bilingual=False) == raw
    assert miss_db.count() == 1


def test_clear_unmatched_clears_both(miss_db):
    """clear_unmatched 同时清内存与持久化库。"""
    import formatkit

    formatkit.note_unmatched("Zzz Unknown FC")
    assert miss_db.count() == 1
    formatkit.clear_unmatched()
    assert miss_db.count() == 0
    assert formatkit.unmatched_team_names(limit=10) == []


def test_teammiss_command_registered():
    """/teammiss 已注册为管理员指令。"""
    from commands.admin import register

    class Rec:
        def __init__(self):
            self.cmds = {}

        def register(self, name, fn, **kw):
            self.cmds[name] = (fn, kw)

    rec = Rec()
    register(rec)
    assert "teammiss" in rec.cmds
    assert rec.cmds["teammiss"][1].get("admin_only") is True
