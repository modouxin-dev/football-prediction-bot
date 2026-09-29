"""命令层 / Command layer (Command Pattern).

设计目标：把「谁来处理 /command」与「怎么处理」解耦。

- `main.py` 只负责：装配依赖（`CommandRuntime`）→ 注册指令（dispatcher）→ 启动
- 每个命令是独立模块里的一个 `handler(update, context)`，互不依赖 main 的内部实现
- 需要回用到 main 的能力（菜单分发、赛程渲染等）通过 `CommandRuntime` 注入，
  不产生 `commands → main` 的反向导入，避免循环依赖

权限统一由 dispatcher 包装处理：命令模块不再各自重复
`if not is_admin(...): return await deny(update)`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from support import deny, is_admin

# 子模块**不在模块顶层导入**：它们需要反向引用本模块的 CommandDispatcher，
# 若在此处 import 会形成循环导入（commands → basic → commands）。
# 改为在 build_dispatcher() 内部延迟导入，此时本模块已初始化完成。

__all__ = ["CommandDispatcher", "CommandRuntime", "CommandSpec", "build_dispatcher"]


@dataclass(frozen=True)
class CommandRuntime:
    """命令执行所需的外部依赖，由 main.py 在启动时装配一次。

    之所以用注入而非直接 import main：命令层若反向导入入口模块，
    会形成循环依赖，也会让命令无法脱离机器人单独测试。
    """

    # 只注入「必须留在 main.py」的能力：菜单分发与赛程渲染依赖
    # main 内部的 on_menu_key / 分页状态，无法下沉到共享层。
    dispatch_menu: Callable[[object, object, str], Awaitable[None]]
    show_fixtures: Callable[..., Awaitable[None]]
    modes: dict = field(default_factory=dict)

    @classmethod
    def from_bot_data(cls, bot_data: dict) -> Optional["CommandRuntime"]:
        """从 bot_data 取出运行时；未装配时返回 None（便于单测直接调用）。"""
        return bot_data.get("cmd_runtime")


@dataclass(frozen=True)
class CommandSpec:
    """一条指令的注册信息。"""
    name: str
    handler: Callable[..., Awaitable[None]]
    admin_only: bool = False
    description: str = ""


class CommandDispatcher:
    """指令注册表 + 分发器。

    职责边界刻意做窄：只做「查表 + 权限拦截 + 调用」，
    不含任何业务逻辑——业务逻辑全在各 command 模块里。
    """

    def __init__(self) -> None:
        self._specs: list[CommandSpec] = []
        self._by_name: dict[str, CommandSpec] = {}

    def register(self, name: str, handler, *, admin_only: bool = False,
                 description: str = "") -> None:
        spec = CommandSpec(name=name, handler=handler, admin_only=admin_only,
                           description=description)
        if name in self._by_name:
            raise ValueError(f"指令重复注册：{name}")
        self._by_name[name] = spec
        self._specs.append(spec)

    @property
    def specs(self) -> list[CommandSpec]:
        return list(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def get(self, name: str) -> Optional[CommandSpec]:
        return self._by_name.get(name)

    async def execute(self, name: str, update, context) -> None:
        """按名称执行一条指令（含权限拦截）。供测试与未来的 Web/API 复用。"""
        spec = self._by_name.get(name)
        if spec is None:
            raise KeyError(f"未注册的指令：{name}")
        await self._guard(spec, update, context)

    async def _guard(self, spec: CommandSpec, update, context) -> None:
        """权限拦截：管理员指令在此统一处理，命令模块不再各自重复。"""
        if spec.admin_only:
            settings = context.application.bot_data.get("settings")
            if settings is not None and not is_admin(update, settings):
                await deny(update)
                return
        await spec.handler(update, context)

    def wrap(self, spec: CommandSpec):
        """生成可直接交给 CommandHandler 的回调（带权限拦截）。"""
        async def handler(update, context):
            await self._guard(spec, update, context)
        return handler


def build_dispatcher() -> CommandDispatcher:
    """装配全部指令。新增指令只需在对应模块里 register 一行。

    子模块在此延迟导入（见文件顶部说明）：调用时本模块已初始化完毕，
    各命令模块可以安全地从这里取 CommandDispatcher。
    """
    from . import admin, basic, matches  # 延迟导入，避免循环依赖

    dispatcher = CommandDispatcher()
    basic.register(dispatcher)
    matches.register(dispatcher)
    admin.register(dispatcher)
    return dispatcher
