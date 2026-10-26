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


# ---------------------------------------------------------------------------
# 防「静默卡死」加固（真实事故续集）
#
# 事故升级：即使修好了重复声明，页面仍可能因**接口永久挂起**而停在
# 「正在加载数据…」——Promise.all 既不 resolve 也不 reject，
# catch 分支永远不会执行，用户看不到任何错误。
# 本组测试锁死三件事：
#   1. 每个接口都有超时（AbortController），挂起会转成可捕获的错误
#   2. load() 的六个接口全部带 .catch 兜底，任一失败都降级渲染而非整体失败
#   3. 页面带静态构建标记，无需 JS 即可判断线上是否是新版本
# ---------------------------------------------------------------------------

def test_fetch_has_timeout() -> None:
    """接口必须有超时，否则挂起会让页面永久停在加载中。"""
    html = INDEX.read_text(encoding="utf-8")
    assert "AbortController" in html, "缺少 AbortController：接口挂起时页面会永久卡住"
    assert "ac.abort()" in html, "创建了 AbortController 却没有 abort，超时不会触发"


def test_all_endpoints_have_fallback() -> None:
    """load() 里每个接口都必须带 .catch，任一失败都要降级渲染。"""
    html = INDEX.read_text(encoding="utf-8")
    for ep in ("/health", "/health/model", "/audit", "/strength", "/fixtures", "/backtest"):
        i = html.find("get('%s" % ep)
        assert i != -1, f"load() 未请求 {ep}"
        tail = html[i : i + 260]
        assert ".catch(" in tail, f"{ep} 缺少 .catch 兜底：该接口失败会导致整页加载失败"


def test_build_marker_is_static() -> None:
    """页面须含不依赖 JS 的构建标记，便于一眼判断线上版本。"""
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r'id="build"[^>]*>([^<]+)<', html)
    assert m, "缺少静态构建标记 id=\"build\"：无法判断 Railway 是否部署了新版本"
    assert m.group(1).startswith("BUILD-"), f"构建标记格式异常：{m.group(1)!r}"


def test_watchdog_fires_fast_enough() -> None:
    """看门狗需在 10 秒内给出可见提示，不能让用户无限等待。"""
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"\}\s*,\s*(\d+)\)\s*;", html)
    assert m, "未找到看门狗 setTimeout"
    assert int(m.group(1)) <= 10000, f"看门狗 {m.group(1)}ms 太久，用户会以为页面坏了"


def test_static_placeholder_is_distinguishable() -> None:
    """静态占位文案须与「脚本已运行」时的文案不同，否则无法区分卡死与加载中。"""
    html = INDEX.read_text(encoding="utf-8")
    assert "初始化中" in html, "静态占位未改文案：卡死与加载中在视觉上无法区分"


# ---- 渲染引用必须在本函数内声明 -------------------------------------------
# 真实事故（2026-10-26）：周期战绩的 fetch 被误写进诊断函数 runDiagnostics
# 的局部作用域里，而渲染处 periodCard(period, true) 在 load() 内引用它——
# 跨作用域访问不到，抛 ReferenceError: period is not defined，
# **整个 innerHTML 赋值中断，看板全白，只显示「加载失败」**。
#
# 这类错误 node --check 抓不到（语法完全合法），pytest 也抓不到
# （.html 不在扫描范围），只能靠这里的静态作用域检查兜住。

import re as _re


def _load_fn_body() -> str:
    """提取 async function load() 的完整函数体（按大括号配平）。"""
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("async function load(){")
    i = html.index("{", start)
    depth, j = 0, i
    while j < len(html):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return html[i:j + 1]


def _global_functions() -> set[str]:
    """全脚本顶层的 function 声明——它们在任何函数内都可见。"""
    html = INDEX.read_text(encoding="utf-8")
    return set(_re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
                           html, _re.M))


def _declared_in(body: str) -> set[str]:
    """收集函数体内声明的标识符：解构数组 / const / let / var / catch 形参。"""
    names: set[str] = set()
    # catch(err) 的形参——同样是本作用域内可见的局部绑定
    for m in _re.finditer(r"catch\s*\(\s*([A-Za-z_$][\w$]*)\s*\)", body):
        names.add(m.group(1))
    # 解构：const [a, b, c] = ...
    for m in _re.finditer(r"\b(?:const|let|var)\s*\[([^\]]*)\]", body):
        names.update(x.strip() for x in m.group(1).split(",") if x.strip())
    # 解构对象：const {a, b} = ...
    for m in _re.finditer(r"\b(?:const|let|var)\s*\{([^}]*)\}", body):
        names.update(x.strip() for x in m.group(1).split(",") if x.strip())
    # 普通声明：const x = / let x = / var x =
    for m in _re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", body):
        names.add(m.group(1))
    return names


def test_render_references_are_declared_in_load():
    """load() 里渲染用到的变量，必须就在 load() 内声明。

    漏一个就整页白屏——且只有浏览器控制台能看到 ReferenceError。
    """
    body = _load_fn_body()
    declared = _declared_in(body)

    # 渲染段里所有 Card(...) / section(...) 调用的第一个实参标识符
    used = set()
    for m in _re.finditer(r"\b([A-Za-z_$][\w$]*)\((\s*[A-Za-z_$][\w$]*)\s*[,)]", body):
        used.add(m.group(2).strip())

    # 只校验「像局部变量」的小写标识符，放过字面量与内置
    locals_used = {u for u in used if u[:1].islower()}
    visible = declared | _global_functions()
    missing = sorted(u for u in locals_used if u not in visible)
    assert not missing, (
        f"load() 渲染引用了未在本函数声明的变量：{missing}。"
        "跨作用域引用会让整页 innerHTML 赋值抛 ReferenceError 而白屏"
    )


def test_period_is_fetched_inside_load():
    """周期战绩的数据必须在 load() 内取，不能只存在于诊断函数。"""
    body = _load_fn_body()
    assert "/stats/period" in body, (
        "load() 没有请求 /stats/period——周期战绩会拿不到数据，"
        "或引用到别处定义的同名变量而抛 ReferenceError"
    )
    assert "period" in _declared_in(body), "load() 内没有 period 变量"
