"""首页启发式抽作者：形状门 + 标题碎片过滤。

2026-10-10 真机事故：30 篇语料里有 8 篇把标题碎片、摘要句子当成人名写进了
``paper_authors``（``Collaborative Platform``、``for Social``、``we propose an automated``、
``Additional Key Words``…），而且因为合并规则 4「authors 取最长列表」，这些垃圾还盖住了
PDF 内嵌的干净作者。本文件锁住两件事：

* 形状门（``_plausible_author``）：不是人名的字符串一律不进列表；
* 标题碎片过滤（``detect_authors``）：换行标题的尾行不再被按词对切成"人名"。

反例全部来自真机数据，不是编的。
"""

from __future__ import annotations

import pytest

from app.parsing.pdf import PageText
from app.services.metadata_service import (
    _is_title_fragment,
    _plausible_author,
    _title_word_set,
    detect_authors,
)

#: 真机落库的垃圾（2604.01520 / 2608.13472 / 2402.07138 / 2308.00708 / 2010.03525）。
REAL_JUNK = (
    "Collaborative Platform",
    "for Social",
    "Science Automation",
    "from Topology",
    "Generation to",
    "Design Framework",
    "we propose an automated",
    "In this study",
    "public policy",
    "Additional Key Words",
    "Phrases: Transformation by Example",
    "Edited by: Paul Ralph",
    "for Software Engineering Research",
    "Language Understanding",
    "they miss transform",
    "its engineering→ Programming by example",
    "swchen }@usc.edu",
    "Ant Group",
    "Graduate Student Member",
)

#: 真机落库的干净人名（含全大写、连字符、复姓、重音符、缩写点）。
REAL_NAMES = (
    "Lei Wang",
    "Yuanzi Li",
    "Ji-Rong Wen",
    "Peide D. Ye",
    "Áron Szabó",
    "TIMOFEY BRYKSIN",
    "Joseph C. O’Brien",
    "Hongyi Dou",
    "Ludwig van Beethoven",
    "Maria de la Cruz",
)


@pytest.mark.parametrize("junk", REAL_JUNK)
def test_real_junk_is_not_a_name(junk: str) -> None:
    assert not _plausible_author(junk), junk


@pytest.mark.parametrize("name", REAL_NAMES)
def test_real_names_survive_the_shape_gate(name: str) -> None:
    assert _plausible_author(name), name


def test_title_words_are_recognised_as_fragments() -> None:
    title = "LLM Agents as Social Scientists: A Human-AI Collaborative Platform"
    words = _title_word_set(title)

    assert _is_title_fragment("Collaborative Platform", words)
    assert not _is_title_fragment("Lei Wang", words)
    assert not _is_title_fragment("Collaborative", words), "单词不算（可能是人名）"
    # "for Social" 走的是另一道门：标题里没有 "for"，所以它不是"标题碎片"，
    # 全靠形状门拒绝（见 REAL_JUNK）。两道门各管一类，这里把分工写清楚。
    assert not _is_title_fragment("for Social", words)
    assert not _plausible_author("for Social")


def test_a_wrapped_title_does_not_become_authors() -> None:
    """事故的核心：标题跨两行时，整标题匹配不上，尾行就落到了姓名切分器手里。

    尾行 "A Human-AI Collaborative Platform for Social Science Automation" 是偶数个词
    （8 个，落在 4<n<=10 的「按词对人名」分支），于是被切成
    "Collaborative Platform" / "for Social" / "Science Automation" 三个"人名"。
    """
    page = PageText(
        page=1,
        text="\n".join(
            [
                "LLM Agents as Social Scientists: A Human-AI",
                "Collaborative Platform for Social Science Automation",
                "Lei Wang, Yuanzi Li, Jinchao Wu",
                "Abstract",
                "Traditional social science research often requires designing complex experiments.",
            ]
        ),
    )

    # 真实调用传的是 PDF 里检出的**完整**标题。
    authors = detect_authors(
        [page], title="LLM Agents as Social Scientists: A Human-AI Collaborative Platform"
    )

    assert authors == ["Lei Wang", "Yuanzi Li", "Jinchao Wu"]


def test_the_author_block_ends_at_the_front_matter() -> None:
    """ACM/IEEE 正刊会印 "Additional Key Words and Phrases"，那不是人名。"""
    page = PageText(
        page=1,
        text="\n".join(
            [
                "A Study of Things",
                "Alice Johnson and Bob Smith",
                "Additional Key Words and Phrases: software engineering, peer review",
                "Carol Danvers",
            ]
        ),
    )

    assert detect_authors([page], title="A Study of Things") == ["Alice Johnson", "Bob Smith"]


def test_prose_after_a_missing_abstract_marker_stays_out() -> None:
    """没有 Abstract 标记时的兜底：摘要句子靠形状门被挡下，而不是靠标记。"""
    page = PageText(
        page=1,
        text="\n".join(
            [
                "Can Machines Replace Us",
                "Dana Lee, Erin Park",
                "In this study we propose an automated pipeline.",
                "we demonstrate that interaction history matters",
                "Additional results are reported below.",
            ]
        ),
    )

    assert detect_authors([page], title="Can Machines Replace Us") == ["Dana Lee", "Erin Park"]


def test_a_single_line_byline_still_works() -> None:
    """回归护栏：最普通的一行式作者栏不能被误杀。"""
    page = PageText(page=1, text="\n".join(["A Gate-All-Around FET", "Zhuocheng Zhang, Zehao Lin", "Abstract"]))

    assert detect_authors([page], title="A Gate-All-Around FET") == ["Zhuocheng Zhang", "Zehao Lin"]


def test_the_title_line_itself_is_skipped_even_when_matched() -> None:
    """标题整行匹配上时，起点应跳到下一行；这里连带确认标题行不会变成人名。"""
    page = PageText(
        page=1,
        text="\n".join(["Simple Title", "Ann Lee, Ben Ray", "Abstract"]),
    )

    assert detect_authors([page], title="Simple Title") == ["Ann Lee", "Ben Ray"]
