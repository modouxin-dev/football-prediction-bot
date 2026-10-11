"""web/index.html 内联脚本的静态校验。

背景（真实事故）：
    web/index.html 里 `const $ = (id) => document.getElementById(id);`
    被声明了两次（第 81 行与第 176 行）。同一作用域内重复 const 声明是
    **语法错误**，整个 <script> 块一行都不会执行，表现是：
      - 页面永远停在静态占位「正在加载数据…」
      - 「刷新」「诊断」按钮全部失效（onclick 从未绑定）
      - 浏览器控制台才有报错，用户侧看不到任何提示
    而 pytest 与 pyflakes 都查不到它（.html 不在扫描范围），
    所以只能靠这里的静态检查兜住。

本测试只做「能不能解析 / 有没有重复声明」这类语法层校验，
不替代人工点击验证。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


def _script_blocks() -> list[str]:
    html = INDEX.read_text(encoding="utf-8")
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


def test_index_html_exists() -> None:
    assert INDEX.is_file(), f"看板入口不存在：{INDEX}"


def test_has_script_block() -> None:
    assert _script_blocks(), "index.html 里没有内联 <script>，看板不会有数据"


def test_no_duplicate_top_level_declarations() -> None:
    """同一 <script> 块内，顶层 const/let 不得重复声明同一标识符。

    重复 const 是语法错误，会让整块脚本静默失效。
    只检查顶格（无缩进）声明，避免误判函数体内部的同名局部变量。
    """
    for i, block in enumerate(_script_blocks()):
        seen: dict[str, int] = {}
        for lineno, line in enumerate(block.splitlines(), start=1):
            m = re.match(r"(const|let|var|function)\s+([A-Za-z_$][\w$]*)", line)
            if not m:
                continue
            kind, name = m.group(1), m.group(2)
            if kind == "var":
                continue  # var 允许重复声明
            if name in seen:
                pytest.fail(
                    f"index.html 第 {i} 个 <script> 块：标识符 `{name}` 在"
                    f" 顶层重复声明（第 {seen[name]} 行与第 {lineno} 行）。"
                    f" 重复 const/let 是语法错误，会导致整块脚本不执行、"
                    f" 页面永远停在「正在加载数据…」。"
                )
            seen[name] = lineno


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node，跳过运行解析检查")
def test_script_blocks_parse_with_node() -> None:
    """用 node 真正解析每个 <script> 块，语法错误在这里就会暴露。"""
    for i, block in enumerate(_script_blocks()):
        path = Path(f"/tmp/_index_script_{i}.js")
        path.write_text(block, encoding="utf-8")
        proc = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            f"index.html 第 {i} 个 <script> 块语法错误\n{proc.stderr}"
        )


def test_referenced_element_ids_exist() -> None:
    """JS 里 $('xxx') 引用的元素必须在 HTML 中存在，否则运行时 TypeError。"""
    html = INDEX.read_text(encoding="utf-8")
    defined = set(re.findall(r'id="([^"]+)"', html))
    used: set[str] = set()
    for block in _script_blocks():
        used |= set(re.findall(r"\$\('([^']+)'\)", block))
        used |= set(re.findall(r"getElementById\('([^']+)'\)", block))
    missing = sorted(used - defined)
    assert not missing, f"JS 引用了不存在的元素 id：{missing}"


def test_has_watchdog_block() -> None:
    """必须存在独立于主脚本的看门狗块。

    主脚本一旦语法错误就整体失效，只有**另一个** <script> 块能救场，
    否则用户看到的永远是无限「正在加载数据…」。
    """
    blocks = _script_blocks()
    assert len(blocks) >= 2, (
        "index.html 只有 1 个 <script> 块；主脚本出错时没有任何兜底提示，"
        "页面会一直停在「正在加载数据…」。"
    )
    assert any("setTimeout" in b for b in blocks[1:]), (
        "后续 <script> 块里没有看门狗（setTimeout 兜底提示）。"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
