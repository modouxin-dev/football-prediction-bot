"""命令接线体检：防止「调用了不存在的函数」这类 NameError 再次跑到生产环境。

/storage 曾因调用未定义的 _dispatch_cmd_typing 而在运行时抛 NameError，
本地 pytest 没覆盖到，只有线上日志才暴露。这里用 AST 静态扫描堵住这个口子。
"""
import ast
import builtins
from pathlib import Path

import pytest


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
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def test_main_has_no_undefined_module_level_calls():
    """main.py 里调用的函数必须真实存在（模块级定义或内置函数）。"""
    src = Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    defined = _module_scope_names(tree)
    builtin_names = set(dir(builtins))

    undefined = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name not in defined and name not in builtin_names:
                undefined.add(name)

    assert not undefined, f"main.py 调用了未定义的名字：{sorted(undefined)}"


def test_command_handlers_point_to_existing_callbacks():
    """注册的每个命令回调都必须是 main 模块里真实存在的协程函数。"""
    import inspect

    import main

    src = Path(main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    registered = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CommandHandler"
            and node.args
            and isinstance(node.args[1], ast.Name)
        ):
            registered.append(node.args[1].id)

    assert registered, "没有解析到任何 CommandHandler 注册"

    missing = []
    for name in registered:
        fn = getattr(main, name, None)
        if fn is None or not inspect.iscoroutinefunction(fn):
            missing.append(name)

    assert not missing, f"命令回调缺失或不是协程：{missing}"


def test_date_command_registered_and_callable():
    """/date 指定日期查询必须注册，且回调是真实协程。

    规范要求支持「指定日期」查询，之前只有今日/未来7天/下一场，缺这一档。
    """
    import inspect

    import main

    assert hasattr(main, "date_cmd"), "缺少 date_cmd"
    assert inspect.iscoroutinefunction(main.date_cmd), "date_cmd 必须是协程"

    src = Path("main.py").read_text(encoding="utf-8")
    assert 'CommandHandler("date", date_cmd)' in src, "/date 未注册到 Application"


def test_date_command_handles_bad_and_missing_args():
    """/date 缺参数或格式错误时要给用法提示，不能崩。"""
    import asyncio
    import inspect

    import main

    assert inspect.iscoroutinefunction(main.date_cmd)

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
        asyncio.run(main.date_cmd(upd, ctx))
        assert upd.effective_message.sent, f"args={args} 时应给出提示"
