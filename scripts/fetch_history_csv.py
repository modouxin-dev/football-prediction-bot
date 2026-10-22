#!/usr/bin/env python3
"""下载五大联赛历史赛果 CSV 到 data/history/。

为什么需要这个脚本
------------------
backtest_corpus 已支持五大联赛，但**光有代码没有数据等于没支持**：
镜像里 data/history/ 当前只有 E0_2023/2024/2025（英超），
西甲/意甲/德甲/法甲的回测样本仍是 0，覆盖盲区依然存在。

本脚本负责把缺的 CSV 拉下来。跑完后 `available_divs()` 才会返回五个分区，
回测输出的 `by_div` 里西甲/意甲/德甲/法甲才会出现非零数字。

用法
----
    # 拉全部五个联赛的近三个赛季（默认 2023/2024/2025）
    python scripts/fetch_history_csv.py

    # 只拉指定分区 / 赛季
    python scripts/fetch_history_csv.py --divs SP1,I1 --seasons 2023,2024

    # 只看看会下载什么，不真的下
    python scripts/fetch_history_csv.py --dry-run

    # 已存在的文件也重新覆盖
    python scripts/fetch_history_csv.py --force

退出码：全部成功 0；任一失败 1（已成功的保留，可直接重跑）。

⚠️ 需要出口网络能访问 www.football-data.co.uk。
   若所在环境（如受限容器）返回 403，请换一台能出网的机器跑，
   再把生成的 CSV 提交进仓库——这些 CSV 是随代码进镜像的。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 允许以脚本方式直接运行（无需先安装包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest_corpus import DEFAULT_DIVS  # noqa: E402
from football_data_uk import DIV_TO_COMPETITION, fetch_season, history_dir  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("fetch_history")

DEFAULT_SEASONS = [2023, 2024, 2025]


async def _fetch_one(div: str, season: int, out_dir: Path,
                     force: bool) -> tuple[str, int, bool]:
    dest = out_dir / f"{div}_{season}.csv"
    if dest.is_file() and not force:
        log.info("跳过（已存在）%s", dest.name)
        return div, season, True
    try:
        text = await fetch_season(season, div)
    except Exception as exc:
        log.error("下载失败 %s_%s：%s", div, season, exc)
        return div, season, False
    if not text.strip():
        log.error("下载为空 %s_%s", div, season)
        return div, season, False
    dest.write_text(text, encoding="utf-8")
    log.info("已写入 %s（%d 字节）", dest.name, len(text))
    return div, season, True


async def run(divs: list[str], seasons: list[int], force: bool,
              dry_run: bool) -> int:
    out_dir = history_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(d, s) for d in divs for s in seasons]
    if dry_run:
        for div, season in jobs:
            comp = DIV_TO_COMPETITION.get(div, "?")
            print(f"  将下载 {div}({comp})_{season}.csv -> {out_dir}")
        return 0

    results = []
    for div, season in jobs:
        results.append(await _fetch_one(div, season, out_dir, force))

    ok = sum(1 for _, _, s in results if s)
    fail = len(results) - ok
    log.info("完成：成功 %d / 失败 %d", ok, fail)
    if fail == 0:
        from backtest_corpus import available_divs  # 延迟导入：读的是新落盘的文件
        log.info("当前可用分区：%s", available_divs())
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="下载五大联赛历史 CSV")
    ap.add_argument("--divs", default=",".join(DEFAULT_DIVS),
                    help="分区代号，逗号分隔（默认五大联赛）")
    ap.add_argument("--seasons", default=",".join(map(str, DEFAULT_SEASONS)),
                    help="赛季起始年份，逗号分隔")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = ap.parse_args()

    divs = [d.strip().upper() for d in args.divs.split(",") if d.strip()]
    unknown = [d for d in divs if d not in DIV_TO_COMPETITION]
    if unknown:
        log.error("未知分区代号：%s（已知：%s）", unknown,
                  sorted(DIV_TO_COMPETITION))
        return 1
    seasons = []
    for raw in args.seasons.split(","):
        raw = raw.strip()
        if raw.isdigit():
            seasons.append(int(raw))
    if not seasons:
        log.error("没有有效赛季")
        return 1

    return asyncio.run(run(divs, seasons, args.force, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
