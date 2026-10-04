"""原子格式化工具 / Atomic formatting helpers.

被 views/ 与 keyboards.py 共用的一层：转义、概率条、队名、积分榜行摘要、
因素推导等。放在这里而不是留在 bot_handler.py，是为了让视图层可以独立
引用它们而不必反向依赖入口模块（那会形成循环导入）。
"""
from __future__ import annotations

import html
import unicodedata
from datetime import datetime

import tghtml
from analyzer import OUTCOMES, calculate_prediction_level, overround
from service import MODEL_VERSION, parse_kickoff
from templates import (
    BRAND_EN,
    TEAM_NAMES,
    DISCLAIMER,
    LEAGUE_NAMES,
    MENU_ITEMS,
    NO_DATA,
    SEP,
    STATUS_TEXT,
    TABS,
    THIN_SEP,
    VALUE_FLAG,
)

def team_name(raw: str | None, bilingual: bool = True) -> str:
    """队名展示 / Display team name.

    双语模式返回「中文名 (English)」，未收录时只返回英文原名，绝不猜测或编造。
    Bilingual mode returns "中文 (English)"; unknown teams fall back to the
    original English name — never guessed or fabricated.
    """
    raw = (raw or "").strip()
    if not raw:
        return "?"
    cn = TEAM_NAMES.get(raw)
    if not cn:
        hit = _TEAM_INDEX.get(_team_key(raw))
        cn = TEAM_NAMES.get(hit) if hit else None
    if not cn or not bilingual:
        return raw
    return f"{cn} ({raw})"

# —— 队名归一化索引 ——
# API-Football 对同一支队的写法并不唯一（"Fenerbahçe SK" / "Fenerbahce"、
# "1. FC Köln" / "FC Koln"），精确匹配会漏。这里在导入期建一张归一化索引：
# 去掉重音、标点、常见俱乐部词缀后做匹配。归一化只用于**找已收录的队**，
# 找不到仍返回英文原名，绝不按发音或子串臆造中文名。
import re as _re
import unicodedata as _ud

# 俱乐部通用词缀：剥离后 "Liverpool FC" 与 "Liverpool" 归一到同一个键
_CLUB_TOKENS = frozenset(
    "fc afc cf sc sv bv vfb vfl ac cd ud rc ca ec aa sk jk if bk fk ff "
    "club de del della the 1909 1899 1846 1848 05 98 08".split()
)


def _team_key(name: str) -> str:
    """队名归一键：去重音、去标点、去俱乐部词缀、小写、去多余空格。"""
    s = _ud.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not _ud.combining(ch))
    s = s.lower().replace("&", " and ")
    s = _re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t and t not in _CLUB_TOKENS]
    return " ".join(tokens)


def _build_team_index() -> dict[str, str]:
    """TEAM_NAMES 的归一化索引；同一归一键冲突时保留最长原名（信息更全）。"""
    idx: dict[str, str] = {}
    for raw in TEAM_NAMES:
        k = _team_key(raw)
        if not k:
            continue
        prev = idx.get(k)
        if prev is None or len(raw) > len(prev):
            idx[k] = raw
    return idx


_TEAM_INDEX = _build_team_index()


def team_short_name(raw: str | None) -> str:
    """列表用短名：已收录的队显示中文名，未收录的显示英文原名。

    为什么不复用 team_name(..., bilingual=False)：那个参数的语义是「关闭双语、
    返回数据源原名」，实测它会把「Arsenal FC」原样输出成英文，而不是返回
    「阿森纳」。两者名字相近但行为相反，混用会静默出错，故另开一个函数名。

    与 team_name 同样遵守「未收录绝不猜测」：宁可显示长英文名，也不编造中文。
    """
    raw = (raw or "").strip()
    if not raw:
        return "?"
    cn = TEAM_NAMES.get(raw)
    if cn:
        return cn
    # 精确未命中时走归一化索引：同一个队在不同数据源里词缀/重音写法不同
    hit = _TEAM_INDEX.get(_team_key(raw))
    return TEAM_NAMES.get(hit) if hit else raw


# 手机单行可读上限（显示宽度）。Telegram 消息气泡在常见 360dp 屏上约能放
# 18 个中文字符 = 36 列；超过就会折行，对「一眼扫完」的卡片是致命的。
MOBILE_MAX_COLS = 36


def fit_cols(text: str, max_cols: int = MOBILE_MAX_COLS) -> str:
    """按显示宽度截断，超宽以 … 收尾；不补空格（补位请用 align_cjk）。

    与 align_cjk 的分工：那个用于 <code> 等宽块内的**表格对齐**（必须补到
    固定列宽），这个用于正文/标题等**非表格**位置（补空格无意义，只会让
    行尾出现看不见的空白）。

    只截断不猜测：宁可显示「Borussia Mönchen…」也不编造一个中文名。
    """
    text = str(text)
    if display_width(text) <= max_cols:
        return text
    out = ""
    for ch in text:
        if display_width(out) + display_width(ch) > max_cols - 1:
            break
        out += ch
    return (out + "…").strip()


def team_mobile(raw: str | None, max_cols: int = 16) -> str:
    """移动端队名：优先中文短名，过长（多为未收录的英文原名）按列宽截断。

    为什么不是直接用 team_short_name：实测「Wolverhampton Wanderers FC」
    这类未收录原名宽 26 列，两个队名横排就是 52 列，在手机上必然折成三行。
    已收录的队（五大联赛主流球队）几乎都落在 16 列内，因此截断只作用于
    极少数未收录队，不影响正常观感。
    """
    return fit_cols(team_short_name(raw), max_cols)


def league_label(league_id: int) -> str:
    """展示用联赛名：已知 ID 显示中文名 + ID，未知则只显示 ID。"""
    name = LEAGUE_NAMES.get(int(league_id))
    return f"{name} · {league_id}" if name else f"联赛 {league_id}"


def leagues_label(league_ids) -> str:
    """多个联赛的展示名，去重保序后用「/」连接。

    单联赛时与 league_label 输出一致，老配置（无 league_ids）行为不变。
    """
    raw = list(league_ids or ())
    seen, ids = set(), []
    for i in raw:
        try:
            value = int(i)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            seen.add(value)
            ids.append(value)
    if not ids:
        return "未配置"
    return " / ".join(league_label(i) for i in ids)

def fmt_time(dt: datetime, tz, pattern: str = "%m-%d %H:%M") -> str:
    """时区格式化。

    下沉到本模块的理由：`build_prediction_payload` 需要它，而本模块位于
    依赖链底层（views/ 依赖它），不能反向导入 bot_handler 去拿 BotUI。
    CommonView.fmt_time 保留同名方法转发至此，行为完全一致。
    """
    return dt.astimezone(tz).strftime(pattern)

def esc(value) -> str:
    return html.escape(str(value), quote=False)

def bar(prob: float, width: int = 10) -> str:
    """概率条：▰▰▰▰▱▱▱▱▱▱"""
    filled = max(0, min(width, round(prob * width)))
    return "▰" * filled + "▱" * (width - filled)


def hbar(prob: float, width: int = 10) -> str:
    """实心概率条：█████░░░░░

    与 bar() 的区别只是字形（方块 vs 圆角方块），用于预测主卡等需要更重视觉
    分量的位置。放在 formatkit 而不是某个 View 上，是因为 prediction / analysis
    都要用——挂在类上会让跨模块调用拿到不存在的方法（历史上就出过这个 bug）。
    """
    filled = max(0, min(width, round(prob * width)))
    return "█" * filled + "░" * (width - filled)


def display_width(text: str) -> int:
    """显示宽度：CJK/全角按 2 列，其余按 1 列。

    Telegram 的 <code>/<pre> 是等宽渲染，中文占两列。按字符数补空格会让
    中英混排的表格错开，所以对齐必须按显示宽度算。
    """
    width = 0
    i = 0
    while i < len(text):
        ch = text[i]
        # 英格兰/苏格兰/威尔士旗是「黑旗 + 若干 tag 字符」的序列，渲染成
        # 一个字形（2 列）。逐字符累加会把一个旗算成 8 列，按钮宽度判断
        # 和任何含旗的等宽表格都会跟着算错。
        if ch == "\U0001f3f4" and i + 1 < len(text) and 0xE0020 <= ord(text[i + 1]) <= 0xE007F:
            j = i + 1
            while j < len(text) and 0xE0020 <= ord(text[j]) <= 0xE007F:
                j += 1
            width += 2
            i = j
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        i += 1
    return width


def align_cjk(text: str, width: int, align: str = "left") -> str:
    """按显示宽度对齐补空格（left/right），超出则截断并以 … 收尾。

    只用于 <code>/<pre> 内部——Telegram 只有等宽块里的空格才真正对齐，
    正文是比例字体，补多少空格都对不齐。
    """
    text = str(text)
    # 严格大于才截断：宽度正好等于目标时原样返回。
    # 用 >= 会把「数据完整性」(5 字 = 10 列) 在 width=10 下截成「数据完整…」，
    # 标签被吃掉一字 —— 这个 bug 真实发生过，故此处边界必须是 >。
    if display_width(text) > width:
        # 逐字累加，留最后一列给省略号
        out = ""
        for ch in text:
            if display_width(out) + display_width(ch) > width - 1:
                break
            out += ch
        # 省略号宽度只有 1，若刚好截在双宽字符后，总宽会差 1 列；
        # 补空格补齐，保证返回值严格等于目标列宽（否则表格仍会错开）
        out = (out + "…") if align == "left" else ("…" + out)
        gap = " " * (width - display_width(out))
        return out + gap if align == "left" else gap + out
    gap = " " * (width - display_width(text))
    return text + gap if align == "left" else gap + text


def pad_cjk(text: str, width: int) -> str:
    """align_cjk 的左对齐别名，语义更直白，读代码时一眼看出在补列宽。"""
    return align_cjk(text, width, "left")


def kv_line(icon: str, label: str, value: str, width: int = 12) -> str:
    """一行「标签 + 值」，两者放进**同一个** <code> 等宽块。

    为什么必须同块：Telegram 正文是比例字体，在 code 外补多少空格都对不齐
    ——4 字标签与 5 字标签各补到 10 列，渲染出来仍会差半格。只有整行进
    <code> 时，pad_cjk 按显示宽度补的空格才是「真正的列」。

    第二个理由：补位空格落在标签与值**中间**，而不是行尾。若写成
    `<code>标签  </code>值`，尾部空格有被渲染器 trim 的风险，对齐就白做了。

    宽度默认 12：最长标签「最可能比分」占 10 列，留 2 列间隔，
    否则 5 字标签会与值贴死。

    值里不能含 <b>/<code> 等标签（Telegram 不支持嵌套），需要富文本的值
    请改用全角空格分栏（见 prediction.py 的结论行）。
    """
    return f"{icon} <code>{pad_cjk(label, width)}{value}</code>"


BLANK = ""  # 分节之间的空行：移动端可读性的关键，没有它各块会挤成一坨


def section(icon: str, title: str, *body: str) -> list[str]:
    """统一分节：图标+标题 → 分隔线 → 内容 → 空行。

    所有视图（预测主卡 / 常规预测 / 赛程 / 积分榜 / 深度分析）共用这一套排版，
    改一处即全局生效，避免各视图各自拼字符串导致风格漂移。

    返回的最后带一个空行，调用方直接 extend 即可，无需再手动补 ""。
    """
    out = [f"{icon} <b>{title}</b>", SEP, *body, BLANK]
    return out


def section_join(blocks: list[list[str]]) -> str:
    """把若干 section() 拼成最终文本，块之间不额外加空行（section 自带）。"""
    lines: list[str] = []
    for block in blocks:
        lines.extend(block)
    # 收尾去掉最后一个空行，避免消息底部多一行空白
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# 比赛状态 → 图标。未收录的状态返回 ⚪，而不是抛异常或留空。
STATUS_EMOJI = {
    "NS": "🕐", "TBD": "🕐",
    "1H": "🔴", "2H": "🔴", "ET": "🔴", "BT": "⏸",
    "HT": "⏸", "P": "🎯", "PEN": "🎯", "LIVE": "🔴",
    "FT": "✅", "AET": "✅",
    "PST": "⏸", "CANC": "❌", "ABD": "❌",
    "SUSP": "⚠️", "INT": "⚠️", "WO": "❌",
}


def status_emoji(short: str) -> str:
    """比赛状态短码 → 图标；未收录返回 ⚪。"""
    return STATUS_EMOJI.get(str(short or "").upper(), "⚪")

def split_html_blocks(text: str, limit: int = 3500) -> list[str]:
    """按行拆分超长 HTML 文本，避免超过 Telegram 单条 4096 字符限制。

    实现下沉到 tghtml.split_message：旧版「按行切了就完事」，一旦 <pre>
    表格被切在中间，前半块结尾的 <pre> 没闭合会让 Telegram 整条 400 拒绝，
    后半块开头也退化成纯文本。新版在拆口处闭合/重开标签，见 tghtml 模块。
    """
    return tghtml.split_message(text, limit)

def _is_fallback(p) -> bool:
    """预测结果是否来自备用数据源 football-data.org。"""
    return "football-data" in str(getattr(p, "source", "")).lower()

def _is_fallback_source(report: dict) -> bool:
    """深度分析报告是否基于备用数据源。"""
    return "football-data" in str(report.get("source", "")).lower()

def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

def _row_summary(row: dict | None) -> dict | None:
    """积分榜一行 → 排名 / 积分 / 胜平负 / 场均进失球。缺字段时相应项为 None。"""
    if not row:
        return None
    all_, home, away = row.get("all") or {}, row.get("home") or {}, row.get("away") or {}
    played = _num(all_.get("played")) or (_num(home.get("played")) + _num(away.get("played")))
    gf = _num((all_.get("goals") or {}).get("for")) or (
        _num((home.get("goals") or {}).get("for")) + _num((away.get("goals") or {}).get("for"))
    )
    ga = _num((all_.get("goals") or {}).get("against")) or (
        _num((home.get("goals") or {}).get("against")) + _num((away.get("goals") or {}).get("against"))
    )
    win = _num(all_.get("win")) or (_num(home.get("win")) + _num(away.get("win")))
    draw = _num(all_.get("draw")) or (_num(home.get("draw")) + _num(away.get("draw")))
    lose = _num(all_.get("lose")) or (_num(home.get("lose")) + _num(away.get("lose")))
    return {
        "rank": row.get("rank"),
        "points": row.get("points"),
        "played": int(played),
        "win": int(win),
        "draw": int(draw),
        "lose": int(lose),
        "avg_for": gf / played if played else None,
        "avg_against": ga / played if played else None,
    }

def _form_mark(result: str) -> str:
    return {"win": "🟢", "draw": "🟡", "lose": "🔴"}.get(result, "⚪")

def _form_line(form: dict) -> str:
    """近期战绩一行摘要（没有足够样本时明确说没有数据）。"""
    if not form["played"]:
        return NO_DATA
    avg_for = f"{form['avg_for']:.1f}" if form["avg_for"] is not None else "-"
    avg_against = f"{form['avg_against']:.1f}" if form["avg_against"] is not None else "-"
    marks = " ".join(_form_mark(m["result"]) + m["score"] for m in form["matches"][:5])
    return f"{form['win']}胜 {form['draw']}平 {form['lose']}负 · 进 {form['goals_for']} / 失 {form['goals_against']} · 场均 {avg_for} / {avg_against}\n   {marks}"

def _row_line(row: dict | None) -> str:
    if not row:
        return NO_DATA
    summary = _row_summary(row)
    if not summary or not summary["played"]:
        return NO_DATA
    rank = f"第 {summary['rank']} 名" if summary["rank"] else "排名未知"
    points = f"{summary['points']} 分" if summary["points"] is not None else "积分未知"
    avg_for = f"{summary['avg_for']:.2f}" if summary["avg_for"] is not None else "-"
    avg_against = f"{summary['avg_against']:.2f}" if summary["avg_against"] is not None else "-"
    return (
        f"{rank} · {points} · {summary['win']}胜{summary['draw']}平{summary['lose']}负"
        f" · 场均进 {avg_for} / 失 {avg_against}"
    )

def _factors(report: dict) -> tuple[list[str], list[str], list[str]]:
    """从数据推导有利 / 不利 / 不确定因素。没有数据就不编，直接归入不确定。"""
    pros: list[str] = []
    cons: list[str] = []
    unknowns: list[str] = []
    hs = report["model"]["home_strength"]
    aws = report["model"]["away_strength"]
    hf, af, h2h = report["home_form"], report["away_form"], report["h2h"]

    if not report["has_team_data"]:
        unknowns.append("积分榜中缺少这两支球队的数据，强弱对比不成立")
        unknowns.append("数据源未提供伤停信息")
        return pros, cons, unknowns

    if hs.attack_home > 1.1:
        pros.append(f"主队主场进攻强度 {hs.attack_home:.2f}，高于联赛平均")
    elif hs.attack_home < 0.9:
        cons.append(f"主队主场进攻强度 {hs.attack_home:.2f}，低于联赛平均")
    if aws.defense_away > 1.1:
        pros.append(f"客队客场失球偏多（防守强度 {aws.defense_away:.2f}）")
    elif aws.defense_away < 0.9:
        cons.append(f"客队客场防守稳固（防守强度 {aws.defense_away:.2f}）")

    if hf["played"] >= 3:
        if hf["win"] > hf["lose"]:
            pros.append(f"主队近期 {hf['win']}胜{hf['draw']}平{hf['lose']}负，状态较好")
        elif hf["lose"] > hf["win"]:
            cons.append(f"主队近期 {hf['win']}胜{hf['draw']}平{hf['lose']}负，状态偏低")
    if af["played"] >= 3 and af["win"] > af["lose"]:
        cons.append(f"客队近期 {af['win']}胜{af['draw']}平{af['lose']}负，来势不弱")

    if h2h["played"] >= 3:
        if h2h["win"] > h2h["lose"]:
            pros.append(f"历史交锋占优：{h2h['win']}胜{h2h['draw']}平{h2h['lose']}负")
        elif h2h["lose"] > h2h["win"]:
            cons.append(f"历史交锋处于劣势：{h2h['win']}胜{h2h['draw']}平{h2h['lose']}负")

    if hf["played"] < 3 or af["played"] < 3:
        unknowns.append("近期已完场比赛不足 3 场，状态判断不稳定")
    if h2h["played"] == 0:
        unknowns.append("暂无历史交锋记录")
    if min(hs.games_home, aws.games_away) < 5:
        unknowns.append("主/客场已赛场次不足 5 场，强度估计不稳定")
    unknowns.append("数据源未提供伤停信息（当前套餐不支持）")
    return pros, cons, unknowns

def build_prediction_payload(p, tz) -> dict:
    """统一预测结果数据结构（供格式化与未来的网页端复用）。"""
    a = p.analysis
    probabilities = {
        "home_team": team_name(p.home),
        "away_team": team_name(p.away),
        "home_win": float(a["win_prob"]),
        "draw": float(a["draw_prob"]),
        "away_win": float(a["loss_prob"]),
    }
    level = calculate_prediction_level(probabilities)
    probabilities.update(
        {
            "result": level["result"],
            "level_key": level["key"],
            "level_name": level["name"],
            "level_emoji": level["emoji"],
            "source": p.source,
            "season": p.season,
            "kickoff": fmt_time(p.kickoff, tz),
        }
    )
    return probabilities

_WEB_ENTRY_TEXT = (
    "🌐 <b>网页端</b>\n\n"
    "网页查询界面正在规划中，当前阶段以 Telegram 机器人的数据稳定性为主。\n\n"
    "开放后将支持：\n"
    "· 在浏览器里查看今日赛程与预测\n"
    "· 查询历史预测与命中情况\n"
    "· 多联赛切换\n\n"
    "目前请继续使用下方菜单功能。"
)

def web_entry_text(web_url: str = "") -> str:
    """网页端入口文案。

    配置了 WEB_URL 时给出可点击的真实地址；未配置时明确说明未部署，
    **绝不给一条打不开的链接**——点开是 404 比直说没有更糟。

    因此未部署文案里**不出现任何 http(s) 字面量**：Telegram 会自动把
    `https://...` 变成可点击链接，写示例地址等于给用户一条死链。
    """
    if not web_url:
        return section_join([
            ["🌐 <b>网页端</b>", BLANK],
            section("🚧", "当前状态", "看板代码已就绪，但当前实例<b>未部署 Web 服务</b>。"),
            section(
                "🛠", "启用方法",
                "• 安装可选依赖 <code>pip install -r requirements-web.txt</code>",
                "• 启动 <code>uvicorn api:app --host 0.0.0.0 --port 8000</code>",
                "• 设置环境变量 <code>WEB_URL</code>，值为该服务的完整访问地址",
            ),
            section("✅", "未启用期间", "下方菜单功能不受影响。"),
        ])
    return section_join([
        ["🌐 <b>网页统计看板</b>", BLANK],
        section("🔗", "访问地址", esc(web_url)),
        section(
            "📊", "可查看",
            "• 模型健康度（Log Loss 趋势、校准曲线）",
            "• 球队攻防强度榜",
            "• 历史预测审计（预测 vs 实际赛果）",
        ),
        section("💡", "说明", "数据取自本地库，刷新页面不消耗 API 额度。"),
    ])
