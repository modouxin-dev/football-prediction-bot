"""命令接线体检：防止「调用了不存在的函数」这类 NameError 再次跑到生产环境。

/storage 曾因调用未定义的 _dispatch_cmd_typing 而在运行时抛 NameError，
本地 pytest 没覆盖到，只有线上日志才暴露。这里用 AST 静态扫描堵住这个口子。

命令已迁移到 commands/ 包（Command Pattern），因此本文件同时扫描
main.py 与 commands/ 下的全部模块。
"""
import ast
import builtins
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _module_scope_names(tree: ast.AST) -> set[str]:
    """收集可用的名字：模块顶层的类/变量/导入 + 任意层级的函数定义（含嵌套）。

    嵌套函数（如命令内部定义的辅助函数）同样合法，不能漏掉，否则会误报。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _scan_module(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    defined = _module_scope_names(tree)
    builtin_names = set(dir(builtins))
    undefined = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id not in defined and node.func.id not in builtin_names
    }
    return undefined


@pytest.mark.parametrize("rel", [
    "main.py", "support.py", "scheduler.py",
    "commands/__init__.py", "commands/adapters.py",
    "commands/basic.py", "commands/admin.py", "commands/matches.py",
])
def test_no_undefined_module_level_calls(rel):
    """每个模块里调用的函数名必须真实存在（模块定义或内置函数）。"""
    undefined = _scan_module(ROOT / rel)
    assert not undefined, f"{rel} 调用了未定义的名字：{sorted(undefined)}"


def test_registered_commands_point_to_real_coroutines():
    """dispatcher 里每条指令的回调都必须是真实协程函数。"""
    from commands import build_dispatcher

    dispatcher = build_dispatcher()
    assert len(dispatcher) >= 15, f"指令数量异常：{len(dispatcher)}"
    missing = [
        s.name for s in dispatcher.specs
        if not inspect.iscoroutinefunction(s.handler)
    ]
    assert not missing, f"指令回调缺失或不是协程：{missing}"


def test_every_bot_command_is_registered():
    """BOT_COMMANDS 里列出的每条指令都必须在 dispatcher 中注册。

    迁移前该校验靠源码字符串匹配（CommandHandler("x"...）；
    迁移后注册改为数据驱动，直接查运行时注册表更严格。
    """
    from commands import build_dispatcher
    from main import BOT_COMMANDS

    dispatcher = build_dispatcher()
    missing = [cmd for cmd, _ in BOT_COMMANDS if cmd not in dispatcher]
    assert not missing, f"以下指令未注册：{missing}"


def test_admin_commands_are_flagged():
    """管理员指令必须打上 admin_only 标记，否则权限会被绕过。"""
    from commands import build_dispatcher

    admin_cmds = {s.name for s in build_dispatcher().specs if s.admin_only}
    assert {"test", "status", "stats", "storage"} <= admin_cmds, admin_cmds


def test_date_command_handles_bad_and_missing_args():
    """/date 缺参数或格式错误时要给用法提示，不能崩。"""
    import asyncio

    from commands.matches import date_cmd

    assert inspect.iscoroutinefunction(date_cmd)

    class Msg:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append(text)
            return None

    class Upd:
        def __init__(self):
            self.effective_message = Msg()
            self.effective_user = None
            self.effective_chat = None

    class Ctx:
        def __init__(self, args):
            self.args = args
            self.user_data = {}
            self.application = None

    for args in ([], ["2026/10/10"], ["not-a-date"]):
        upd, ctx = Upd(), Ctx(args)
        asyncio.run(date_cmd(upd, ctx))
        assert upd.effective_message.sent, f"args={args} 时应给出提示"
