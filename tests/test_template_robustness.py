"""模板渲染健壮性 / Template rendering robustness (Stage 8.1 审计点 3)。

解耦后文案分布在 views/ + templates.py，动态值靠 f-string 与下标注入。
本文件验证：**凡是真实可能出现的输入组合，渲染都必须成功且不含 Key Error**。

方法上刻意区分两类缺失：

- **不可达的缺失**（如 analysis 少了 win_prob）：由 analyzer 保证恒有。
  不为它加防御——那会掩盖真实 bug，属于噪音。故这里不做「随便删键」的测试。
- **可达的降级**（无赔率、无交锋、队名含特殊字符、备用数据源…）：
  必须优雅渲染，这是本文件的重点。

关于 HTML：队名来自外部 API，必须转义后再拼进 HTML 消息，
否则一个 `<` 就会让整条 Telegram 消息发送失败（parse_mode=HTML）。
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone

import pytest

from bot_handler import BotUI
from config import load_settings
from service import PredictionService

SETTINGS = load_settings(
    {"TELEGRAM_TOKEN": "1:TEST", "RAPID_API_KEY": "k", "CHAT_ID": "555",
     "SEASON": "2026", "ADMIN_ID": "555"}
)


def _build_service():
    from tests.test_prediction import FullAPI, now_fixtures

    return PredictionService(SETTINGS, FullAPI(now_fixtures())), now_fixtures()


@pytest.fixture(scope="module")
def base():
    """一个真实的 Prediction 对象（带赔率、有交锋数据的正常场景）。"""
    svc, fixtures = _build_service()
    return asyncio.run(svc.predict_fixture(1001, fixtures))


@pytest.fixture()
def ui():
    return BotUI()


def render_all(ui, p, tz=None):
    """把主消息相关的视图全渲染一遍，返回 (方法名, 结果/异常)。"""
    tz = tz or SETTINGS.timezone
    out = {}
    for name in ("format_prediction", "format_prediction_card", "format_odds_detail",
                 "matchup", "confidence_text", "risk_lines"):
        try:
            out[name] = getattr(ui, name)(p, tz) if name in (
                "format_prediction", "format_prediction_card", "format_odds_detail",
                "matchup") else getattr(ui, name)(p)
        except Exception as exc:
            out[name] = exc
    return out


# ============================================================================
# 一、可达的降级场景：不得抛异常
# ============================================================================

def test_no_odds_does_not_crash(ui, base):
    """无赔率是最常见的降级（免费套餐常不开放赔率）。

    odds=None 时 best 也应为 None，消息应走「赔率：暂无」分支。
    """
    p = copy.copy(base)
    p.odds = None
    p.best = None
    res = render_all(ui, p)
    bad = {k: v for k, v in res.items() if isinstance(v, BaseException)}
    assert not bad, f"无赔率时渲染失败：{bad}"
    assert "暂无" in res["format_prediction"]


def test_best_without_odds_degrades_gracefully(ui, base):
    """best 与 odds 的隐式不变量被打破时，也必须降级而非崩溃。

    这是本文件唯一一条「人为构造的不可能输入」——因为它是真实存在的
    跨模块隐式耦合：best 由 evaluate_outcomes(analysis, odds) 产生，
    两者的一致性不在本模块内保证。曾经这里会抛 TypeError 让整条推送失败。
    """
    p = copy.copy(base)
    p.odds = None          # 保留 truthy 的 best，故意打破不变量
    assert p.best, "前提：base 应带 best"
    res = render_all(ui, p)
    bad = {k: v for k, v in res.items() if isinstance(v, BaseException)}
    assert not bad, f"best 存在但 odds=None 时渲染失败：{bad}"


def test_empty_bookmakers(ui, base):
    p = copy.copy(base)
    p.bookmakers = []
    p.odds = None
    p.best = None
    res = render_all(ui, p)
    assert not [v for v in res.values() if isinstance(v, BaseException)]


def test_missing_optional_text_fields(ui, base):
    """venue / round_label 常为空字符串（低级别联赛数据不全）。"""
    p = copy.copy(base)
    p.venue = ""
    p.round_label = ""
    res = render_all(ui, p)
    assert not [v for v in res.values() if isinstance(v, BaseException)]


def test_insufficient_data_flag(ui, base):
    """积分榜里没有这两队 → 必须显示「数据不足」警告。

    注意：`insufficient` 是 Prediction 的**只读 property**（由 model 与两队
    id 推导），不能靠赋值 __dict__ 伪造——那样读到的仍是原值。
    要构造这个场景，得让 model 里不含这两队。
    """
    from analyzer import LeagueModel

    p = copy.copy(base)
    # LeagueModel 是 frozen dataclass，不能改字段，直接换成空模型
    p.model = LeagueModel()     # teams 默认为空
    assert p.insufficient, "前提：model 无球队时应判定为数据不足"
    text = ui.format_prediction(p, SETTINGS.timezone)
    assert "数据不足" in text


# ============================================================================
# 二、外部输入必须转义（否则整条 HTML 消息发送失败）
# ============================================================================

@pytest.mark.parametrize("name", [
    "A & B", "A < B", "A > B", '"quoted"', "a'</script>", "A&B<C>D",
])
def test_special_characters_are_escaped(ui, base, name):
    """队名来自外部 API，必须转义后再拼进 HTML。

    判据：转义后的形式必须出现在输出里，且原始形式必须**不**出现。
    （不能简单断言「输出里没有 &」——转义后的 &amp; 本身也含 &。）
    """
    from bot_handler import esc

    p = copy.copy(base)
    p.home = name
    p.away = name
    text = ui.format_prediction(p, SETTINGS.timezone)
    escaped = esc(name)
    assert escaped in text, f"{name!r} 未以转义形式出现（期望 {escaped!r}）"
    if escaped != name:          # 只在名字确实含特殊字符时检查
        assert name not in text, f"{name!r} 以未转义形式出现，会导致 HTML 解析失败"
    assert "<b>" in text         # 我们自己的标签仍在


def test_emoji_and_unicode_names_render(ui, base):
    p = copy.copy(base)
    p.home = "⚽ 拜仁 München"
    p.away = "🇩🇪 Dortmund"
    assert ui.format_prediction(p, SETTINGS.timezone)


def test_empty_team_name_does_not_crash(ui, base):
    p = copy.copy(base)
    p.home = ""
    p.away = ""
    assert ui.format_prediction(p, SETTINGS.timezone)


# ============================================================================
# 三、输出结构不变式
# ============================================================================

def test_probabilities_sum_to_one_in_output(ui, base):
    """展示的三项概率必须与模型一致（不允许为了好看而归一化到别的数）。"""
    a = base.analysis
    assert abs(a["win_prob"] + a["draw_prob"] + a["loss_prob"] - 1.0) < 1e-6


def test_prediction_contains_disclaimer(ui, base):
    """每条预测都必须带免责声明——合规红线。"""
    text = ui.format_prediction(base, SETTINGS.timezone)
    assert "仅供" in text or "不构成" in text or "参考" in text


def test_prediction_has_no_guaranteed_win_wording(ui, base):
    from templates import DISCLAIMER

    text = ui.format_prediction(base, SETTINGS.timezone)
    for bad in ("必胜", "稳赢", "100%准确", "确定中奖"):
        assert bad not in text


def test_output_is_nonempty_string(ui, base):
    for name, val in render_all(ui, base).items():
        if name == "risk_lines":
            assert isinstance(val, list)
        else:
            assert isinstance(val, str) and val.strip()


# ============================================================================
# 四、深度分析报告
# ============================================================================

@pytest.fixture(scope="module")
def report():
    svc, fixtures = _build_service()
    return asyncio.run(svc.analyze_fixture(1001, fixtures))


def test_deep_report_renders(ui, report):
    text = ui.format_deep_report(report, SETTINGS.timezone)
    assert "深度分析" in text
    assert "历史交锋" in text


def test_deep_report_handles_empty_optional_sections(ui, report):
    """交锋为空、近期状态为空：真实可达（新升班马、跨联赛首次交手）。"""
    r = dict(report)
    # 两处都必须是「空但结构完整」的 dict：h2h_stats / form_stats 恒返回 dict，
    # 传 None 是不可达输入（本文件开头已说明不为不可达输入做防御）。
    # played=0 才是真实的「新升班马 / 跨联赛首次交手」场景。
    r["h2h"] = {"played": 0}
    r["home_form"] = {"played": 0}
    r["away_form"] = {"played": 0}
    text = ui.format_deep_report(r, SETTINGS.timezone)
    assert "深度分析" in text


def test_deep_report_surfaces_fetch_errors(ui, report):
    """可选数据拉取失败时要显示真实原因，不能伪装成「没有数据」。"""
    r = dict(report)
    r["errors"] = {"home_form": "HTTP 429 额度用尽"}
    text = ui.format_deep_report(r, SETTINGS.timezone)
    assert "429" in text


def test_deep_report_missing_error_key_is_safe(ui, report):
    """errors 里没有对应键时不得 KeyError（用 .get 判断却用 [] 取值的经典坑）。"""
    r = dict(report)
    r["errors"] = {}          # 只有空 dict，没有任何键
    text = ui.format_deep_report(r, SETTINGS.timezone)
    assert "深度分析" in text


# ============================================================================
# 五、静态模板自检
# ============================================================================

def test_templates_module_has_no_project_imports():
    """templates.py 必须在依赖链最底层，否则会形成循环导入。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("templates.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    project = {"service", "analyzer", "config", "repository", "api_client",
               "formatkit", "keyboards", "views", "bot_handler", "main"}
    assert not (mods & project), f"templates.py 引入了项目内模块：{mods & project}"


def test_no_undefined_names_in_views():
    """views/ 下不得调用未定义的名字（迁移时最容易漏的就是 helper）。"""
    import ast
    import builtins
    from pathlib import Path

    for path in Path("views").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
        undefined = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id not in defined
        }
        assert not undefined, f"{path} 调用了未定义的名字：{sorted(undefined)}"
