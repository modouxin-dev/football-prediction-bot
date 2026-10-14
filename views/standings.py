"""积分榜视图 / Standings view

本文件内的文案与生成它的逻辑同在一处，改文案只动这一个文件。
"""

from __future__ import annotations

from formatkit import (_row_summary, align_cjk, esc, pad_cjk, rank_marker,
                       team_short_name)
from templates import DISCLAIMER, NO_DATA, SEP
from views.common import CommonView

# 列宽：队名最长 14 列（7 个汉字），其余按内容定宽。
# 战绩取 6 列：胜平负都可能是两位数（12-2-1），给 5 列会被截成 12-2… —— 丢数据。
# 整行 = 名次 2 + 间隔 1 + 队名 14 + 1 + 积分 4 + 1 + 战绩 6 + 1 + 净胜 4 = 34 列，
# 小于 MOBILE_MAX_COLS(36)，手机上不会折行（有测试锁死）。
TEAM_W = 14
PTS_W, REC_W, DIFF_W = 4, 6, 4


class StandingsView:
    @staticmethod
    def format_standings_page(rows: list[dict], tz, limit: int = 20, league_name: str = "", updated=None) -> str:
        """联赛排名：整表进等宽块做列对齐，排头用 1️⃣~🔟 让名次纵向成列。

        参数名用 league_name 而非 league_label：后者是 formatkit 导出的**函数**，
        同名会在本作用域内把它遮蔽掉（pyflakes W0404 场景），
        一旦函数体内想调用 league_label(...) 就会拿到字符串而崩溃。
        """
        lines = ["🏆 <b>联赛排名</b>"]
        if league_name:
            lines.append(f"🏅 <code>{esc(league_name)}</code>")
        if updated is not None:
            lines.append(
                f"🕑 <code>UPDATED: {CommonView.fmt_time(updated, tz, '%m-%d %H:%M')}</code>"
            )
        lines.append(SEP)
        if not rows:
            lines.append(NO_DATA)
            lines += [SEP, DISCLAIMER]
            return "\n".join(lines)

        shown = rows[:limit]
        # 表头 + 每行同进 <code> 等宽块：Telegram 正文是比例字体，只有等宽块里
        # pad_cjk 按显示宽度补的空格才是「真正的列」（见 formatkit.kv_line）。
        # 排头符号用 1️⃣~🔟/两位数字，宽度恒为 2 列，纵向自然成列。
        lines.append(
            f"<code>{'  '} {pad_cjk('球队', TEAM_W)} {pad_cjk('积分', PTS_W)}"
            f" {pad_cjk('战绩', REC_W)} {pad_cjk('净胜', DIFF_W)}</code>"
        )
        for i, row in enumerate(shown, start=1):
            summary = _row_summary(row)
            name = esc(team_short_name((row.get("team") or {}).get("name")))
            rank = (summary or {}).get("rank") or i
            pts = (summary or {}).get("points")
            # 无积分时只显示「-」，不显示孤立的「分」字
            points = align_cjk(f"{int(pts)}分" if pts is not None else "-", PTS_W, "right")
            record = f"{summary['win']}-{summary['draw']}-{summary['lose']}" if summary else "-"
            # 净胜球：优先用 API 的 goalsDiff，缺了就用场均进失球 × 场次反推
            gd = row.get("goalsDiff")
            if gd is None and summary and summary["avg_for"] is not None \
                    and summary["avg_against"] is not None:
                gd = round((summary["avg_for"] - summary["avg_against"]) * summary["played"])
            diff_txt = align_cjk("-" if gd is None else f"{int(gd):+d}", DIFF_W, "right")
            lines.append(
                f"<code>{rank_marker(rank)} {pad_cjk(name, TEAM_W)} {points}"
                f" {align_cjk(record, REC_W)} {diff_txt}</code>"
            )
        lines += [SEP, DISCLAIMER]
        return "\n".join(lines)
