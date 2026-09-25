"""统一存储路径 / Unified storage paths.

机器人运行在云端，容器临时目录（./data、/app/data、/tmp）会随重新部署丢失。
所有需要留存的数据必须写到挂载卷，默认是 /data：

    /data/football.db   预测、赛程、比赛结果（必须保存）
    /data/cache         API 缓存（可重建）
    /data/charts        图表 PNG（可重建，默认不落盘）
    /data/exports       导出文件
    /data/backups       数据备份

目录不可写时自动回退到临时目录，并在日志中明确告警，机器人功能不受影响。
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = "/data"  # Railway Volume 默认挂载点


def _resolve(env_key: str, default: Path, fallback_root: Path) -> Path:
    """解析路径：优先环境变量，其次默认，均不可写时回退到临时目录。"""
    raw = (os.getenv(env_key) or "").strip()
    path = Path(raw) if raw else default
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.touch()
        probe.unlink()
        return path
    except Exception as exc:  # 挂载卷没挂上或没权限 → 回退，不让启动失败
        fb = fallback_root / path.name
        log.warning("%s=%s 不可写（%s），回退到 %s（重新部署会丢失）", env_key, path, exc, fb)
        try:
            fb.mkdir(parents=True, exist_ok=True)
        except Exception:
            fb = Path(tempfile.gettempdir())
        return fb


def data_dir() -> Path:
    return _resolve("DATA_DIR", Path(_DEFAULT_DATA_DIR), Path(tempfile.gettempdir()) / "football")


DATA_DIR = data_dir()

DB_PATH = Path(os.getenv("DATABASE_PATH") or str(DATA_DIR / "football.db"))
CACHE_DIR = _resolve("CACHE_DIR", DATA_DIR / "cache", Path(tempfile.gettempdir()) / "football-cache")
CHART_DIR = _resolve("CHART_DIR", DATA_DIR / "charts", Path(tempfile.gettempdir()) / "football-charts")
EXPORT_DIR = _resolve("EXPORT_DIR", DATA_DIR / "exports", Path(tempfile.gettempdir()) / "football-exports")
BACKUP_DIR = _resolve("BACKUP_DIR", DATA_DIR / "backups", Path(tempfile.gettempdir()) / "football-backups")

# 图表默认只在内存生成（BytesIO），避免堆积临时文件；设为 true 才落盘留存
SAVE_CHARTS = (os.getenv("SAVE_CHARTS") or "").strip().lower() in {"1", "true", "yes", "on"}


MARKER_NAME = ".volume_marker"  # 首次写入后保留，用于跨部署验证 Volume 是否生效


def probe_storage() -> dict:
    """存储自检：写→读→比对，并保留一个持久标记文件用于跨部署验证。

    返回写入/读取是否成功、标记文件的首次写入时间与存活时长。
    标记文件不删除：重新部署后仍能读到它，就证明 Volume 真的挂上了。
    """
    from datetime import datetime, timezone

    result = {
        "dir": str(DATA_DIR),
        "write": False,
        "read": False,
        "mounted": False,
        "first_write": None,
        "age_seconds": None,
    }
    marker = DATA_DIR / MARKER_NAME
    now = datetime.now(timezone.utc)

    # 1) 写入测试 + 读取比对（临时文件）
    tmp = DATA_DIR / ".storage_test"
    try:
        tmp.write_text("ok", encoding="utf-8")
        result["write"] = tmp.exists()
        result["read"] = tmp.read_text(encoding="utf-8").strip() == "ok"
        tmp.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("存储写入/读取测试失败：%s", exc)
        return result

    # 2) 持久标记：首次写入记时间，之后只读不改
    try:
        if not marker.exists():
            marker.write_text(now.isoformat(), encoding="utf-8")
            result["first_write"] = now
            result["age_seconds"] = 0.0
        else:
            raw = marker.read_text(encoding="utf-8").strip()
            first = datetime.fromisoformat(raw)
            result["first_write"] = first
            result["age_seconds"] = (now - first).total_seconds()
    except Exception as exc:
        log.warning("写入持久标记失败：%s", exc)
        return result

    result["mounted"] = result["age_seconds"] is not None and (
        result["age_seconds"] > 60 or result["first_write"] is not None
    )
    return result


def persistent() -> bool:
    """当前数据目录是否为真正的挂载卷（False 表示重新部署会丢）。"""
    return str(DATA_DIR).startswith(_DEFAULT_DATA_DIR)


def summary() -> str:
    """供 /status 展示：一眼看出数据落在哪、会不会丢。"""
    return (
        f"{'✅ 挂载卷' if persistent() else '⚠️ 临时目录（重新部署会丢）'} "
        f"DATA_DIR={DATA_DIR} · DB={DB_PATH.name}"
    )
