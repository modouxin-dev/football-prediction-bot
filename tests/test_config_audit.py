"""配置自检测试 —— 防"配了但没生效"的死代码复发。

事故原型：赛前信号字段曾长期解析了却没传进 Settings，线上恒取默认
False/0，整条信号链路是死代码，而启动日志一片正常。根因是没有任何地方
打印实际生效值。这里的测试保证：
  1. 每个被审计的字段都真实存在于 Settings（不凭印象编名字）
  2. 生效值被如实打印
  3. "环境变量写了、生效值却是假值"时必须告警
"""

from types import SimpleNamespace

from config import AUDIT_FIELDS, Settings, audit_settings


def test_every_audited_field_exists_on_settings():
    """审计清单里的字段名必须真实存在 —— use_market 这类臆造名会在此红。"""
    for env_name, attr in AUDIT_FIELDS:
        assert attr in Settings.__dataclass_fields__, (
            f"{env_name} 映射的字段 {attr} 在 Settings 上不存在")


def test_audit_prints_actual_values():
    """打印的是生效值，不是环境变量原文。"""
    s = SimpleNamespace(prematch_enabled=True, market_weight=0.75)
    lines, _ = audit_settings(s)
    joined = "\n".join(lines)
    assert "PREMATCH_ENABLED=True" in joined
    assert "MARKET_WEIGHT=0.75" in joined


def test_audit_warns_when_env_set_but_value_falsy():
    """显式配了却没生效 —— 死代码的典型症状，必须告警。"""
    s = SimpleNamespace(prematch_enabled=False)
    _, warnings = audit_settings(
        s, env={"PREMATCH_ENABLED": "true"})
    assert warnings and any("PREMATCH_ENABLED" in w for w in warnings)


def test_audit_warns_on_zero_strength():
    """强度类字段设了非 0 却生效为 0，同样要告警。"""
    s = SimpleNamespace(rating_strength=0)
    _, warnings = audit_settings(s, env={"RATING_STRENGTH": "0.4"})
    assert any("RATING_STRENGTH" in w for w in warnings)


def test_audit_silent_when_env_matches_value():
    """配了且生效一致 → 不告警，避免日志噪音。"""
    s = SimpleNamespace(prematch_enabled=True, market_weight=0.75)
    _, warnings = audit_settings(
        s, env={"PREMATCH_ENABLED": "true", "MARKET_WEIGHT": "0.75"})
    assert warnings == []


def test_audit_silent_when_env_absent():
    """没配环境变量就走默认值，属正常，不告警。"""
    s = SimpleNamespace(prematch_enabled=True)
    _, warnings = audit_settings(s, env={})
    assert warnings == []


def test_audit_ignores_blank_env():
    """空字符串视为未设置。"""
    s = SimpleNamespace(prematch_enabled=False)
    _, warnings = audit_settings(s, env={"PREMATCH_ENABLED": "  "})
    assert warnings == []


def test_audit_tolerates_missing_attribute():
    """字段被改名/移除时不得抛异常 —— 启动日志不能因为自检而崩。"""
    lines, _ = audit_settings(SimpleNamespace(), env={})
    assert lines and all("=" in ln for ln in lines)
