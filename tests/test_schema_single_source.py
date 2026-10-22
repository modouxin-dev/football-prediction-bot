"""建表脚本单一来源守卫。

为什么需要这条守卫
------------------
此前 `backtest_runs` 在 SCHEMA 与 QUOTA_SCHEMA 里各定义一份，`api_quota`
同样如此。两份当时完全一致，所以 `IF NOT EXISTS` 不会报错——问题出现在
**以后**：某天加一个字段只改了其中一份，谁先执行谁说了算，线上就会冒出
"字段时有时无"的诡异现象。

这类 bug 不会让 CI 变红，只会在生产半夜冒出来。所以把「同一份 DDL 只能
有一处文本」用测试钉死，比靠人记住可靠。

现状：QUOTA_SCHEMA 由 `_ddl_of()` 从 SCHEMA 里切出来，物理上只有一份文本。
"""

from __future__ import annotations

import re

from repository import QUOTA_SCHEMA, SCHEMA

# 取出 SQL 里所有 "CREATE TABLE IF NOT EXISTS <name>" 的表名
_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)",
                       re.IGNORECASE)


def _tables(sql: str) -> list[str]:
    return _TABLE_RE.findall(sql)


def _ddl_block(sql: str, table: str) -> str:
    marker = f"CREATE TABLE IF NOT EXISTS {table} "
    start = sql.index(marker)
    return sql[start:sql.index(");", start) + 2]


def test_quota_schema_is_derived_from_schema():
    """QUOTA_SCHEMA 必须从 SCHEMA 切出来，不能另抄一份。

    这是整套守卫的核心：`in` 成立即证明两者文本同源，
    将来无论怎么改 SCHEMA，配额那份都会自动跟上。
    """
    assert QUOTA_SCHEMA in SCHEMA, "QUOTA_SCHEMA 与 SCHEMA 已分家"


def test_overlapping_tables_have_identical_ddl():
    """若将来又出现两处声明，DDL 必须逐字一致——防「只改一处」。"""
    overlap = set(_tables(SCHEMA)) & set(_tables(QUOTA_SCHEMA))
    for t in sorted(overlap):
        assert _ddl_block(SCHEMA, t) == _ddl_block(QUOTA_SCHEMA, t), (
            f"{t} 两处定义不一致")


def test_backtest_runs_defined_exactly_once():
    """回测结果表有且只有一处定义（此前在 QUOTA_SCHEMA 里多了一份）。"""
    assert SCHEMA.count("CREATE TABLE IF NOT EXISTS backtest_runs") == 1
    assert QUOTA_SCHEMA.count("CREATE TABLE IF NOT EXISTS backtest_runs") == 0


def test_quota_schema_still_creates_quota_table():
    """去重不能把配额模块自己的表也弄没了。"""
    assert "api_quota" in _tables(QUOTA_SCHEMA)
