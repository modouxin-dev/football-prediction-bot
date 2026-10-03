#!/usr/bin/env python3
"""存储自检：独立于机器人的只读检查（总控审计 2026-10-03 · 4.1）。

为什么需要它：`/status` 里的存储信息是**机器人自己报的**——如果机器人
起不来、或数据库本身坏了，你根本看不到那行字。这个脚本不依赖机器人进程，
直接打开数据库文件做检查，回答的是另一个问题：

    「这份数据现在到底还能不能用？」

用法：
    python storage_check.py                    检查默认库
    python storage_check.py /data/football.db  指定库
    DB_PATH=/data/football.db python storage_check.py

退出码：0 = 全部通过；1 = 存在 ERROR 级问题。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time


def _db_path(argv: list[str]) -> str:
    if len(argv) > 1:
        return argv[1]
    return os.getenv("DB_PATH") or os.getenv("DATABASE_PATH") or "/data/football.db"


def main() -> int:
    db = _db_path(sys.argv)
    errs: list[str] = []
    warns: list[str] = []

    def ok(msg):
        print(f"  [OK]   {msg}")

    def warn(msg):
        warns.append(msg)
        print(f"  [WARN] {msg}")

    def err(msg):
        errs.append(msg)
        print(f"  [ERR]  {msg}")

    print(f"存储自检 · {db}")
    print("-" * 60)

    # 1. 文件存在与可读
    if not os.path.exists(db):
        err(f"数据库文件不存在：{db}")
        print("-" * 60)
        print(f"结果：{len(errs)} 个错误 / {len(warns)} 个警告")
        return 1
    size = os.path.getsize(db)
    ok(f"文件存在 · {size / 1024:.1f} KB · 修改于 "
       f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(db)))}")
    if size == 0:
        err("文件大小为 0，数据库已损坏")
        return 1

    con = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
        # 2. 完整性
        row = con.execute("PRAGMA integrity_check").fetchone()
        if row and row[0] == "ok":
            ok("完整性校验通过（integrity_check = ok）")
        else:
            err(f"完整性校验失败：{row[0] if row else '无返回'}")

        # 3. 核心表
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for need in ("matches", "predictions"):
            if need in tables:
                n = con.execute(f"SELECT count(*) FROM {need}").fetchone()[0]
                ok(f"表 {need} 存在 · {n} 行")
            else:
                err(f"缺少核心表：{need}")

        # 4. 赛果可用性（强度估计的命脉）
        if "matches" in tables:
            fin = con.execute(
                "SELECT count(*) FROM matches WHERE home_score IS NOT NULL"
            ).fetchone()[0]
            if fin >= 60:
                ok(f"已完赛 {fin} 场（≥60，强度榜样本充足）")
            elif fin > 0:
                warn(f"已完赛仅 {fin} 场（<60，强度估计不可靠）")
            else:
                err("已完赛 0 场，无法估计任何球队强度")

        # 5. WAL 模式（持久化设计的一部分）
        jm = con.execute("PRAGMA journal_mode").fetchone()[0]
        if str(jm).lower() == "wal":
            ok("journal_mode = WAL")
        else:
            warn(f"journal_mode = {jm}（预期 WAL）")
    except sqlite3.Error as exc:
        err(f"打开/查询数据库失败：{exc}")
    finally:
        if con is not None:
            con.close()

    # 6. 备份与恢复能力（有备份无恢复 = 没有备份）
    backup_dir = os.getenv("BACKUP_DIR") or "/data/backups"
    if os.path.isdir(backup_dir):
        bks = sorted(f for f in os.listdir(backup_dir) if f.startswith("football-"))
        if bks:
            ok(f"备份目录 {backup_dir} · {len(bks)} 份，最新 {bks[-1]}")
        else:
            warn(f"备份目录 {backup_dir} 存在但没有任何备份")
    else:
        warn(f"备份目录不存在：{backup_dir}（从未备份过）")

    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, "restore.sh")):
        ok("恢复脚本 restore.sh 存在")
    else:
        err("缺少 restore.sh —— 有备份无恢复，等于没有备份")

    # 7. 磁盘余量
    try:
        st = os.statvfs(os.path.dirname(db) or "/")
        free_mb = st.f_bavail * st.f_frsize / 1024 / 1024
        if free_mb < 100:
            err(f"磁盘剩余仅 {free_mb:.0f} MB（<100MB，写入会失败）")
        elif free_mb < 500:
            warn(f"磁盘剩余 {free_mb:.0f} MB（偏紧）")
        else:
            ok(f"磁盘剩余 {free_mb:.0f} MB")
    except OSError:
        pass

    print("-" * 60)
    print(f"结果：{len(errs)} 个错误 / {len(warns)} 个警告")
    if errs:
        print("ERROR 级问题需立即处理：")
        for e in errs:
            print(f"  - {e}")
        return 1
    if warns:
        print("无 ERROR；警告项可择机处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
