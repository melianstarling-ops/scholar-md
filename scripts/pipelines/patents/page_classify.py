"""页型分类 + PyMuPDF 取词层（第二期可替换为 OCR 取词）。

把每页分为：COVER / FRONT_MATTER / FIGURE / SPEC_BODY。
SPEC_BODY 同时实测 gutter_x（由中央"5 的倍数"行号阶梯的中位 x 得到）。

判定顺序（实测 5 份美国专利稳定）：
  1. page 0           → COVER
  2. 页眉含 "Page N"  → FRONT_MATTER（引用文献表/分类号续页）
  3. 有中央行号阶梯   → SPEC_BODY
  4. 词数 < 阈值      → FIGURE
  5. 其余             → FRONT_MATTER（兜底）
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum

import fitz

from profiles import LayoutProfile
from reading_order import Word


class PageKind(str, Enum):
    COVER = "COVER"
    FRONT_MATTER = "FRONT_MATTER"
    FIGURE = "FIGURE"
    SPEC_BODY = "SPEC_BODY"


@dataclass
class PageInfo:
    index: int
    kind: PageKind
    width: float
    height: float
    words: list[Word]
    gutter_x: float = 0.0
    ladder: list[float] = field(default_factory=list)


def page_words(page: "fitz.Page") -> list[Word]:
    """PyMuPDF words -> Word（按 y 再 x 粗排）。"""
    raw = page.get_text("words")  # (x0,y0,x1,y1, text, block, line, word_no)
    return [Word(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]


def _ladder_xcenters(words: list[Word], center_x: float, profile: LayoutProfile) -> list[float]:
    return [
        w.xc
        for w in words
        if w.text.isdigit()
        and int(w.text) % 5 == 0
        and 5 <= int(w.text) <= profile.ladder_value_max
        and abs(w.xc - center_x) <= profile.ladder_band_halfwidth
    ]


def classify_page(index: int, page: "fitz.Page", profile: LayoutProfile) -> PageInfo:
    words = page_words(page)
    width, height = page.rect.width, page.rect.height
    center_x = width / 2
    info = PageInfo(index=index, kind=PageKind.FIGURE, width=width, height=height, words=words)

    header_str = " ".join(w.text for w in words[:15])
    ladder = _ladder_xcenters(words, center_x, profile)
    gutter = statistics.median(ladder) if ladder else center_x
    # 双栏正文弱判据：gutter 两侧各有足量词。claims 续页行号稀疏(不够 ladder_min_count)
    # 但仍是双栏正文，靠"两栏均有 ≥body_column_min_words 词"识别，避免误判为 FRONT_MATTER。
    left_words = sum(1 for w in words if w.xc < gutter - 20)
    right_words = sum(1 for w in words if w.xc > gutter + 20)
    two_col_body = (
        len(ladder) >= profile.ladder_min_count_weak
        and left_words >= profile.body_column_min_words
        and right_words >= profile.body_column_min_words
    )

    if index == profile.cover_page_index:
        info.kind = PageKind.COVER
    elif profile.frontmatter_header_re.search(header_str):
        info.kind = PageKind.FRONT_MATTER
    elif len(ladder) >= profile.ladder_min_count or two_col_body:
        info.kind = PageKind.SPEC_BODY
        info.gutter_x = gutter
        info.ladder = ladder
    elif len(words) < profile.figure_word_max:
        info.kind = PageKind.FIGURE
    else:
        info.kind = PageKind.FRONT_MATTER
    return info


def classify_document(doc: "fitz.Document", profile: LayoutProfile) -> list[PageInfo]:
    return [classify_page(i, doc[i], profile) for i in range(doc.page_count)]
