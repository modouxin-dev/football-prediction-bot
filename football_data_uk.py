"""第三数据源：football-data.co.uk（静态 CSV，免费、无 Key）。

定位：补足两个免费层拿不到的东西
--------------------------------
1. **历史赛季**：football-data.org / API-Foobtall 的免费档只覆盖当前赛季，
   回测样本量上不去（本地已完赛仅 10 场，回测门槛 50 场）。
   本站提供 1993/94 至今的逐场 CSV，一次导入即可拿到上千场已完赛。
2. **赔率**：项目现有 get_odds 在免费层恒为空。本站 CSV 自带多家博彩公司的
   1X2 赔率（Bet365 / Pinnacle / 市场均值等），可直接作为**市场基准线**。

为什么它比"再找一个 API"更合适
------------------------------
- 静态 CSV，无 Key、无限流、零 API 额度消耗。
- 数据更新频率：赛季中每周两次（周日夜、周三夜）。
- 未来赛程的赔率也会提前采集（周末赛周五下午、周中赛周二下午）。

关键约束（务必知悉）
--------------------
- **队名口径不同**：本站写 "Man City"，本项目库里是 "Manchester City FC"。
  matches 表的唯一键是 (competition_code, utc_date, home_team_id, away_team_id)，
  若 team_id 对不上，同一场比赛会被存成两行 → 重复计数。
  因此必须先做队名解析（见 resolve_team_ids）。
- **时间是英国本地时间**，不是 UTC。本站只给 "dd/mm/yy" + "HH:MM"，
  没有时区标记。英超没有跨日比赛（最晚约 20:00 BST → 19:00 UTC），
  所以日期部分与 UTC 一致，仅时刻可能有 1 小时偏差。
  这里按英国本地时间构造 ISO 串，仅用于排序与展示，不用于精确换算。
- CSV 编码不统一（部分老赛季是 Windows-1252），需逐编码回退。

参考：https://www.football-data.co.uk/notes.txt（列名权威定义）
"""
from __future__ import annotations

import csv
import io
import logging
import zlib
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BASE_URL = "https://www.football-data.co.uk/mmz4281"

# 联赛 division 代码 → 本项目 competition_code
DIV_TO_COMPETITION: dict[str, str] = {
    "E0": "PL",   # 英超
    "E1": "ELC",  # 英冠
    "D1": "BL1",  # 德甲
    "SP1": "PD",  # 西甲
    "I1": "SA",   # 意甲
    "F1": "FL1",  # 法甲
    "N1": "DED",  # 荷甲
    "P1": "PPL",  # 葡超
}

# CSV 编码回退顺序：部分老赛季文件不是 UTF-8
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def season_code(start_year: int) -> str:
    """赛季起始年 → 本站的 4 位赛季码。

    2026 → "2627"（2026/27 赛季）。注意取模，2099→2100 也不会错。
    """
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def build_url(start_year: int, div: str = "E0") -> str:
    """构造 CSV 下载地址。

    例：build_url(2026) → https://www.football-data.co.uk/mmz4281/2627/E0.csv
    """
    return f"{BASE_URL}/{season_code(start_year)}/{div}.csv"


# --------------------------------------------------------------------------
# 队名映射：football-data.co.uk → football-data.org 口径
# --------------------------------------------------------------------------
# 本项目库里的队名来自 football-data.org（带 FC / AFC 等后缀）。
# 这里只列英超近年常见队；历史赛季的队（如诺维奇、沃特福德）不在表内，
# 会走 slug 兜底，不影响导入，只是无法与既有行合并。
TEAM_ALIASES: dict[str, str] = {
    "Man City": "Manchester City FC",
    "Man United": "Manchester United FC",
    "Arsenal": "Arsenal FC",
    "Chelsea": "Chelsea FC",
    "Liverpool": "Liverpool FC",
    "Tottenham": "Tottenham Hotspur FC",
    "Newcastle": "Newcastle United FC",
    "Brighton": "Brighton & Hove Albion FC",
    "West Ham": "West Ham United FC",
    "Crystal Palace": "Crystal Palace FC",
    "Fulham": "Fulham FC",
    "Brentford": "Brentford FC",
    "Everton": "Everton FC",
    "Nott'm Forest": "Nottingham Forest FC",
    "Leeds": "Leeds United FC",
    "Burnley": "Burnley FC",
    "Sunderland": "Sunderland AFC",
    "Wolves": "Wolverhampton Wanderers FC",
    "Bournemouth": "AFC Bournemouth",
    "Aston Villa": "Aston Villa FC",
    # ---- 近几个赛季出现过、可能仍在历史 CSV 里 ----
    "Leicester": "Leicester City FC",
    "Southampton": "Southampton FC",
    "Ipswich": "Ipswich Town FC",
    "Sheffield United": "Sheffield United FC",
    "West Brom": "West Bromwich Albion FC",
    "Watford": "Watford FC",
    "Norwich": "Norwich City FC",
    "Hull": "Hull City AFC",
    "Stoke": "Stoke City FC",
    "Swansea": "Swansea City AFC",
    "Middlesbrough": "Middlesbrough FC",
    "Cardiff": "Cardiff City FC",
    "Huddersfield": "Huddersfield Town AFC",
    "Luton": "Luton Town FC",
    "QPR": "Queens Park Rangers FC",
    "Reading": "Reading FC",
    "Blackburn": "Blackburn Rovers FC",
    "Birmingham": "Birmingham City FC",
    "Millwall": "Millwall FC",
    "Coventry": "Coventry City FC",
}


def canonical_team_name(raw: str) -> str:
    """单个队名 → 本项目口径的规范名；查不到就原样返回（不做猜测）。"""
    key = (raw or "").strip()
    return TEAM_ALIASES.get(key, key)


def _slug(name: str) -> str:
    """队名 → 稳定的 ASCII slug，用于兜底生成 team_id。"""
    out = []
    for ch in (name or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -&'":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "unknown"


def _pair_fingerprint(home: str, away: str) -> int:
    """两队组合 → 6 位稳定指纹。

    用 zlib.crc32 而不是内置 hash()：后者受 PYTHONHASHSEED 影响，
    每次进程启动都会变，会让同一场比赛在每次导入时生成新行。
    """
    return zlib.crc32(f"{home}|{away}".encode("utf-8")) % 1_000_000


def _fixture_id(day: str, home: str, away: str) -> str:
    """构造比赛 id（纯数字字符串）。

    必须适配 repository._stable_id 的行为：它只保留字符串里的**数字字符**，
    再取**末 9 位**。因此：

    - 不能用 "fduk-2026-arsenal-fulham-2026-09-12" 这种写法——
      数字只剩 "2026"+"2026"+"09"+"12"，同一天所有比赛的 id 完全相同，
      会互相覆盖（一天 6 场最后只剩 1 场，比分还是最后写的那场）。
    - 正确做法是把**区分度放在末尾**：日期(8 位) + 队对指纹(6 位)，
      末 9 位 = 月日 + 6 位指纹，同日不同对阵必然不同。
    """
    ymd = (day or "").replace("-", "")
    return f"{ymd}{_pair_fingerprint(home, away):06d}"


def resolve_team_ids(home_raw: str, away_raw: str, known: dict[str, str] | None = None
                     ) -> tuple[str, str, str, str]:
    """把 CSV 的裸队名解析成 (home_id, home_name, away_id, away_name)。

    known：从本地库反查出来的「规范名 → 已存在的 team_id」映射。
    优先用它，这样导入的历史比赛能与既有行命中同一条唯一键，
    不会因为 id 不同而插成两条。查不到才退回到 slug 兜底。
    """
    home_name = canonical_team_name(home_raw)
    away_name = canonical_team_name(away_raw)
    known = known or {}
    home_id = known.get(home_name) or f"fduk-{_slug(home_name)}"
    away_id = known.get(away_name) or f"fduk-{_slug(away_name)}"
    return home_id, home_name, away_id, away_name


# --------------------------------------------------------------------------
# 赔率
# --------------------------------------------------------------------------
# 取值优先级：Pinnacle（公认最"锋利"的盘口）> Bet365 > 市场均值。
# 同一家优先用收盘价（列名多一个 C），因为收盘价信息量最大。
_BOOK_SEQUENCE = (
    ("PSCH", "PSCD", "PSCA"),  # Pinnacle 收盘
    ("PSH", "PSD", "PSA"),     # Pinnacle 开盘
    ("B365CH", "B365CD", "B365CA"),
    ("B365H", "B365D", "B365A"),
    ("AvgCH", "AvgCD", "AvgCA"),
    ("AvgH", "AvgD", "AvgA"),
    ("MaxH", "MaxD", "MaxA"),
)


def _to_float(v: Any) -> float | None:
    """赔率专用：必须为正数。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _to_score(v: Any) -> float | None:
    """比分专用：**0 是合法值**。

    不能复用 _to_float（那个要求 > 0）。0-0、1-0、0-3 都是真实赛果，
    若把 0 当成缺失，这些比赛会被误标成 SCHEDULED，
    而回测只吃已完赛场次——等于整批导入白做。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def extract_odds(row: dict[str, Any]) -> dict[str, Any]:
    """从一行 CSV 里挑出第一组完整的 1X2 赔率。

    返回 {"home": h, "draw": d, "away": a, "bookmaker": "Pinnacle/...",
          "closing": bool}；没有可用赔率时返回 {}。
    """
    for keys in _BOOK_SEQUENCE:
        h, d, a = (_to_float(row.get(k)) for k in keys)
        if h and d and a:
            return {
                "home": h, "draw": d, "away": a,
                "bookmaker": keys[0].rstrip("HDA"),
                "closing": keys[0].endswith("CH") or "C" in keys[0][1:],
            }
    return {}


def market_probs(odds: dict[str, Any]) -> dict[str, float] | None:
    """赔率 → 去水位后的市场概率。

    博彩公司的赔率包含水位（overround，三项倒数之和 > 1），
    直接取倒数会得到概率和 > 1 的结果，必须除以总和归一化，
    否则和市场比 Log Loss 时市场会被系统性低估。
    """
    if not odds:
        return None
    h, d, a = (_to_float(odds.get(k)) for k in ("home", "draw", "away"))
    if not (h and d and a):
        return None
    ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    total = ih + id_ + ia
    if total <= 0:
        return None
    return {"主胜": ih / total, "平局": id_ / total, "客胜": ia / total}


# --------------------------------------------------------------------------
# CSV 解析
# --------------------------------------------------------------------------
def _parse_date(raw: str) -> str | None:
    """dd/mm/yy 或 dd/mm/yyyy → YYYY-MM-DD。"""
    s = (raw or "").strip()
    if not s:
        return None
    parts = s.split("/")
    if len(parts) != 3:
        return None
    d, m, y = parts
    if len(y) == 2:
        # 两位年份：70-99 → 19xx，00-69 → 20xx
        y = ("19" if int(y) >= 70 else "20") + y
    if not (y.isdigit() and m.isdigit() and d.isdigit()):
        return None
    return f"{y}-{int(m):02d}-{int(d):02d}"


def to_fixture(row: dict[str, Any], *, season: int, competition: str = "PL",
               known: dict[str, str] | None = None) -> dict | None:
    """CSV 一行 → football-data.org 的扁平 fixture 结构。

    刻意复用备用源的扁平结构，这样 repository._extract_match 无需改动，
    save_matches / load_matches / 回测都能直接吃。
    缺队名或日期的行返回 None（CSV 里确实存在空行）。
    """
    home_raw = (row.get("HomeTeam") or "").strip()
    away_raw = (row.get("AwayTeam") or "").strip()
    day = _parse_date(row.get("Date") or "")
    if not home_raw or not away_raw or not day:
        return None

    home_id, home_name, away_id, away_name = resolve_team_ids(home_raw, away_raw, known)

    # 时间：本站给的是英国本地时间，无时区标记
    time_raw = (row.get("Time") or "").strip()
    clock = time_raw if len(time_raw) >= 4 else "00:00"
    utc_date = f"{day}T{clock}:00Z"

    hg, ag = _to_score(row.get("FTHG")), _to_score(row.get("FTAG"))
    finished = hg is not None and ag is not None

    fx: dict[str, Any] = {
        # 稳定 id：同一场比赛重复导入必须落到同一行
        "id": _fixture_id(day, home_name, away_name),
        "utcDate": utc_date,
        "status": "FINISHED" if finished else "SCHEDULED",
        "matchday": int(row["Matchday"]) if str(row.get("Matchday") or "").isdigit() else None,
        "season": {"startDate": f"{season}-08-01"},
        "homeTeam": {"id": home_id, "name": home_name},
        "awayTeam": {"id": away_id, "name": away_name},
        "score": {"fullTime": {
            "home": int(hg) if finished else None,
            "away": int(ag) if finished else None,
        }},
        "competition": competition,
        "source": "football-data.co.uk",
    }
    odds = extract_odds(row)
    if odds:
        fx["odds"] = odds
        fx["marketProbabilities"] = market_probs(odds)
    return fx


def parse_csv(text: str, *, season: int, competition: str = "PL",
              known: dict[str, str] | None = None) -> list[dict]:
    """解析整份 CSV，返回 fixture 列表。

    text 为已解码的文本；编码回退由 decode_bytes 负责。
    """
    if not text or not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        fx = to_fixture(row, season=season, competition=competition, known=known)
        if fx is not None:
            out.append(fx)
    return out


def decode_bytes(raw: bytes) -> str:
    """按编码回退顺序解码 CSV 字节流。

    部分老赛季文件是 Windows-1252（含 £ 等符号），直接 utf-8 会抛异常。
    """
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


async def fetch_season(start_year: int, div: str = "E0", *, timeout: float = 30.0) -> str:
    """下载某个赛季的 CSV 文本。失败时抛出，由调用方决定如何降级。"""
    import httpx

    url = build_url(start_year, div)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return decode_bytes(resp.content)


# --------------------------------------------------------------------------
# 本地内置历史数据（打包进镜像，无需联网）
# --------------------------------------------------------------------------
# 线上容器不一定能出网，但镜像里 data/history/ 一定有。把三个赛季的
# 精简 CSV 随代码一起提交，运行时直接读文件——这是唯一不依赖出口网络的
# 历史数据获取方式，也是回测样本能上千场的前提。
def history_dir() -> Path:
    """内置历史 CSV 目录：仓库根/data/history。"""
    return Path(__file__).resolve().parent / "data" / "history"


def available_local_seasons(div: str = "E0") -> list[int]:
    """扫描内置目录，返回可用的起始年份列表（升序）。"""
    d = history_dir()
    if not d.is_dir():
        return []
    out = []
    for p in d.glob(f"{div}_*.csv"):
        stem = p.stem[len(div) + 1:]
        if stem.isdigit():
            out.append(int(stem))
    return sorted(out)


def load_local(start_year: int, div: str = "E0", *, competition: str = "PL",
               known: dict[str, str] | None = None) -> list[dict]:
    """读取内置历史赛季 CSV，返回 fixture 列表。

    文件不存在返回空列表（不抛异常），调用方按「无数据」降级即可。
    """
    p = history_dir() / f"{div}_{start_year}.csv"
    if not p.is_file():
        log.debug("内置历史数据缺失: %s", p)
        return []
    try:
        text = decode_bytes(p.read_bytes())
    except OSError as exc:
        log.warning("读取内置历史数据失败 %s: %s", p, exc)
        return []
    return parse_csv(text, season=start_year, competition=competition, known=known)


def load_local_all(div: str = "E0", *, competition: str = "PL",
                   known: dict[str, str] | None = None) -> list[dict]:
    """读取全部内置赛季，按日期升序合并。"""
    out: list[dict] = []
    for season in available_local_seasons(div):
        out.extend(load_local(season, div, competition=competition, known=known))
    out.sort(key=lambda f: (f.get("utc_date") or ""))
    return out
