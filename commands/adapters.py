"""把「新消息」伪装成 callback_query 的适配器。

直接命令（/fixtures）与按钮回调走的是同一套就地编辑渲染逻辑，
但两者拿到的对象接口不同：命令拿到的是 Message，回调拿到的是
CallbackQuery。这里的适配器让命令也能复用回调路径，避免两份实现。
"""
from __future__ import annotations


class MessageQuery:
    """把 Message 包装成 callback_query 的鸭子类型。"""

    def __init__(self, message) -> None:
        self.message = message

    async def answer(self, *args, **kwargs) -> None:
        return None

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
        # 首次以新消息发出，后续编辑同一条（等价于按钮的就地切换体验）
        if getattr(self, "_sent", False):
            return await self.message.edit_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs
            )
        self._sent = True
        return await self.message.reply_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs
        )


class FakeUpdate:
    """轻量 Update 包装：让直接命令走与按钮相同的 handler 签名。"""

    def __init__(self, update) -> None:
        self._update = update
        self.callback_query = MessageQuery(update.effective_message)
        self.effective_message = update.effective_message
        self.effective_user = update.effective_user
        self.effective_chat = update.effective_chat

    def __getattr__(self, name):
        return getattr(self._update, name)


# 兼容旧名（原 main.py 中为下划线前缀的私有名）
_MessageQuery = MessageQuery
_FakeUpdate = FakeUpdate
