"""按钮键盘 / Inline & reply keyboards.

只负责「按钮怎么摆」，不含任何文案生成逻辑。与 views/ 分开是因为
键盘与视图的变更频率不同：改文案不动键盘，改按钮不动文案。
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from formatkit import league_label
from templates import MENU_ITEMS, TABS

class Keyboards:
        @staticmethod
        def get_main_keyboard(fixture_id: int, active: str = "home") -> InlineKeyboardMarkup:
            buttons = [
                InlineKeyboardButton(("● " if key == active else "") + label, callback_data=f"{key}:{fixture_id}")
                for key, label in TABS
            ]
            return InlineKeyboardMarkup(
                [buttons[:2], buttons[2:], [InlineKeyboardButton("🔄 刷新赔率", callback_data=f"refresh:{fixture_id}")]]
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
        def reply_menu_keyboard() -> ReplyKeyboardMarkup:
            """底部常驻键盘：与 Inline 菜单共用 MENU_ITEMS，保证两边一致。"""
            labels = [label for _, label in MENU_ITEMS]
            # 每行 2 个，跟随 MENU_ITEMS 自动适配数量
            rows = [[KeyboardButton(x) for x in labels[i : i + 2]] for i in range(0, len(labels), 2)]
            return ReplyKeyboardMarkup(
                rows,
                resize_keyboard=True,
                is_persistent=True,
            )
