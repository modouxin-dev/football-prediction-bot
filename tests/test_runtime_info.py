"""进程运行时信息 / Process runtime info.

对应缺陷：``/health`` 与 ``/status`` 都只有 version（代码版本），
回答不了「这个实例是不是新部署的那个」——同一 commit 重新部署、或旧容器
没被替换时，版本号一模一样，看上去「已更新」，实际跑的还是老进程。

反向验证记录（改动回退后本文件应有多少条失败）：
    把 api.py 的三个新字段删掉 → ``test_health_exposes_runtime_fields`` 失败；
    把 admin.py 的两行删掉 → ``test_status_shows_platform_and_uptime`` 失败。
    若这两条在回退后仍通过，说明断言写成了空跑，必须重修。
"""
import pytest

import runtime_info


# ── 平台识别 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "env,expected",
    [
        ({"RAILWAY_ENVIRONMENT": "production"}, "railway"),
        ({"RAILWAY_PROJECT_ID": "abc"}, "railway"),
        ({"FLY_APP_NAME": "football-prediction-bot"}, "fly"),
        ({"FLY_MACHINE_ID": "m1"}, "fly"),
        ({"RENDER_SERVICE_NAME": "srv"}, "render"),
        ({}, "local"),
    ],
)
def test_platform_detection(monkeypatch, env, expected):
    """识别依据是各平台注入的环境变量；认不出时退化为 local，不得抛错。"""
    # 先清干净，避免宿主环境（例如本地跑在 Docker 里）污染判定结果
    for key in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID",
                "FLY_APP_NAME", "FLY_MACHINE_ID", "RENDER", "RENDER_SERVICE_NAME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert runtime_info.platform() == expected


# ── 运行时长 ──────────────────────────────────────────────────────────
def test_uptime_is_non_negative_and_monotonic():
    first = runtime_info.uptime_seconds()
    assert first >= 0
    second = runtime_info.uptime_seconds()
    # 单调时钟：后取的必然 >= 先取的，绝不能因为系统时间被改而倒退
    assert second >= first


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0秒"),
        (45, "45秒"),
        (60, "1分"),
        (125, "2分"),
        (3600, "1小时"),
        (3660, "1小时1分"),
        (86400, "1天"),
        (90000, "1天1小时"),
    ],
)
def test_uptime_human_format(seconds, expected):
    assert runtime_info.uptime_human(seconds) == expected


def test_started_at_is_aware_utc():
    started = runtime_info.started_at()
    assert started.tzinfo is not None
    assert started.utcoffset().total_seconds() == 0


# ── 端点真的带上了这些字段 ────────────────────────────────────────────
def test_health_exposes_runtime_fields():
    """断言的是载荷内容，不是源码文本——文本匹配会被注释里的同名字符串骗过。"""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import api

    with TestClient(api.app) as client:
        body = client.get("/health").json()

    assert body["platform"] == runtime_info.platform()
    # started_at 必须是可解析的 ISO 时间，不是占位字符串
    from datetime import datetime

    parsed = datetime.fromisoformat(body["started_at"])
    assert parsed.tzinfo is not None
    # uptime 必须是整数秒：前端/脚本拿去做数值比较，不能是浮点或字符串
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0


def test_status_shows_platform_and_uptime():
    """/status 必须能看出平台与启动时刻。

    Fly.io 那份只跑 main.py、不监听端口，没有 HTTP 端点可查，
    只有 Telegram 的 /status 能暴露它——这是排查「幽灵实例在偷额度」的关键。

    用 AST 而不是字符串匹配：字符串匹配会被注释里的同名文本骗过
    （本项目此前就真实踩过一次——测试断言源码含某标识符，而注释里恰好
    写了同一个词，结果改动回退后测试仍通过，等于空跑）。AST 只认真实调用节点。
    """
    import ast
    from pathlib import Path

    import commands.admin as admin

    tree = ast.parse(Path(admin.__file__).read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "runtime_info"
        ):
            called.add(func.attr)

    assert "platform" in called, "status_cmd 未调用 runtime_info.platform()"
    assert "started_at" in called, "status_cmd 未调用 runtime_info.started_at()"
    assert "uptime_human" in called, "status_cmd 未调用 runtime_info.uptime_human()"
