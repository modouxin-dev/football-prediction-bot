"""tghtml 规范化层测试 / Telegram HTML normalizer tests.

重点锁两条契约：

1. 等宽块内的空白必须原样保留 —— 我们的列对齐（CJK 按显示宽度补位）
   全靠 <code>/<pre> 里的空格。这正是本模块刻意**不照搬** TGHTML
   「折叠多余空白」规则的原因：折叠会把刚修好的对齐压扁。
2. normalize 幂等 —— 它挂在所有发送出口上，某些路径会经过两次
   （视图 → reply_html → split_html_blocks），不幂等就会把 & 变成
   &amp;amp; 这类二次转义污染。
"""
from __future__ import annotations

import pytest

from formatkit import display_width, kv_line, pad_cjk
from tghtml import normalize, split_message


# ---- 标签归约 ----------------------------------------------------------------

@pytest.mark.parametrize("html_in,expected", [
    ("<p>Para 1</p><p>Para 2</p>", "Para 1\nPara 2"),
    ("<div><span>keep</span> me</div>", "keep me"),
    ("<ul><li>one</li><li>two</li></ul>", "• one\n• two"),
    ("<strong>bold</strong>", "<b>bold</b>"),
    ("<em>it</em>", "<i>it</i>"),
    ("<del>gone</del>", "<s>gone</s>"),
])
def test_unsupported_tags_stripped_but_content_kept(html_in, expected):
    """白名单外的标签剥掉、内容留下（TGHTML 的 Preserve Content）。"""
    assert normalize(html_in) == expected


def test_heading_becomes_bold():
    """Telegram 没有标题概念，h1-h6 一律降级为粗体 + 换行。"""
    out = normalize("<h1>Title</h1><p>body</p>")
    assert "<b>Title</b>" in out and "body" in out
    assert "<h1>" not in out


@pytest.mark.parametrize("bad", ["<b>未闭合", "<b><i>交叉</b>", "</b>孤立闭标签", "<<<>>>"])
def test_malformed_input_never_raises(bad):
    """畸形输入不得抛异常：它来自未来的任何一处文案，崩了就是整条消息没了。"""
    assert isinstance(normalize(bad), str)


def test_unclosed_tag_is_closed():
    """未闭合标签必须补齐，否则 Telegram 整条 400。"""
    assert normalize("<b>未闭合") == "<b>未闭合</b>"


def test_crossed_nesting_is_paired():
    out = normalize("<b>粗<i>斜</b>")
    assert out == "<b>粗<i>斜</i></b>"


# ---- 转义与幂等 --------------------------------------------------------------

def test_bare_angle_bracket_is_escaped():
    """裸 '<' 必须转义：不转义会被 Telegram 当成标签起始，整条解析失败。"""
    assert "&lt;" in normalize("a < b")


@pytest.mark.parametrize("text", [
    "攻击 &gt;1：进球高于平均",
    "a &amp; b",
    "<code>主胜  67%</code>",
    "<b>粗体</b> 与 <code>等宽</code>",
    "多行\n\n\n文本",
])
def test_normalize_is_idempotent(text):
    """出口会经过多次，不幂等就会累积转义污染。"""
    once = normalize(text)
    assert normalize(once) == once


def test_entities_survive_round_trip():
    """已转义的实体不能被二次转义（&gt; 不能变成 &amp;gt;）。"""
    assert normalize("a &gt; b") == "a &gt; b"
    assert normalize("&amp;") == "&amp;"


# ---- 等宽块保护（本模块最关键的一条）------------------------------------------

def test_monospace_whitespace_is_preserved():
    """<code> 内的对齐空格必须原样保留——列对齐全靠它。"""
    raw = "<code>预期进球    1.97 | 0.73</code>"
    assert normalize(raw) == raw


def _value_column(line: str, value: str) -> int:
    """数值在 <code> 内容里的起始显示列（从 0 计）。"""
    body = line.split("<code>")[1]
    return display_width(body[: body.index(value)])


def test_monospace_alignment_survives_normalize():
    """两行等宽块经过规范化后，数值仍落在同一显示列。

    这是「不折叠空白」这条取舍的回归测试：一旦有人改成 TGHTML 那种激进
    折叠，百分比列就会互相错开，而这个测试会立刻红。
    """
    rows = [
        (kv_line("⚽", "预期进球", "1.97 | 0.73"), "1.97"),
        (kv_line("🎲", "最可能比分", "1-0"), "1-0"),
        # 值不能是标签的子串，否则 index() 会命中标签内部（「完整」在「数据完整性」里）
        (kv_line("🧩", "数据完整性", "齐备"), "齐备"),
    ]
    columns = {_value_column(normalize(line), value) for line, value in rows}
    assert columns == {12}, columns


def test_kv_line_pads_to_display_width():
    """kv_line 的标签列按显示宽度补位，4 字与 5 字标签数值起点一致。"""
    short = kv_line("⚽", "预期进球", "1.00")
    long_ = kv_line("🎲", "最可能比分", "1-0")
    assert display_width(pad_cjk("预期进球", 12)) == 12
    assert display_width(pad_cjk("最可能比分", 12)) == 12
    # 4 字与 5 字标签的字符数不同，但显示宽度都被补到 12，故数值起点一致
    assert _value_column(short, "1.00") == _value_column(long_, "1-0") == 12
    assert short.split("<code>")[1].startswith(pad_cjk("预期进球", 12))
    assert long_.split("<code>")[1].startswith(pad_cjk("最可能比分", 12))


def test_multi_newline_collapsed_to_two():
    assert normalize("a\n\n\n\n\nb") == "a\n\nb"


# ---- 拆分 --------------------------------------------------------------------

def test_short_text_not_split():
    assert split_message("short", 100) == ["short"]


def test_split_blocks_keep_tags_balanced():
    """拆口处必须闭合/重开标签：半截 <pre> 会让 Telegram 整条 400。"""
    long = "<pre>" + "\n".join(f"row{i:03d}  data" for i in range(400)) + "</pre>"
    parts = split_message(long, 500)
    assert len(parts) > 1
    for part in parts:
        assert part.count("<pre>") == part.count("</pre>") == 1


def test_split_respects_limit():
    long = "\n".join(f"line {i}" for i in range(2000))
    for part in split_message(long, 500):
        assert len(part) <= 500


def test_split_plain_text_keeps_all_lines():
    """拆分不能吞内容：所有行都要出现在某一块里。"""
    long = "\n".join(f"line{i}" for i in range(300))
    joined = "".join(split_message(long, 300))
    for i in range(300):
        assert f"line{i}" in joined


def test_empty_input():
    assert normalize("") == ""
    assert normalize(None or "") == ""
