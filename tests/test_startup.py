"""启动链路回归测试。

背景（真实线上事故）：
    曾在 main() 中用 `asyncio.run(app.bot.delete_webhook(...))` 清理 Webhook。
    asyncio.run() 会在结束时关闭并置空当前 event loop，随后
    Application.run_polling() 内部执行 `asyncio.get_event_loop()` 抛
    "There is no current event loop in thread 'MainThread'"，
    导致 Updater.start_polling 协程从未被 await：
        RuntimeWarning: coroutine 'Updater.start_polling' was never awaited
    表现为容器启动几秒即退出、所有 Telegram 命令无响应。

本测试从静态层面锁死该问题，防止回归。
"""

import ast
import inspect
import pathlib

import main


def _main_source() -> str:
    return pathlib.Path(main.__file__).read_text(encoding="utf-8")


def test_main_must_not_call_asyncio_run():
    """main.py 不得出现 asyncio.run 调用。

    run_polling 自行管理 event loop；在此之前调用 asyncio.run 会关闭 loop，
    使 run_polling 崩溃且 Updater 协程永不 await。
    """
    source = _main_source()
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
        ):
            offenders.append(node.lineno)

    assert not offenders, (
        "main.py 第 "
        + ", ".join(str(n) for n in offenders)
        + " 行调用了 asyncio.run()，这会关闭 event loop 并导致 run_polling 崩溃；"
        "请在 post_init 等 run_polling 的 loop 内执行异步清理。"
    )


def test_webhook_cleanup_runs_inside_post_init():
    """Webhook 清理必须放在 post_init（run_polling 的 event loop 内）。"""
    source = _main_source()
    tree = ast.parse(source)

    target = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "post_init":
            target = node
            break

    assert target is not None, "未找到 post_init 函数"

    segment = ast.get_source_segment(source, target) or ""
    assert "delete_webhook" in segment, (
        "post_init 中未调用 delete_webhook；Webhook 残留会让 getUpdates 返回 409 并终止进程"
    )


def test_run_polling_is_last_call_in_main():
    """main() 的最后一步必须是 run_polling，且之前不得插入阻塞性清理。"""
    source = inspect.getsource(main.main)
    assert "run_polling" in source
    lines = [ln.strip() for ln in source.splitlines() if ln.strip()]
    assert lines[-1].startswith("app.run_polling"), (
        "main() 末尾应为 app.run_polling(...)，实际为：" + lines[-1]
    )
