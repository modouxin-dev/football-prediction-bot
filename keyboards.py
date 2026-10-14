"""按钮键盘 / Inline & reply keyboards.

只负责「按钮怎么摆」，不含任何文案生成逻辑。与 views/ 分开是因为
键盘与视图的变更频率不同：改文案不动键盘，改按钮不动文案。
"""
from __future__ import annotations

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove)

from templates import MENU_ITEMS, PANEL_CLOSE_LABEL, TABS

# 主菜单每行按钮数。
# 实测：MENU_ITEMS 现有 8 项，按每行 2 个排是 4 行，底部面板连同「收起」
# 共 5 行，占掉近半屏；按每行 3 个排是 3 行（3+3+2），符合「每行 ≤3 个」
# 的排版规范。抽成常量是为了让 Inline 菜单与底部面板共用同一个值 ——
# 两处各写各的数字，迟早会出现「菜单 3 列、面板 2 列」的不一致。
MENU_PER_ROW = 3


def nav_row(with_fixtures: bool = True) -> list[InlineKeyboardButton]:
    """二级视图的统一出口行。

    实测发现的缺陷：比赛四个 tab 的切换键盘（get_main_keyboard）此前只有
    tab 与「刷新赔率」，没有任何出口 —— 用户点进「赔率对比」「历史交锋」
    之后只能重新发命令才能离开。凡是替换掉上一层内容的视图都必须给出口，
    这里统一定义，避免各自漏掉。

    with_fixtures=False 用于上一层不是赛程列表的场景（如积分榜）。
    """
    row: list[InlineKeyboardButton] = []
    if with_fixtures:
        row.append(InlineKeyboardButton("↩️ 返回赛程", callback_data="menu:fixtures"))
    row.append(InlineKeyboardButton("🏠 主菜单", callback_data="menu:home"))
    return row


class Keyboards:
    @staticmethod
    def get_main_keyboard(fixture_id: int, active: str = "home") -> InlineKeyboardMarkup:
        buttons = [
            InlineKeyboardButton(("● " if key == active else "") + label, callback_data=f"{key}:{fixture_id}")
            for key, label in TABS
        ]
        return InlineKeyboardMarkup(
            [
                buttons[:2],
                buttons[2:],
                [InlineKeyboardButton("🔄 刷新赔率", callback_data=f"refresh:{fixture_id}")],
                nav_row(),
            ]
        )

    @staticmethod
    def prediction_keyboard(fixture_id: int) -> InlineKeyboardMarkup:
        """预测卡片按钮：只留「这场比赛能做什么」，其余收进主菜单。

        原先 4 行 7 个按钮，其中「联赛排名」「历史命中率」与这场比赛无关，
        「今日赛程」又与底部的「返回赛程」重复——同一层出现两个回赛程的
        入口，用户不知道该点哪个。按钮一多，真正要点的「详细分析」反而
        被淹没，这是「排版杂」的主要来源。

        现在三行：第一行是本场的深入操作，第二行刷新（本场相关），
        第三行出口。全局功能（排名、命中率）回主菜单找。
        """
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 详细分析", callback_data=f"deep:{fixture_id}"),
                    InlineKeyboardButton("📈 概率图表", callback_data=f"chart:prob:{fixture_id}"),
                ],
                [InlineKeyboardButton("🔄 刷新数据", callback_data=f"refresh:{fixture_id}")],
                nav_row(),
            ]
        )

    @staticmethod
    def analysis_keyboard(fixture_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚽ 看预测", callback_data=f"fx:{fixture_id}"),
                    InlineKeyboardButton("📈 概率图表", callback_data=f"chart:prob:{fixture_id}"),
                ],
                [
                    InlineKeyboardButton("🎴 比赛主卡", callback_data=f"chart:card:{fixture_id}"),
                    InlineKeyboardButton("🎯 概率环", callback_data=f"chart:ring:{fixture_id}"),
                ],
                [
                    InlineKeyboardButton("📊 战绩图", callback_data=f"chart:form:{fixture_id}"),
                    InlineKeyboardButton("🥅 进失球图", callback_data=f"chart:goals:{fixture_id}"),
                    InlineKeyboardButton("🤝 交锋图", callback_data=f"chart:h2h:{fixture_id}"),
                ],
                [
                    InlineKeyboardButton("↩️ 返回赛程", callback_data="menu:fixtures"),
                    InlineKeyboardButton("🏠 主菜单", callback_data="menu:home"),
                ],
            ]
        )

    @staticmethod
    def chart_keyboard(fixture_id: int, kind: str) -> InlineKeyboardMarkup:
        """图片消息下方的导航：可继续切换其它图表或回到分析页。"""
        others = [k for k in ("form", "goals", "h2h") if k != kind]
        rows = [
            [
                InlineKeyboardButton(
                    {"form": "📊 战绩图", "goals": "🥅 进失球图", "h2h": "🤝 交锋图"}[k],
                    callback_data=f"chart:{k}:{fixture_id}",
                )
                for k in others
            ]
        ]
        rows.append(
            [InlineKeyboardButton("🔍 回到分析", callback_data=f"fa:{fixture_id}")]
            + nav_row()
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def digest_keyboard(focus: list[tuple[int, str]]) -> InlineKeyboardMarkup:
        """每日汇总的键盘：焦点战直达 + 底部两个出口。

        焦点战按钮直接带队名而不是序号：此前「⚽ 预测 3」要求用户先记住
        序号与比赛的对应关系，而序号在翻页、按联赛筛选后会重排——点进去
        落到另一场比赛是这类列表最常见的误操作。按钮上写清是哪一场，
        对应关系就不必靠记忆。

        每行只放一个焦点战按钮：两队名加 vs 在手机上已经接近一行宽度，
        并排两个必然各自截断，截断后反而认不出是哪场。

        底部两个出口沿用已有 callback（menu:fixtures / menu:stats），
        不新增路由，避免改一处要连带改 handler。
        """
        rows = [[InlineKeyboardButton(label, callback_data=f"fx:{fid}")] for fid, label in focus]
        rows.append(
            [
                InlineKeyboardButton("📋 完整赛程", callback_data="menu:fixtures"),
                InlineKeyboardButton("📊 准确率统计", callback_data="menu:stats"),
            ]
        )
        rows.append(nav_row(with_fixtures=False))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def standings_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 刷新", callback_data="menu:refresh")] + nav_row()]
        )

    @staticmethod
    def menu_keyboard() -> InlineKeyboardMarkup:
        """Inline 主菜单：点击后在原消息上切换，不刷屏。"""
        buttons = [InlineKeyboardButton(label, callback_data=f"menu:{key}") for key, label in MENU_ITEMS]
        return InlineKeyboardMarkup(
            [buttons[i:i + MENU_PER_ROW] for i in range(0, len(buttons), MENU_PER_ROW)]
        )

    @staticmethod
    def reply_menu_keyboard(expanded: bool = True):
        """底部常驻键盘：与 Inline 菜单共用 MENU_ITEMS，保证两边一致。

        面板占屏近一半高度，与正文抢空间，所以做成可收放的：
        - expanded：全部功能按钮 + 末行「✖️ 收起面板」
        - collapsed：返回 ReplyKeyboardRemove，把键盘整个移出屏幕

        收起必须是 ReplyKeyboardRemove 而不是「只留一行按钮」——后者仍是
        一个 ReplyKeyboardMarkup，客户端会继续为它留出一行高度，视觉上
        键盘并没有沉下去。只有 ReplyKeyboardRemove 才会真正收起并还原成
        系统字母键盘。
        收起只是隐藏键盘，不影响任何命令的可用性。
        """
        if not expanded:
            return ReplyKeyboardRemove()
        labels = [label for _, label in MENU_ITEMS]
        # 每行 MENU_PER_ROW 个，跟随 MENU_ITEMS 自动适配数量
        rows = [
            [KeyboardButton(x) for x in labels[i:i + MENU_PER_ROW]]
            for i in range(0, len(labels), MENU_PER_ROW)
        ]
        # 收起按钮单独一行：混在功能里容易被误当成某个功能
        rows.append([KeyboardButton(PANEL_CLOSE_LABEL)])
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True,
            is_persistent=True,
        )
