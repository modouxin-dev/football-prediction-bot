"""按钮键盘 / Inline & reply keyboards.

只负责「按钮怎么摆」，不含任何文案生成逻辑。与 views/ 分开是因为
键盘与视图的变更频率不同：改文案不动键盘，改按钮不动文案。
"""
from __future__ import annotations

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup,
                      ReplyKeyboardRemove)

from formatkit import league_label
from templates import (
    MENU_ITEMS,
    PANEL_CLOSE_LABEL,
    PANEL_OPEN_LABEL,
    TABS,
)

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
            """预测卡片按钮：两列三行，继续操作全部走按钮，不挤在正文里。"""
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("📊 详细分析", callback_data=f"deep:{fixture_id}"),
                        InlineKeyboardButton("📈 概率图表", callback_data=f"chart:prob:{fixture_id}"),
                    ],
                    [
                        InlineKeyboardButton("📅 今日赛程", callback_data="menu:fixtures"),
                        InlineKeyboardButton("🏆 联赛排名", callback_data="menu:standings"),
                    ],
                    [
                        InlineKeyboardButton("🎯 历史命中率", callback_data="menu:stats"),
                        InlineKeyboardButton("🔄 刷新数据", callback_data=f"refresh:{fixture_id}"),
                    ],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="menu:home")],
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
                [
                    InlineKeyboardButton("🔍 回到分析", callback_data=f"fa:{fixture_id}"),
                    InlineKeyboardButton("🏠 主菜单", callback_data="menu:home"),
                ]
            )
            return InlineKeyboardMarkup(rows)
    
        @staticmethod
        def standings_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔄 刷新", callback_data="menu:refresh"),
                        InlineKeyboardButton("🏠 主菜单", callback_data="menu:home"),
                    ]
                ]
            )
    
        @staticmethod
        def menu_keyboard() -> InlineKeyboardMarkup:
            """Inline 主菜单：点击后在原消息上切换，不刷屏。"""
            buttons = [InlineKeyboardButton(label, callback_data=f"menu:{key}") for key, label in MENU_ITEMS]
            return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])
    
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
            # 每行 2 个，跟随 MENU_ITEMS 自动适配数量
            rows = [[KeyboardButton(x) for x in labels[i : i + 2]] for i in range(0, len(labels), 2)]
            # 收起按钮单独一行：混在功能里容易被误当成某个功能
            rows.append([KeyboardButton(PANEL_CLOSE_LABEL)])
            return ReplyKeyboardMarkup(
                rows,
                resize_keyboard=True,
                is_persistent=True,
            )
