import os
import logging
import tempfile
import time
from datetime import datetime
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

# 跨部署标记：写在 DATA_DIR 里，重新部署后仍在即证明持久卷生效
MARKER_NAME = ".volume_marker"


def _is_mount_point(path: Path) -> bool:
    """判断目录是否为独立挂载点（Volume / Persistent Disk）。"""
    try:
        target = path.resolve()
    except Exception:
        return False
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 1 and Path(parts[1]) == target:
                    return True
    except OSError:
        pass  # 非 Linux（无 /proc/mounts）走下面的退化判断
    except Exception as exc:
        log.warning("读取挂载信息失败：%s", exc)
    # 退化判断：不在系统临时目录下，才有可能是用户挂的持久卷
    return Path(tempfile.gettempdir()) not in target.parents


def probe_storage() -> dict:
    """存储自检：写入、读取、挂载状态、标记文件存活时长。

    标记文件只在首次创建时写入时间戳；之后每次探测都保留原值，
    因此 age_seconds 反映的是「这份数据活了多久」，而不是「上次探测距今多久」。
    """
    payload = f"probe:{time.time_ns()}"
    test_file = DATA_DIR / ".write_test"
    ok_write = ok_read = False
    try:
        test_file.write_text(payload, encoding="utf-8")
        ok_write = True
        ok_read = test_file.read_text(encoding="utf-8") == payload
        test_file.unlink()
    except Exception as exc:
        log.warning("存储探针失败：%s", exc)

    marker = DATA_DIR / MARKER_NAME
    created_at = None
    first_write = None
    if marker.exists():
        try:
            raw = marker.read_text(encoding="utf-8").strip()
            created_at = datetime.fromisoformat(raw).timestamp()
            first_write = raw
        except Exception:
            try:
                created_at = marker.stat().st_mtime
                first_write = datetime.fromtimestamp(created_at).isoformat()
            except OSError:
                created_at = None
    if created_at is None:
        try:
            first_write = datetime.now().isoformat()
            marker.write_text(first_write, encoding="utf-8")
            created_at = time.time()
        except Exception as exc:
            log.warning("写入卷标记失败：%s", exc)
            first_write = None

    return {
        "write": ok_write,
        "read": ok_read,
        "mounted": _is_mount_point(DATA_DIR),
        # first_write：标记文件的首次写入时间（ISO），重新部署后应保持不变
        "first_write": first_write,
        # age_seconds：这份数据活了多久；首次执行为 0
        "age_seconds": None if created_at is None else max(0.0, time.time() - created_at),
        "dir": str(DATA_DIR),
        "db": str(DB_PATH),
    }


def is_persistent() -> bool:
    """数据目录是否跨重启保留。

    判定基准是「不在系统临时目录下」：Volume 未挂载时 DATA_DIR 会回退到
    /tmp（或系统临时目录），此时预测、赛果、Elo 评分都会随容器重启清零。

    不依赖 /proc/mounts 判定：部分环境下 /tmp 本身也是挂载点，
    而自托管的 bind mount 未必出现在挂载表内，用挂载点判断会两边都误判。
    """
    try:
        target = DATA_DIR.resolve()
    except Exception:
        target = DATA_DIR
    tmp = Path(tempfile.gettempdir())
    return tmp != target and tmp not in target.parents


def persistence_warning() -> str | None:
    """返回持久化未生效时的警告文案，已生效则返回 None。"""
    if is_persistent():
        return None
    return (
        f"数据目录 {DATA_DIR} 不是持久卷：容器重启后预测、赛果、Elo 评分将全部清零。"
        "Railway 请新建 Volume 并挂载到 /data；自托管请确认 docker-compose 的 ./data:/data。"
    )


def _fmt_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def summary() -> str:
    """一行存储摘要，供 /status 展示。"""
    try:
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except OSError:
        size = 0
    mounted = _is_mount_point(DATA_DIR)
    kind = "挂载卷" if mounted else "临时目录"
    return f"DATA_DIR={DATA_DIR}（{kind}）· DB {_fmt_bytes(size)}"
