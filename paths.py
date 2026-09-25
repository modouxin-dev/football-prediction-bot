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


def persistent() -> bool:
    """当前数据目录是否为真正的挂载卷（False 表示重新部署会丢）。"""
    return str(DATA_DIR).startswith(_DEFAULT_DATA_DIR)


def summary() -> str:
    """供 /status 展示：一眼看出数据落在哪、会不会丢。"""
    return (
        f"{'✅ 挂载卷' if persistent() else '⚠️ 临时目录（重新部署会丢）'} "
        f"DATA_DIR={DATA_DIR} · DB={DB_PATH.name}"
    )
