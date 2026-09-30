"""HTML → Telegram HTML 规范化 / HTML-to-Telegram normalizer.

设计参考 TGHTML（Deno/JSR 的 @adriangalilea/tghtml）：它把任意 HTML
（尤其是 LLM 生成的富文本）转成 Telegram 能渲染的子集，核心主张是
「保留内容、维持结构、输出合规、容错畸形输入、保持视觉呈现」。

本模块是它在 Python 侧的对应物，但**只做 TGHTML 规则的安全子集**，
这是刻意的取舍：

    TGHTML 面向「LLM 吐出的脏 HTML」，因此需要激进折叠空白——
    因为源文本里的换行与空格都是无意义的排版噪声。

    本模块面向「我们自己生成的结构化文本」，其中的空白**是有意义的**：
    · <code>/<pre> 等宽块内用空格做列对齐（CJK 按显示宽度补位）
    · 正文里用全角空格（U+3000）做标签与数值的分栏
    · 分节靠空行（BLANK）撑开可读性
    若照搬「折叠多余空白」，对齐会被压扁、分栏会错位——这些恰恰是
    上一轮排版改进刚修好的东西。

因此规则定为：
    1. 白名单外的标签：剥掉标签、保留内容（TGHTML 的 Preserve Content）
    2. 块级标签：转成换行与语义间距（段落间空行，列表项带 • ）
    3. 标题 h1-h6：转粗体（Telegram 无标题概念）
    4. 连续换行最多 2 个（TGHTML 的 Maximum Newlines）
    5. 空白只在**非等宽块**内做保守清理：去行尾空白，不动行内空格
    6. 未闭合标签自动补齐（Handle Malformed Input）
    7. 文本节点一律重新转义，保证输出永不含裸 '<'（Be Telegram-Compliant）
    8. 超长消息按行拆分，拆口处关闭/重开标签，不产生半截标签

第 7 条让本函数是**幂等**的：已转义的 `&gt;` 解析后是 `>`，重新转义回
`&gt;`，多次调用不变；而漏转义的裸 `<` 会被修正，不会污染 Telegram 解析。
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Telegram Bot API 支持的 HTML 标签（唯一能通过 parse_mode=HTML 渲染的集合）
ALLOWED_TAGS = frozenset(
    {"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "blockquote"}
)

# 同义标签 → 白名单标签。Telegram 不认 <strong>/<em>/<del> 等，必须归一。
TAG_ALIASES = {
    "strong": "b",
    "em": "i",
    "ins": "u",
    "strike": "s",
    "del": "s",
    "tg_spoiler": "tg-spoiler",
}

# 块级标签：剥掉标签本身，用换行还原它的排版意图。
# 注意 li 特殊处理（加 • 前缀），br/hr 特殊处理（换行 / 分隔线）。
BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "main", "aside",
        "nav", "figure", "figcaption", "dl", "dt", "dd", "ul", "ol",
        "table", "thead", "tbody", "tr", "td", "th", "fieldset", "form",
    }
)

HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# 自闭合/空元素：没有结束标签，不能压栈
VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link"})

# 等宽块：内部空白与内容必须原样保留（对齐靠它）
MONOSPACE_TAGS = frozenset({"code", "pre"})

# 连续 3 个以上换行压成 2 个
_MULTI_NEWLINE = re.compile(r"\n{3,}")

# 行尾空白（不含换行本身）
_TRAILING_WS = re.compile(r"[ \t　]+\n")


class _TelegramHTMLParser(HTMLParser):
    """流式解析并把结果写进 out 列表。

    用 HTMLParser 而不是正则：HTML 允许嵌套与属性变体（单引号、无引号、
    大小写混用），正则处理这些既脆弱又会漏；标准库解析器已把大小写归一、
    属性值解码这些脏活做完，我们只关心标签语义。
    """

    def __init__(self) -> None:
        # convert_charrefs=True：&gt; &amp; 等实体直接解码成字符交给
        # handle_data，我们再统一重新转义，从而保证幂等与合规。
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []  # 当前打开的标签栈（已归一为白名单名）

    # -- 工具 ---------------------------------------------------------------
    def _in_monospace(self) -> bool:
        """是否处于 <code>/<pre> 内：内部空白原样保留。"""
        return any(tag in MONOSPACE_TAGS for tag in self.stack)

    def _emit(self, text: str) -> None:
        self.out.append(text)

    def _ensure_line_start(self) -> None:
        """块级元素开始前，若当前行非空则先换行（避免两块黏在一行）。"""
        joined = "".join(self.out)
        if joined and not joined.endswith("\n"):
            self._emit("\n")

    # -- 文本 ---------------------------------------------------------------
    def handle_data(self, data: str) -> None:
        # 等宽块内：只做实体合规转义，不碰空白（对齐靠这些空格）
        # 等宽块外：把 CR 归一、去掉行尾空白
        if not self._in_monospace():
            data = data.replace("\r\n", "\n").replace("\r", "\n")
            data = _TRAILING_WS.sub("\n", data)
        self._emit(html.escape(data, quote=False))

    # -- 标签 ---------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()

        if tag in VOID_TAGS:
            if tag == "br":
                self._emit("\n")
            elif tag == "hr":
                self._ensure_line_start()
                self._emit("──────────────────\n")
            return

        if tag in HEADING_TAGS:
            # 标题 → 粗体（Telegram 无标题层级，用加粗表达视觉层级）
            self._ensure_line_start()
            self._emit("<b>")
            self.stack.append("b")
            return

        if tag == "li":
            self._ensure_line_start()
            self._emit("• ")
            return

        if tag in BLOCK_TAGS:
            self._ensure_line_start()
            # 块级标签不进栈：它本身不产生样式，只需一个换行边界
            return

        canonical = TAG_ALIASES.get(tag, tag)
        if canonical not in ALLOWED_TAGS:
            # 白名单外（span/font/h3 之外的杂标签）：剥标签、保留内容
            return

        if canonical == "a":
            href = dict(attrs).get("href")
            if href:
                # href 必须转义后再拼回，否则带 & 的链接会破坏实体解析
                self._emit(f'<a href="{html.escape(str(href), quote=True)}">')
                self.stack.append("a")
            return

        self._emit(f"<{canonical}>")
        self.stack.append(canonical)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in VOID_TAGS:
            return

        if tag in HEADING_TAGS:
            self._close_until("b")
            self._emit("\n")
            return

        if tag in BLOCK_TAGS or tag == "li":
            self._ensure_line_start()
            return

        canonical = TAG_ALIASES.get(tag, tag)
        if canonical not in ALLOWED_TAGS:
            return
        self._close_until(canonical)

    def _close_until(self, tag: str) -> None:
        """关闭到最近的 tag（含），顺带关闭它之上被跨越的标签。

        畸形输入里常见 <b><i></b> 这种交叉嵌套，逐层匹配会漏；
        这里按栈自上而下关闭直到命中，保证输出标签严格配对。
        """
        if tag not in self.stack:
            return  # 没有对应开标签：忽略孤立闭标签，绝不凭空补开标签
        while self.stack:
            current = self.stack.pop()
            self._emit(f"</{current}>")
            if current == tag:
                break

    # -- 收尾 ---------------------------------------------------------------
    def close_all(self) -> None:
        """补齐所有未闭合标签（TGHTML 的 Handle Malformed Input）。"""
        while self.stack:
            self._emit(f"</{self.stack.pop()}>")

    # -- 杂项 ---------------------------------------------------------------
    def handle_entityref(self, name: str) -> None:  # pragma: no cover
        # convert_charrefs=True 时通常不会走到这里，保留以兼容畸形输入
        self._emit(html.escape(f"&{name};", quote=False))

    def handle_charref(self, name: str) -> None:  # pragma: no cover
        self._emit(html.escape(f"&#{name};", quote=False))


def normalize(text: str) -> str:
    """把任意 HTML 规范化为 Telegram 兼容 HTML。

    幂等：normalize(normalize(x)) == normalize(x)。
    """
    if not text:
        return ""

    parser = _TelegramHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # 解析器在极端畸形输入下可能抛错：此时退回「原样 + 安全转义」，
        # 宁可格式难看，也不能因为美化把消息弄没了。
        return html.escape(str(text), quote=False)
    parser.close_all()

    result = "".join(parser.out)
    result = _MULTI_NEWLINE.sub("\n\n", result)
    return result.strip("\n")


def split_message(text: str, limit: int = 3500) -> list[str]:
    """按行拆分超长消息，并在拆口处闭合/重开标签。

    Telegram 单条上限 4096 字符；取 3500 留出余量（emoji 与 CJK 的计数
    与字节数不一致，紧贴上限容易在实际发送时被拒）。

    与旧的 split_html_blocks 的区别：旧版按行切了就完事，若 <pre> 表格
    被切在中间，后半块开头就是一堆裸数据、且前半块结尾的 <pre> 没闭合，
    Telegram 会整条 400 报错。这里在拆口处补 </pre>、下块开头补 <pre>。
    """
    if len(text) <= limit:
        return [text]

    blocks: list[str] = []
    current: list[str] = []
    current_len = 0
    open_tags: list[str] = []

    def flush() -> None:
        nonlocal current_len
        if not current:
            return
        body = "".join(current).rstrip()
        if body:
            # 关闭本块内仍开着的标签，保证单块自身合法
            blocks.append(body + "".join(f"</{t}>" for t in reversed(open_tags)))
        current.clear()
        current_len = 0

    for line in text.split("\n"):
        # 只在块首行重开标签：否则每行都补一次 <pre>，等宽块会层层嵌套。
        # 续块的开头补回上一块被关闭的标签，让 <pre> 表格在续块里仍是等宽。
        reopen = "" if current else "".join(f"<{t}>" for t in open_tags)
        candidate = reopen + line + "\n"
        if current_len + len(candidate) > limit and current:
            flush()
            # flush 之后才轮到本行，因此必须在块已清空时重算 reopen，
            # 否则续块开头拿不到 <pre>，等宽表格会在第二块起变成纯文本。
            candidate = "".join(f"<{t}>" for t in open_tags) + line + "\n"
        current.append(candidate)
        current_len += len(candidate)
        # 粗略维护开标签栈：行级配对，标签不跨行时完全准确
        for tag in re.findall(r"<(/?)([a-z][a-z-]*)[^>]*>", line):
            closing, name = tag
            if name in VOID_TAGS:
                continue
            if closing:
                if name in open_tags:
                    open_tags.remove(name)
            elif name in ALLOWED_TAGS:
                open_tags.append(name)

    flush()
    return blocks or [text]
