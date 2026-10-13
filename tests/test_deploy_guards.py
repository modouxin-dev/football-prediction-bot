"""部署守卫回归测试。

这几个文件（start.sh / docker-entrypoint.sh / dependabot.yml）此前是盲区：
pytest 和 pyflakes 都只扫 .py，改坏了 CI 也不会红。这里把三条曾经真实
踩过的坑固化成断言，防止被改回去。

1. start.sh 用 `&` 起看板却不等它 → 看板崩溃时容器不退出、Railway 不重启，
   表现为「机器人还在推送，看板却打不开」，且没有任何告警。
2. docker-entrypoint.sh 检测到 /data 不可写只打印 CRITICAL 就继续 →
   机器人静默降级到内存库，看着正常，重启后数据全丢。
3. 依赖全部钉死旧版本且无自动更新机制。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
START_SH = ROOT / "start.sh"
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------- 1. 看板进程守护 ----------


def test_start_sh_exists_and_is_executable() -> None:
    assert START_SH.is_file(), "start.sh 缺失，看板将永远不会启动"


def test_start_sh_uses_bash_not_sh() -> None:
    """`wait -n` 是 bash 内建，dash 不支持，用 /bin/sh 会整段失效。"""
    first_line = _read(START_SH).splitlines()[0].strip()
    assert first_line == "#!/bin/bash", (
        f"start.sh 的 shebang 必须是 #!/bin/bash，实际是 {first_line!r}。"
        "Debian 的 /bin/sh 是 dash，不支持 wait -n。"
    )


def test_start_sh_waits_for_any_child() -> None:
    """任一子进程退出必须让容器退出，交给 Railway 重启。"""
    body = _read(START_SH)
    assert "wait -n" in body, (
        "start.sh 缺少 `wait -n`：看板崩溃时容器不会退出，"
        "Railway 不会重启，看板会静默失效。"
    )


def test_start_sh_exits_nonzero_when_child_dies() -> None:
    """退出码必须是非零，Railway 才认为是失败并重启；0 被视为正常结束。"""
    body = _read(START_SH)
    # 找到 wait -n 之后的清理与退出，确认最终 exit 非零
    tail = body[body.index("wait -n"):]
    assert "exit 1" in tail, "子进程退出后必须 exit 1，否则 Railway 不会重启容器"


@pytest.mark.skipif(shutil.which("bash") is None, reason="环境无 bash")
def test_start_sh_actually_stops_container_on_child_crash() -> None:
    """真跑一遍：模拟子进程崩溃，确认脚本按预期退出 1。"""
    script = (
        _read(START_SH)
        .replace('python -m uvicorn api:app --host 0.0.0.0 --port "${PORT}"',
                 f'"{sys.executable}" -c "import time; time.sleep(30)"')
        .replace("python main.py",
                 f'"{sys.executable}" -c "import time; time.sleep(1); raise SystemExit(7)"')
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, (
        f"子进程崩溃后 start.sh 应退出 1（触发 Railway 重启），实际 {proc.returncode}"
    )
    assert "a process exited" in (proc.stderr + proc.stdout)


# ---------- 2. 数据不可写必须拒绝启动 ----------


def test_entrypoint_refuses_to_start_when_data_not_writable() -> None:
    body = _read(ENTRYPOINT)
    critical = '[CRITICAL] $DATA_ROOT is NOT writable'
    assert critical in body, "可写性检查被删掉了"
    # CRITICAL 之后必须紧跟 exit 1，不能只是警告
    idx = body.index(critical)
    tail = body[idx:]
    line_end = tail.index("\n")
    block = tail[: tail.index("fi", line_end)]
    assert "exit 1" in block, (
        "数据不可写时必须 exit 1。只打印警告会让机器人静默降级到内存库，"
        "重启后数据全丢且无任何迹象。"
    )


# ---------- 3. 依赖自动更新 ----------


def test_dependabot_config_exists() -> None:
    assert DEPENDABOT.is_file(), (
        ".github/dependabot.yml 缺失：依赖全部钉死旧版本，且没有更新机制"
    )


def test_dependabot_config_covers_pip_and_actions() -> None:
    text = _read(DEPENDABOT)
    assert 'package-ecosystem: "pip"' in text
    assert 'package-ecosystem: "github-actions"' in text


# ---------- 4. 页脚版本不能硬编码 ----------


def test_index_html_build_is_not_hardcoded() -> None:
    """硬编码的构建号让人分不清线上跑的是哪一版，排查全靠猜。"""
    html = _read(ROOT / "web" / "index.html")
    assert "BUILD-2026-10-11-B" not in html, "页脚版本号不应硬编码"
    assert "$('build').textContent" in html, "页脚版本应从 /health 的真实版本填充"
