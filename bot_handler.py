"""消息模板与按钮键盘的门面 / Facade for message templates & keyboards.

这个文件曾经是 1135 行的单体类，现已按**视图垂直拆分**：

    templates.py    静态文案与展示常量（零依赖，最底层）
    formatkit.py    原子格式化工具（转义、概率条、队名、积分榜摘要…）
    keyboards.py    按钮键盘（只管按钮怎么摆）
    views/          各视图：文案与生成它的逻辑同处一地
    bot_handler.py  本文件：Mixin 组装 + 兼容导出

**为什么不把文案抽成 JSON/YAML 模板**：实测文案行只占原文件的 40%~60%，
其余是循环与条件分支；抽走文案后体积压不到预期。更关键的是，文案与
「在什么条件下说这句话」强耦合——拆开后改一句文案要跨两个文件，
反而比原来更难维护。按视图切分，改文案只动 views/ 里对应的一个文件。

**关于 BotUI**：它是**纯门面**，用 Mixin 组合各视图的方法，自身不含实现。
543 个既有测试与 main.py 全部通过 `ui.xxx` / `BotUI.xxx` 调用，
保持签名不变即可零改动迁移。
"""
from __future__ import annotations

# ---- 兼容导出 ---------------------------------------------------------------
# 这些名字原本就定义在 bot_handler.py，外部（main.py / commands/ / 543 个测试）
# 至今从本模块导入。迁移后它们分散在 formatkit 与 templates，这里集中再导出，
# 让调用方零改动。pyflakes 会报「imported but unused」——这是有意为之。
from formatkit import (
    NO_DATA,
    _WEB_ENTRY_TEXT,
    _factors,
    _form_line,
    _is_fallback,
    _num,
    _row_line,
    _row_summary,
    bar,
    build_prediction_payload,
    esc,
    league_label,
    split_html_blocks,
    team_name,
    web_entry_text,
)
from keyboards import Keyboards
from templates import (
    BRAND_CN,
    BRAND_EN,
    DISCLAIMER,
    LEAGUE_NAMES,
    MENU_ITEMS,
    OUTCOME_LABEL,
    SEP,
    STATUS_TEXT,
    TABS,
    TEAM_NAMES,
    THIN_SEP,
    VALUE_FLAG,
    VALUE_HIGH,
    VALUE_LOW,
)
from views.analysis import AnalysisView
from views.common import CommonView
from views.fixtures import FixturesView
from views.menu import MenuView
from views.prediction import PredictionView
from views.standings import StandingsView

__all__ = [
    "BotUI", "MENU_ITEMS", "WEB_ENTRY_TEXT", "esc", "league_label",
    "split_html_blocks", "team_name", "web_entry_text", "bar",
]

class BotUI(
    Keyboards,          # 按钮键盘
    CommonView,         # 时间、对阵、概率条等公共原语
    PredictionView,     # 预测主消息 / 预测卡片 / 赔率
    AnalysisView,       # 深度分析 / 历史交锋
    FixturesView,       # 赛程页
    StandingsView,      # 积分榜
    MenuView,           # 欢迎 / 菜单 / 帮助
):
    """消息模板门面。

    方法全部来自 Mixin，本类只保留一个兜底常量属性。
    MRO：同名方法以左侧 Mixin 优先；当前各组无重名，顺序仅影响可读性。
    """

    # 未配置 WEB_URL 时的兜底文案（保留类属性以兼容既有引用）
    WEB_ENTRY_TEXT = _WEB_ENTRY_TEXT

WEB_ENTRY_TEXT = BotUI.WEB_ENTRY_TEXT
