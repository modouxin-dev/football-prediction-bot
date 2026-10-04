#!/usr/bin/env python3
"""队名批量体检 / Team-name bulk audit.

回答一个问题：库里这几百条队名，到底哪些是错的、哪些会在用户眼前出问题？

检测分两类：

  A. 静态自检（不需要网络、不需要 API key，秒出结果）

    self-hit     每个已收录原名，能否命中「自己」的中文名。
                 命中到别人 = 张冠李戴，是最严重的一类 bug。
    collision    归一化后撞同一个键、但中文名不同 —— 假命中的温床。
    dup-key      源码里同一个键写了两次（Python dict 静默覆盖，肉眼看不见）。
    dup-cn       不同队映射到同一个中文名 —— 用户根本分不清谁是谁。
    width        中文名超过移动端列宽，卡片里会被截断成「…」。
    hygiene      空值 / 夹带英文 / 首尾空格 等卫生问题。
    confusable   中文名互为子串（易混，仅提示不判错）。

  B. 覆盖检测（需要真实队名语料）

    coverage     真实队名跑一遍，列出命中不了的。语料三选一：
                 --fetch        调 API-Football 拉真实队名（需要 key）
                 --from-db      从项目 SQLite 反查历史出现过的队名（零成本）
                 --from-file    每行一个队名

用法::

    python tools/team_name_audit.py                    # 只跑静态自检
    python tools/team_name_audit.py --from-db app.db   # 加覆盖检测
    python tools/team_name_audit.py --fetch            # 拉真实队名（需 key）
    python tools/team_name_audit.py --json out.json    # 机器可读结果

退出码：有 ERROR 级问题返回 1，可挂 CI 当门禁。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sqlite3
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from templates import LEAGUE_NAMES, TEAM_NAMES  # noqa: E402
from formatkit import (  # noqa: E402
    _TEAM_INDEX,
    _team_key,
    display_width,
    team_short_name,
)

MOBILE_COLS = 16  # team_mobile 默认列宽，超过即被截断
APISPORTS = "https://v3.football.api-sports.io"

# 杯赛类联赛：/teams 端点通常返回空，拉取时跳过
CUP_LEAGUES = {1, 2, 3, 4, 848}

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, level: str, check: str, message: str, **extra) -> None:
        self.rows.append({"level": level, "check": check,
                          "message": message, **extra})

    def of(self, level: str) -> list[dict]:
        return [r for r in self.rows if r["level"] == level]

    @property
    def error_count(self) -> int:
        return len(self.of(ERROR))


# ---------------------------------------------------------------- 静态自检


def check_self_hit(rep: Report) -> None:
    """已收录原名能否命中自己的中文名。命中别人 = 张冠李戴。

    注意：因为 team_short_name 是「精确匹配优先」，已收录的键理论上恒能命中
    自己，这一项通常是 0 条。它的价值是**回归保护**——一旦有人改坏归一化
    让精确匹配失效，这里会立刻炸出来，而不是等用户在机器人里看到怪队名。
    """
    for raw, cn in TEAM_NAMES.items():
        got = team_short_name(raw)
        if got != cn:
            rep.add(ERROR, "self-hit",
                    f"{raw!r} 应显示「{cn}」，实际显示「{got}」",
                    raw=raw, expected=cn, actual=got)


def check_collision(rep: Report) -> None:
    """归一键相同但中文名不同 —— 归一化会把其中一个劫持到另一个。"""
    groups: dict[str, list[str]] = {}
    for raw in TEAM_NAMES:
        groups.setdefault(_team_key(raw), []).append(raw)
    for key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        cns = {TEAM_NAMES[m] for m in members}
        if len(cns) > 1:
            detail = " / ".join(f"{m}→{TEAM_NAMES[m]}" for m in sorted(members))
            # 撞车的键按设计**不进索引**（见 _build_team_index）：这些队
            # 的未收录变体会退回英文原名，而不是被显示成另一支队的中文名。
            # 所以这里不再是 ERROR（张冠李戴），而是 WARN（已知缺口）：
            # 想让它们显示中文，只能逐条补收录，不能靠归一化猜。
            isolated = key not in _TEAM_INDEX
            level = WARN if isolated else ERROR
            rep.add(level, "collision",
                    f"归一键 {key!r} 撞车：{detail}；"
                    + ("已隔离（不进索引），未收录变体退回英文原名"
                       if isolated else
                       "索引仍留了代表，其余队的未收录变体会被误显示成它"),
                    key=key, members=sorted(members), isolated=isolated)
        else:
            # 同一支队的不同写法，正常，仅提示
            rep.add(INFO, "alias",
                    f"归一键 {key!r} 的多种写法：{' / '.join(sorted(members))}",
                    key=key, members=sorted(members))


def check_dup_key(rep: Report) -> None:
    """源码里同一个键写了两次 —— Python dict 静默保留后者，肉眼看不见。"""
    path = os.path.join(ROOT, "templates.py")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except OSError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "TEAM_NAMES" not in targets or not isinstance(node.value, ast.Dict):
            continue
        seen: dict[str, int] = {}
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                seen[k.value] = seen.get(k.value, 0) + 1
        for name, times in sorted(seen.items()):
            if times > 1:
                rep.add(ERROR, "dup-key",
                        f"键 {name!r} 在源码里定义了 {times} 次（后者覆盖前者）",
                        raw=name, times=times)


def _is_alias(a: str, b: str) -> bool:
    """两个原名是否像「同一支队的不同写法」。

    判据：归一化后的 token 集合相等、或一个是另一个的子集。
    ``Atalanta`` ⊆ ``Atalanta BC`` → 同队；``Inter`` 与
    ``FC Internazionale Milano`` 无子集关系 → 判不出来，交给人工。

    为什么不直接判「不同就是错」：词缀表永远补不全（BC / SFC / FR / SE…），
    用不完整的归一化去下 ERROR 结论，只会淹没真正的错误。
    """
    ta, tb = set(_team_key(a).split()), set(_team_key(b).split())
    return ta == tb or ta < tb or tb < ta


def check_dup_cn(rep: Report) -> None:
    """中文名被多个原名共用 —— 可能是同队异名，也可能是真的录错了。

    机器能确定「是同一队」的降为 INFO；确定不了的只给 WARN 请人工看，
    **不冒充 ERROR**：这一项误报代价太高，宁可让人扫一眼。
    """
    groups: dict[str, list[str]] = {}
    for raw, cn in TEAM_NAMES.items():
        groups.setdefault(cn, []).append(raw)
    for cn, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        same = all(_is_alias(a, b) for a in members for b in members)
        level = INFO if same else WARN
        tag = "（同一队的多种写法）" if same else "（写法差异较大，请人工确认是否同一队）"
        rep.add(level, "dup-cn",
                f"「{cn}」被 {len(members)} 个原名共用：{' / '.join(sorted(members))}{tag}",
                cn=cn, members=sorted(members), same_team=same)


def check_width(rep: Report) -> None:
    """中文名超过移动端列宽会在卡片里被截断成「…」。"""
    for raw, cn in sorted(TEAM_NAMES.items()):
        w = display_width(cn)
        if w > MOBILE_COLS:
            rep.add(WARN, "width",
                    f"「{cn}」宽 {w} 列 > {MOBILE_COLS}，移动端显示为「{team_short_name(raw)[:6]}…」",
                    raw=raw, cn=cn, width=w)


def _cjk_count(text: str) -> int:
    return sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))


def check_hygiene(rep: Report) -> None:
    """卫生问题：空值、没翻译、首尾空格、异常字符。

    关于「夹带英文」：AC米兰 / 东京FC / 巴黎FC 都是通行译名，夹英文不等于
    没翻完。只有**一个汉字都没有**才算真没翻译，其余一律不报——判据宁缺
    毋滥，否则报告会被几十条噪音淹掉。
    """
    for raw, cn in TEAM_NAMES.items():
        if not cn or not cn.strip():
            rep.add(ERROR, "hygiene", f"{raw!r} 的中文名为空", raw=raw)
            continue
        if _cjk_count(cn) == 0:
            rep.add(ERROR, "hygiene",
                    f"{raw!r} 的中文名「{cn}」不含任何汉字，等于没翻译",
                    raw=raw, cn=cn)
        if cn != cn.strip():
            rep.add(WARN, "hygiene", f"{raw!r} 的中文名有首尾空格：{cn!r}", raw=raw, cn=cn)
        # 键带首尾空格：精确匹配会失败，靠归一化兜底能救回来（实测
        # team_short_name('Sparta Rotterdam') 仍能命中），所以只算瑕疵
        if raw != raw.strip():
            rep.add(WARN, "hygiene",
                    f"键 {raw!r} 带首尾空格：精确匹配失效，目前靠归一化兜底",
                    raw=raw)
        if "…" in cn or cn.endswith("."):
            rep.add(WARN, "hygiene", f"{raw!r} 的中文名带省略号或句点：{cn!r}", raw=raw, cn=cn)


def check_unstable_key(rep: Report) -> None:
    """归一化键不稳定：含 NFKD 拆不开的字母（ø æ þ ð …）。

    这几个字母没有兼容分解，会被 ``[^a-z0-9]+`` 当成分隔符，把一个完整单词
    劈成两半：``Lillestrøm SK`` → ``lillestr m``、``Bodø/Glimt`` → ``bod glimt``。
    后果不是显示错，而是**同队的两种写法归一不到一起**，只能靠逐条硬收录，
    北欧联赛（挪超/丹超/瑞超）的队名因此格外脆。
    """
    for raw in sorted(TEAM_NAMES):
        bad = [
            ch for ch in raw
            if ord(ch) > 127
            and unicodedata.category(ch).startswith("L")
            and unicodedata.normalize("NFKD", ch) == ch
        ]
        if bad:
            rep.add(WARN, "unstable-key",
                    f"{raw!r} 含不可分解字母 {'/'.join(sorted(set(bad)))}"
                    f" → 归一键被劈成 {_team_key(raw)!r}",
                    raw=raw, chars=sorted(set(bad)), key=_team_key(raw))


def check_confusable(rep: Report, limit: int = 12) -> None:
    """中文名互为子串 —— 容易看错，也常是录入时手滑的信号。"""
    names = sorted(set(TEAM_NAMES.values()))
    pairs = []
    for a in names:
        for b in names:
            if a != b and a in b and len(a) >= 2:
                pairs.append((a, b))
    for a, b in pairs[:limit]:
        rep.add(INFO, "confusable", f"「{a}」是「{b}」的子串", short=a, long=b)
    if len(pairs) > limit:
        rep.add(INFO, "confusable", f"另有 {len(pairs) - limit} 对子串关系未列出", )


# ---------------------------------------------------------------- 语料来源


def corpus_from_db(path: str) -> dict[str, str]:
    """从项目 SQLite 反查历史出现过的队名 —— 零成本、与主流程同源。"""
    out: dict[str, str] = {}
    con = sqlite3.connect(path)
    stmts = [
        ("matches", "SELECT home_team_name, competition_code FROM matches"),
        ("matches", "SELECT away_team_name, competition_code FROM matches"),
        ("predictions", "SELECT home, league FROM predictions"),
        ("predictions", "SELECT away, league FROM predictions"),
    ]
    for table, sql in stmts:
        try:
            for name, comp in con.execute(sql):
                if name and str(name).strip():
                    out.setdefault(str(name).strip(), str(comp or ""))
        except sqlite3.Error:
            continue
    con.close()
    return out


def corpus_from_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            name = line.strip()
            if name and not name.startswith("#"):
                out.setdefault(name, "")
    return out


def corpus_from_api(leagues: list[int], season: int, key: str) -> dict[str, str]:
    """调 API-Football /teams 拉真实队名全集 —— 最权威的 ground truth。"""
    out: dict[str, str] = {}
    for lid in leagues:
        url = f"{APISPORTS}/teams?league={lid}&season={season}"
        req = urllib.request.Request(url, headers={"x-apisports-key": key})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            print(f"  ! 联赛 {lid} 拉取失败：{exc}", file=sys.stderr)
            continue
        label = LEAGUE_NAMES.get(lid, f"联赛{lid}")
        for item in data.get("response", []):
            name = (item.get("team") or {}).get("name")
            if name:
                out.setdefault(name.strip(), label)
    return out


# ---------------------------------------------------------------- 覆盖检测


def check_coverage(rep: Report, corpus: dict[str, str]) -> dict:
    """真实队名跑一遍，分出精确命中 / 归一键命中 / 未命中。"""
    exact, fuzzy, missed = [], [], []
    for name in sorted(corpus):
        if name in TEAM_NAMES:
            exact.append(name)
        elif team_short_name(name) != name:
            fuzzy.append(name)
        else:
            missed.append(name)
            tag = corpus.get(name) or ""
            rep.add(ERROR, "coverage",
                    f"未命中：{name}" + (f"（{tag}）" if tag else ""),
                    raw=name, league=tag)

    by_league: dict[str, list[str]] = {}
    for name in missed:
        by_league.setdefault(corpus.get(name) or "未标注", []).append(name)
    for league, names in sorted(by_league.items(), key=lambda kv: -len(kv[1])):
        rep.add(INFO, "coverage-summary",
                f"{league}：{len(names)} 条未命中 —— " + "、".join(names[:20])
                + ("…" if len(names) > 20 else ""),
                league=league, count=len(names))

    total = len(corpus)
    return {
        "total": total,
        "exact": len(exact),
        "fuzzy": len(fuzzy),
        "missed": len(missed),
        "rate": round((total - len(missed)) / total * 100, 1) if total else 0.0,
        "missed_names": missed,
        "fuzzy_names": fuzzy,
        "by_league": {k: len(v) for k, v in by_league.items()},
    }


# ---------------------------------------------------------------- 报告输出

LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


def render(rep: Report, cov: dict | None, verbose: bool) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"队名体检报告 · {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("=" * 60)
    lines.append(f"收录 {len(TEAM_NAMES)} 条 · 归一化索引 {len(_TEAM_INDEX)} 键")
    if cov:
        lines.append(
            f"语料 {cov['total']} 条：精确命中 {cov['exact']} · "
            f"归一键命中 {cov['fuzzy']} · 未命中 {cov['missed']} "
            f"（覆盖率 {cov['rate']}%）"
        )
    lines.append("")

    for level in (ERROR, WARN, INFO):
        rows = rep.of(level)
        if not rows:
            continue
        if level == INFO and not verbose:
            lines.append(f"[INFO] {len(rows)} 条提示（--verbose 展开）")
            lines.append("")
            continue
        lines.append(f"[{level}] {len(rows)} 条")
        by_check: dict[str, list[dict]] = {}
        for r in rows:
            by_check.setdefault(r["check"], []).append(r)
        for check, items in by_check.items():
            lines.append(f"  · {check}")
            for it in items[:40]:
                lines.append(f"      - {it['message']}")
            if len(items) > 40:
                lines.append(f"      - …另有 {len(items) - 40} 条")
        lines.append("")

    if not rep.rows:
        lines.append("全部通过：静态自检无 ERROR / WARN / INFO。")
    lines.append("-" * 60)
    lines.append(f"ERROR {len(rep.of(ERROR))} · WARN {len(rep.of(WARN))} · "
                 f"INFO {len(rep.of(INFO))}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="队名批量体检")
    ap.add_argument("--fetch", action="store_true",
                    help="调 API-Football 拉真实队名（需要 API key）")
    ap.add_argument("--from-db", metavar="PATH", help="从 SQLite 反查历史队名")
    ap.add_argument("--from-file", metavar="PATH", help="从文件读队名（每行一个）")
    ap.add_argument("--leagues", default="", help="只查这些联赛 ID，逗号分隔")
    ap.add_argument("--season", type=int, default=datetime.now().year,
                    help=f"赛季年份（默认 {datetime.now().year}）")
    ap.add_argument("--json", metavar="PATH", help="额外输出机器可读 JSON")
    ap.add_argument("--emit-missing", metavar="PATH",
                    help="把未命中队名单独写成文件，方便直接补录")
    ap.add_argument("--verbose", action="store_true", help="展开 INFO 级提示")
    args = ap.parse_args()

    rep = Report()
    check_self_hit(rep)
    check_collision(rep)
    check_dup_key(rep)
    check_dup_cn(rep)
    check_width(rep)
    check_hygiene(rep)
    check_unstable_key(rep)
    check_confusable(rep)

    corpus: dict[str, str] = {}
    cov = None
    if args.from_db:
        corpus.update(corpus_from_db(args.from_db))
    if args.from_file:
        corpus.update(corpus_from_file(args.from_file))
    if args.fetch:
        key = os.environ.get("API_FOOTBALL_KEY") or os.environ.get("RAPID_API_KEY")
        if not key:
            print("! --fetch 需要环境变量 API_FOOTBALL_KEY 或 RAPID_API_KEY",
                  file=sys.stderr)
        else:
            ids = ([int(x) for x in args.leagues.split(",") if x.strip()]
                   if args.leagues
                   else [i for i in LEAGUE_NAMES if i not in CUP_LEAGUES])
            corpus.update(corpus_from_api(ids, args.season, key))
    if corpus:
        cov = check_coverage(rep, corpus)

    print(render(rep, cov, args.verbose))

    if args.json:
        payload = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "count": len(TEAM_NAMES),
            "coverage": cov,
            "issues": sorted(rep.rows, key=lambda r: LEVEL_ORDER[r["level"]]),
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\nJSON 已写入 {args.json}")

    if args.emit_missing and cov:
        with open(args.emit_missing, "w", encoding="utf-8") as fh:
            for n in cov["missed_names"]:
                fh.write(n + "\n")
        print(f"未命中清单已写入 {args.emit_missing}")

    return 1 if rep.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
