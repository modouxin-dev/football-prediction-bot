"""静态门禁：全仓不得出现「未定义名」（总控审计 2026-10-03 根因建议第 1 条）。

为什么单独做一条门禁：
`TTL_FIXTURES`、`s`、`Any` 三个未定义名能一路活到 main，共同根因是
**测试有盲区 + 没有静态检查**。补多少条运行时用例都只能覆盖被想到的路径，
而这类错误恰恰出现在没人想到的路径上（`get_fixtures_by_season` 零覆盖、
`/backfill all` 分支零覆盖）。静态检查能一次性扫出全部同类问题，
是投入产出比最高的一道防线。

只关心 `undefined name`：
`bot_handler.py` 存在有意重导出，会触发一批 `imported but unused`，
那是设计如此，不是缺陷，故不过滤也不校验这一类。
"""
import pathlib
import subprocess
import sys

import pytest

pyflakes = pytest.importorskip("pyflakes", reason="需要 pyflakes（见 requirements-dev.txt）")

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 与 CI 保持一致的扫描范围：顶层模块 + 子包
TARGETS = ["*.py", "commands/*.py", "views/*.py"]


def _collect_files():
    files = []
    for pat in TARGETS:
        files.extend(sorted(ROOT.glob(pat)))
    return [str(f.relative_to(ROOT)) for f in files if f.is_file()]


def test_targets_non_empty():
    """扫描范围不能为空——否则「零 undefined name」是假阳性。"""
    files = _collect_files()
    assert len(files) >= 20, f"扫描到的文件太少，glob 可能失效：{files}"


def test_no_undefined_names():
    """pyflakes 的 undefined name 必须为 0。

    曾真实出现的三个（均已修复，见 tests/test_audit_p0.py）：
    * football_data.py:435  undefined name 'TTL_FIXTURES'
    * commands/admin.py:599 undefined name 's'
    * commands/admin.py:51/270 undefined name 'Any'
    """
    files = _collect_files()
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *files],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    # pyflakes 无问题时退出码 0，有问题时非 0；两者都从 stdout 解析
    bad = [ln for ln in (proc.stdout or "").splitlines() if "undefined name" in ln]
    assert not bad, "存在未定义名（会导致运行时 NameError）：\n" + "\n".join(bad)
