import os
import logging
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Render Persistent Disk 默认挂载点
_DEFAULT_DATA_DIR = Path("/data")

def _resolve(env_key, default, fallback_root):
    raw = (os.getenv(env_key) or "").strip()
    path = Path(raw) if raw else default
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.touch(); probe.unlink()
        return path
    except Exception as exc:
        fb = fallback_root / path.name
        log.warning(f"{env_key}={path} 不可写 ({exc})，回退至 {fb}")
        try:
            fb.mkdir(parents=True, exist_ok=True)
        except Exception:
            fb = Path(tempfile.gettempdir()) / path.name
        return fb

# 核心路径定义
DATA_DIR = _resolve("DATA_DIR", _DEFAULT_DATA_DIR, Path("/tmp"))
DB_PATH = Path(os.getenv("DATABASE_PATH") or (DATA_DIR / "football.db"))
CACHE_DIR = _resolve("CACHE_DIR", DATA_DIR / "cache", DATA_DIR)
CHART_DIR = _resolve("CHART_DIR", DATA_DIR / "charts", DATA_DIR)
EXPORT_DIR = _resolve("EXPORT_DIR", DATA_DIR / "exports", DATA_DIR)
BACKUP_DIR = _resolve("BACKUP_DIR", DATA_DIR / "backups", DATA_DIR)
