"""PDF 文本层抽取与健康度:born-digital 路线 B 的"文本层第二传感器"地基。

本模块只做抽取/统计/归一化,不做采信判定——bbox 对齐(page_geometry/
assign_source_words)与采信门(prose_adoption)是后续任务的职责,不在此实现。

两条互不污染的归一化路径:
  - normalize_prose_for_content:NFC,采信内容用,保守,不折叠上下标语义。
  - normalize_prose_for_compare:NFKC,审计对账/文本比较用,允许折叠。
数字 token 提取必须在 NFKC 折叠**之前**处理上标指数,否则 `10²` 与 `102`
会被 NFKC 直接判成同一个字符串,指数语义永久丢失(计划 §6.4"折叠陷阱")。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

import fitz

from scripts.pipelines.textbooks import checkpoint as _checkpoint
from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout

# ---- PUA 判定:与 triage.py 的惯例一致,独立维护(只读参考,不导入其私有实现) ----
_PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))

# 常见连字(ligature)展开表——与 Unicode NFKC 的兼容分解一致,只展开不判坏(§6.2)。
_LIGATURE_EXPANSIONS = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}

# 比较视图中互相等价的"连字符类"字符:连字符变体、减号——普通字符比较时统一折叠。
_HYPHEN_LIKE_FOR_COMPARE = "‐‑‒–−-"
_CANONICAL_HYPHEN = "-"

# 行末续行连字符判定(含软连字符)。
_LINE_END_HYPHENS = ("-", "‐", "­")

# 单字符碎片率/低文本页标记的占位阈值——待后续任务用真实语料实测分布标定
# (计划 §8.4:对全部样书离线跑 source_health 分布后标定 ROUTE_B_V1_THRESHOLDS)。
LOW_TEXT_CHAR_THRESHOLD = 20

_WS_RE = re.compile(r"\s+")

# 上标数字/正负号 → ASCII,用于在 NFKC 折叠前显式标记指数(10² → 10^2)。
_SUPER_TRANSLATE = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_SUPER_RUN_RE = re.compile(r"(?<=[0-9])([⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")

# 数字 token:按优先级排列的互斥分支——range/科学计数法/指数记号必须排在
# 普通数字前面,否则会被普通数字分支提前截断,丢失指数或范围语义。
_NUMERIC_TOKEN_RE = re.compile(
    r"""
    (?P<range>
        (?<![A-Za-z0-9])\d+(?:\.\d+)?\s*[\-–−~]\s*\d+(?:\.\d+)?(?![A-Za-z0-9])
    )
  | (?P<scitimes>
        (?<![A-Za-z0-9])[+\-−]?\d+(?:\.\d+)?\s*[×xX]\s*10\^[+\-−]?\d+
    )
  | (?P<sciascii>
        (?<![A-Za-z0-9])[+\-−]?\d+(?:\.\d+)?[eE][+\-−]?\d+
    )
  | (?P<caretexp>
        (?<![A-Za-z0-9])[+\-−]?\d+(?:\.\d+)?\^[+\-−]?\d+
    )
  | (?P<plain>
        (?<![A-Za-z0-9])[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?
    )
    """,
    re.VERBOSE,
)

# 单位 token:不维护封闭专业词典,只用结构特征(含大写字母/斜杠/常见单位符号)
# 粗筛,允许一定误报——这是计划 §6.4 明确接受的取舍。
_UNIT_RE = re.compile(r"[A-Za-zµμΩ°%]{1,8}(?:/[A-Za-zµμΩ°]{1,8})?")


@dataclass(frozen=True)
class SourceWord:
    text: str
    bbox: tuple[float, float, float, float]
    block_no: int
    line_no: int
    word_no: int


def _is_pua(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _PUA_RANGES)


def _is_bad_control(ch: str) -> bool:
    """非空白 C0/C1 控制字符——换行/tab/回车等空白控制字符不算坏。"""
    return unicodedata.category(ch) == "Cc" and not ch.isspace()


def _is_unassigned(ch: str) -> bool:
    return unicodedata.category(ch) == "Cn"


def _expand_ligatures(text: str) -> str:
    for lig, expansion in _LIGATURE_EXPANSIONS.items():
        if lig in text:
            text = text.replace(lig, expansion)
    return text


def extract_source_page(page: fitz.Page) -> dict:
    """从 fitz.Page 抽取 words/整页文本/字体信息——source_health 的输入。

    fitz 能取到什么记什么:取不到的字段记 None,不猜测补全。
    """
    raw_words = page.get_text("words")
    words = [
        SourceWord(
            text=w[4],
            bbox=(w[0], w[1], w[2], w[3]),
            block_no=w[5],
            line_no=w[6],
            word_no=w[7],
        )
        for w in raw_words
    ]

    doc = page.parent
    fonts = []
    for f in page.get_fonts(full=True):
        xref, _ext, font_type, basefont, name, encoding = f[:6]
        has_tounicode = None
        if doc is not None:
            try:
                has_tounicode = "/ToUnicode" in doc.xref_object(xref)
            except Exception:
                has_tounicode = None
        fonts.append(
            {
                "xref": xref,
                "name": name or None,
                "basefont": basefont or None,
                "type": font_type or None,
                "encoding": encoding or None,
                "has_tounicode": has_tounicode,
            }
        )

    return {
        "page_number": page.number,
        "words": words,
        "text": page.get_text(),
        "fonts": fonts,
    }


def source_health(source_page: dict) -> dict:
    """逐页文本层健康度统计(计划 §6.2)。

    只统计、分类计数,不给单一全文百分比、不做采信判定——
    结构保持 page → 分类计数层级,由调用方按类别自行判断风险。
    """
    words = source_page.get("words") or []

    non_space_chars = 0
    fffd_count = 0
    pua_count = 0
    control_count = 0
    unassigned_count = 0
    ligature_count = 0
    single_char_fragments = 0

    lines: dict[tuple[int, int], list[SourceWord]] = {}

    for w in words:
        text = w.text
        if len(text.strip()) == 1:
            single_char_fragments += 1
        for ch in text:
            if ch.isspace():
                continue
            non_space_chars += 1
            if ch == "�":
                fffd_count += 1
            elif _is_pua(ch):
                pua_count += 1
            elif _is_bad_control(ch):
                control_count += 1
            elif _is_unassigned(ch):
                unassigned_count += 1
            if ch in _LIGATURE_EXPANSIONS:
                ligature_count += 1
        lines.setdefault((w.block_no, w.line_no), []).append(w)

    word_count = len(words)
    line_count = len(lines)
    fragment_rate = (single_char_fragments / word_count) if word_count else 0.0

    hyphen_line_ends = 0
    line_texts = []
    for key in sorted(lines):
        line_words = sorted(lines[key], key=lambda item: item.word_no)
        last_text = line_words[-1].text if line_words else ""
        if last_text.endswith(_LINE_END_HYPHENS):
            hyphen_line_ends += 1
        line_texts.append(" ".join(item.text for item in line_words))
    line_end_hyphen_rate = (hyphen_line_ends / line_count) if line_count else 0.0

    seen: dict[str, int] = {}
    for lt in line_texts:
        if not lt.strip():
            continue
        seen[lt] = seen.get(lt, 0) + 1
    repeated_line_candidates = sorted(t for t, c in seen.items() if c > 1)

    fonts = source_page.get("fonts") or []
    suspected_missing_tounicode_cid = any(
        f.get("type") == "Type0" and f.get("has_tounicode") is False for f in fonts
    )

    return {
        "non_space_char_count": non_space_chars,
        "word_count": word_count,
        "line_count": line_count,
        "fffd_count": fffd_count,
        "pua_count": pua_count,
        "control_char_count": control_count,
        "unassigned_codepoint_count": unassigned_count,
        "ligature_count": ligature_count,
        "single_char_fragment_rate": fragment_rate,
        "line_end_hyphen_rate": line_end_hyphen_rate,
        "repeated_line_candidates": repeated_line_candidates,
        "fonts": fonts,
        "suspected_missing_tounicode_cid": suspected_missing_tounicode_cid,
        "is_blank": non_space_chars == 0,
        "is_low_text": 0 < non_space_chars < LOW_TEXT_CHAR_THRESHOLD,
    }


def normalize_prose_for_content(text: str) -> str:
    """采信内容用:保守——只做 NFC + 连字展开 + 空白统一,不做任何"纠正"。

    刻意不用 NFKC:NFKC 会把上标数字折叠成普通数字,破坏指数语义。
    """
    t = unicodedata.normalize("NFC", text or "")
    t = _expand_ligatures(t)
    return _WS_RE.sub(" ", t).strip()


def normalize_prose_for_compare(text: str) -> str:
    """审计对账用:NFKC + 空白统一 + 连字展开 + 连字符类字符等价折叠。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = _expand_ligatures(t)
    for ch in _HYPHEN_LIKE_FOR_COMPARE:
        t = t.replace(ch, _CANONICAL_HYPHEN)
    return _WS_RE.sub(" ", t).strip()


def _looks_like_unit(candidate: str) -> bool:
    """结构特征粗筛,不维护封闭专业词典——允许一定误报(计划 §6.4)。"""
    return bool(candidate) and (
        "/" in candidate
        or any(c.isupper() for c in candidate)
        or any(c in "°Ω%µμ" for c in candidate)
    )


def extract_numeric_tokens(text: str) -> list[str]:
    """提取数字 token(计划 §6.4/§6.6)。

    在 NFKC 折叠之前处理指数上标(10² → 10^2)——折叠之后 `10²` 与字面
    `102` 会变成同一个字符串,指数语义就再也找不回来了。
    """
    if not text:
        return []

    marked = _SUPER_RUN_RE.sub(
        lambda m: "^" + m.group(1).translate(_SUPER_TRANSLATE), text
    )

    tokens: list[str] = []
    for m in _NUMERIC_TOKEN_RE.finditer(marked):
        raw = m.group(0)
        tokens.append(raw.replace("−", "-"))

        after = marked[m.end():]
        if after.startswith(" "):
            after = after[1:]
        unit_m = _UNIT_RE.match(after)
        if unit_m and _looks_like_unit(unit_m.group(0)):
            tokens.append(unit_m.group(0))

    return tokens


def ngram_repetition_score(text: str, n: int = 8) -> float:
    """纯文本自检:字符 n-gram 重复集中度,越高越像 VLM 重复环退化。

    不需要源文本;空文本/短于 n 的文本合法返回 0.0。
    """
    t = _WS_RE.sub(" ", (text or "").strip())
    if len(t) < n:
        return 0.0
    grams = [t[i : i + n] for i in range(len(t) - n + 1)]
    total = len(grams)
    unique = len(set(grams))
    return 1.0 - (unique / total)


# ===========================================================================
# PDF/OCR bbox 对齐:几何归一化 + word→block 归属(计划 §6.1,内容关键路径)
# ---------------------------------------------------------------------------
# 输出直接决定下游"采信门"用哪些块的文本层字符替换 OCR。任何归属错误都会变成
# 内容错误——因此纪律是:无法可靠映射时明确返回 UNSCORABLE / unassigned,绝不猜。
# 采信判定本身(prose_adoption)属于 Task 5,不在此实现。
# ===========================================================================

# 弱交叠归属阈值的占位默认——生产阈值待 §8.4 真实语料标定;测试注入显式值,
# 不依赖此常数(计划 §6.1 规则 3:阈值可注入,不写死生产常数入判定路径)。
DEFAULT_OVERLAP_THRESHOLD = 0.5

# 坐标/尺寸比较容差(浮点噪声)。
_GEOM_EPS = 1e-6


@dataclass(frozen=True)
class PageGeometry:
    """一页的 PDF(point)↔ OCR(pixel)几何对照。

    unscorable=True 时该页几何不可靠(旋转/cropbox 异常/缺 OCR 尺寸等),
    下游必须据此对该页所有归属结果传播"禁止采信"信号,不得猜测映射。
    """

    pdf_width: float
    pdf_height: float
    ocr_width: float
    ocr_height: float
    rotation: int
    unscorable: bool = False
    unscorable_reason: str | None = None


def _positive_number(value) -> float | None:
    """value 是有限正数则返回 float,否则 None(用于校验尺寸/坐标)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def _valid_bbox(bbox) -> bool:
    """bbox 是形状良好的 [x0,y0,x1,y1]:四个有限非负数值且 x1>x0、y1>y0。

    畸形(x1<x0/负值/非数值/缺项/零面积)一律判假——上游据此走 unassigned,
    绝不把畸形框强行参与归属。bool 不算合法坐标(避免 True/False 被当 1/0)。
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    for c in bbox:
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return False
        if not math.isfinite(float(c)) or c < 0:
            return False
    x0, y0, x1, y1 = bbox
    return x1 > x0 and y1 > y0


def _cropbox_anomalous(page: fitz.Page) -> bool:
    """cropbox 是否异常:非零原点或 cropbox≠mediabox。

    正常页 mediabox 原点在 (0,0) 且 cropbox==mediabox,此时 get_text 坐标与
    整页渲染像素是同一线性空间;一旦偏移/裁剪,简单缩放不再成立 → 判异常。
    """
    try:
        mediabox = page.mediabox
        cropbox = page.cropbox
    except Exception:
        return True
    if abs(mediabox.x0) > _GEOM_EPS or abs(mediabox.y0) > _GEOM_EPS:
        return True
    for a, b in (
        (cropbox.x0, mediabox.x0),
        (cropbox.y0, mediabox.y0),
        (cropbox.x1, mediabox.x1),
        (cropbox.y1, mediabox.y1),
    ):
        if abs(a - b) > _GEOM_EPS:
            return True
    return False


def page_geometry(page: fitz.Page, ocr_result: dict) -> PageGeometry:
    """构造一页的 PDF↔OCR 几何对照(计划 §6.1 规则 4)。

    旋转页(90/180/270)、cropbox 异常、OCR JSON 缺/坏 width/height、PDF 尺寸
    非法——任一命中即 unscorable,不猜测。OCR 页级 width/height 取 res JSON 顶层。
    """
    rotation = int(getattr(page, "rotation", 0) or 0)
    rect = page.rect
    pdf_width = float(rect.width)
    pdf_height = float(rect.height)

    result = ocr_result if isinstance(ocr_result, dict) else {}
    ocr_w = _positive_number(result.get("width"))
    ocr_h = _positive_number(result.get("height"))

    reasons: list[str] = []
    if rotation != 0:
        reasons.append("rotation")
    if _cropbox_anomalous(page):
        reasons.append("cropbox")
    if ocr_w is None or ocr_h is None:
        reasons.append("ocr_dims")
    if not (pdf_width > 0 and pdf_height > 0):
        reasons.append("pdf_dims")

    return PageGeometry(
        pdf_width=pdf_width,
        pdf_height=pdf_height,
        ocr_width=ocr_w if ocr_w is not None else 0.0,
        ocr_height=ocr_h if ocr_h is not None else 0.0,
        rotation=rotation,
        unscorable=bool(reasons),
        unscorable_reason=",".join(reasons) if reasons else None,
    )


def normalize_bbox(
    bbox, width: float, height: float
) -> tuple[float, float, float, float]:
    """绝对坐标 → [0,1] 页面分数,使 PDF-point 与 OCR-pixel 落到同一比较空间。

    要求 width/height 为正;bbox 合法性由调用方以 _valid_bbox 预校验。
    """
    if width <= 0 or height <= 0:
        raise ValueError("normalize_bbox: width/height 必须为正")
    x0, y0, x1, y1 = bbox
    return (x0 / width, y0 / height, x1 / width, y1 / height)


def overlap_ratio(word_bbox, block_bbox) -> float:
    """交叠面积占 **word 面积** 的比例(两 bbox 须在同一坐标空间)。

    无交叠或 word 面积非正 → 0.0。用于中心点未命中时的"覆盖率最高"回退。
    """
    ix0 = max(word_bbox[0], block_bbox[0])
    iy0 = max(word_bbox[1], block_bbox[1])
    ix1 = min(word_bbox[2], block_bbox[2])
    iy1 = min(word_bbox[3], block_bbox[3])
    iw = ix1 - ix0
    ih = iy1 - iy0
    if iw <= 0 or ih <= 0:
        return 0.0
    word_area = (word_bbox[2] - word_bbox[0]) * (word_bbox[3] - word_bbox[1])
    if word_area <= 0:
        return 0.0
    return (iw * ih) / word_area


def _bbox_area(b) -> float:
    return (b[2] - b[0]) * (b[3] - b[1])


def assign_word_to_block(
    word, blocks, *, overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD
) -> int | None:
    """把一个 word 归属到 blocks 之一,返回其下标或 None(计划 §6.1 规则 1-3)。

    word / blocks 均为**同一坐标空间**的 [x0,y0,x1,y1](通常是 normalize_bbox
    产出的分数)。判定:
      1. word 中心点落入的块优先;多个命中取面积更小(语义更具体)者。
      2. 中心点未命中但有交叠时,取 word 面积覆盖率最高者。
      3. 最高覆盖率 < overlap_threshold 的弱交叠不强行归属,返回 None。
    所有并列一律用 (面积, bbox 坐标) 做确定性 tie-break,不依赖 blocks 的顺序,
    因此对 blocks 洗牌结果不变(同一 word 不会因块序变化被归到不同块)。
    边界:两块 bbox 逐坐标完全相同时,tie-break 元组相等、退回输入顺序——这是几何
    上无法区分的病态 OCR 输出(重复框),两候选内容等价,洗牌不变性对该退化情形不保证。
    """
    cx = (word[0] + word[2]) / 2.0
    cy = (word[1] + word[3]) / 2.0

    contained: list[tuple[float, tuple, int]] = []
    for i, b in enumerate(blocks):
        if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
            contained.append((_bbox_area(b), tuple(b), i))
    if contained:
        # 面积最小者更具体;面积并列则按 bbox 坐标定序(确定性,与块序无关)。
        contained.sort(key=lambda t: (t[0], t[1]))
        return contained[0][2]

    best_key: tuple | None = None
    best_index: int | None = None
    for i, b in enumerate(blocks):
        ratio = overlap_ratio(word, b)
        if ratio <= 0:
            continue
        # 覆盖率高者优先;并列取面积小、再取 bbox 坐标——均与块序无关。
        key = (-ratio, _bbox_area(b), tuple(b))
        if best_key is None or key < best_key:
            best_key = key
            best_index = i
    if best_key is None:
        return None
    if (-best_key[0]) < overlap_threshold:
        return None
    return best_index


def assign_source_words(
    words,
    blocks,
    geometry: PageGeometry,
    *,
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> dict:
    """把文本层 words 归属到 OCR blocks(计划 §6.1 规则 5)。

    返回 dict 明确区分:
      - assignments: {block_index: [SourceWord, ...]}——按 block 分桶的 words。
      - block_labels: {block_index: block_label}——保留每块语义标签,本模块不丢
        标签信息;table/formula/image 等非正文块内的 words 归属到该块(因此天然
        不进 prose 桶——分桶由 label 交给下游区分)。
      - unassigned: 未能可靠归属的 SourceWord 列表。
      - geometry_unscorable / adoption_forbidden: 该页几何不可靠时置真,向下游
        明确传播"禁止采信"信号;此时不产出任何归属,全部 words 落 unassigned。
    """
    result = {
        "geometry_unscorable": bool(geometry.unscorable),
        "adoption_forbidden": bool(geometry.unscorable),
        "assignments": {},
        "block_labels": {},
        "unassigned": [],
    }

    # 保留全部块的 label(即便畸形块不接收 words,标签信息也不丢)。
    for i, b in enumerate(blocks):
        result["block_labels"][i] = b.get("block_label") if isinstance(b, dict) else None

    if geometry.unscorable:
        # 不猜:整页禁止采信,words 明确全部 unassigned。
        result["unassigned"] = list(words)
        return result

    # OCR 像素块 → 页面分数;畸形块跳过(不参与归属)。
    norm_bboxes: list[tuple] = []
    index_map: list[int] = []
    for i, b in enumerate(blocks):
        bb = b.get("block_bbox") if isinstance(b, dict) else None
        if _valid_bbox(bb):
            norm_bboxes.append(
                normalize_bbox(bb, geometry.ocr_width, geometry.ocr_height)
            )
            index_map.append(i)

    for w in words:
        if not _valid_bbox(getattr(w, "bbox", None)):
            result["unassigned"].append(w)
            continue
        nw = normalize_bbox(w.bbox, geometry.pdf_width, geometry.pdf_height)
        local = assign_word_to_block(
            nw, norm_bboxes, overlap_threshold=overlap_threshold
        )
        if local is None:
            result["unassigned"].append(w)
        else:
            result["assignments"].setdefault(index_map[local], []).append(w)

    return result


# ===========================================================================
# 正文双向对账审计(计划 §6.4,Task 6):页级 audit_prose
# ---------------------------------------------------------------------------
# 输入契约(纯页级函数,不接触 fitz.Page,不做任何 bbox 几何运算——那是 Task 4
# assign_source_words 的职责,本函数直接消费其返回结构):
#   - source_page:extract_source_page 输出;SourceWord 的 block_no/line_no/
#     word_no 保持 Task 3 原义(fitz 原生段落/行/词分组),本模块不重解释、
#     不要求调用方改写字段——SourceWord 是 frozen dataclass,重赋值意味着
#     上游必须重建对象,是不必要的隐藏耦合。
#   - blocks:apply_adoption 之后的最终内容块(block_label/block_content/...)。
#   - decisions:Task 5 的 list[AdoptionDecision],按 block_id 与 blocks 下标
#     一一对应。
#   - assignment:assign_source_words 的返回结构(计划 §6.1)。块内归属源
#     words 取 assignment["assignments"][block_index](block 枚举下标,与
#     AdoptionDecision.block_id 同一索引空间);未归属 words 取
#     assignment["unassigned"];页面几何是否禁止采信取
#     assignment["geometry_unscorable"]。
# 本函数只读:绝不修改 blocks / decisions / source_page / assignment,只产生
# issues/metrics。
# ===========================================================================


@dataclass(frozen=True)
class AuditThresholds:
    """页级正文审计阈值(计划 §6.4)。生产 profile 待 Task 13 标定,测试注入显式值。"""

    minimum_reliable_chars: int
    maximum_bad_char_ratio: float
    maximum_block_ned: float
    minimum_char_recall: float
    minimum_token_recall: float
    minimum_numeric_token_recall: float
    maximum_addition_ratio: float
    maximum_repetition_score: float
    minimum_single_column_sequence_ratio: float


# 独立重跑/CLI 默认占位 profile——生产阈值待 Task 13 用真实语料标定(计划
# §8.4)。名字显式含 uncalibrated,防止被误当生产阈值直接使用;Task 13 标定
# 后如需切换 profile,应在调用方(CLI/Task 9)显式传入新 AuditThresholds,
# 不在本模块内静默替换。
THRESHOLD_PROFILE_UNCALIBRATED = "route_b_v1_uncalibrated"

ROUTE_B_V1_UNCALIBRATED_THRESHOLDS = AuditThresholds(
    minimum_reliable_chars=10,
    maximum_bad_char_ratio=0.1,
    maximum_block_ned=0.3,
    minimum_char_recall=0.8,
    minimum_token_recall=0.8,
    minimum_numeric_token_recall=0.8,
    maximum_addition_ratio=0.3,
    maximum_repetition_score=0.5,
    minimum_single_column_sequence_ratio=0.7,
)

# 独立重跑无 Task 9 记录决策时,audit_document 现场跑 adopt_prose_blocks 的
# dry-run 采信推演阈值——同样是占位值(待 Task 13 标定),只用于审计分派,
# 绝不是采信主链的判定依据(hybrid 主链的真实采信由 Task 9 传入 decisions_by_page)。
_DRY_RUN_ADOPTION_MIN_CHAR_RATIO = 0.5
_DRY_RUN_ADOPTION_MAX_CHAR_RATIO = 2.0
_DRY_RUN_ADOPTION_MAX_NED = 0.2


def _audit_join_words(words) -> str:
    """按 (line_no, word_no) 排序拼接,词间单空格——与 build_adopted_text 的排序惯例一致。"""
    ordered = sorted(words, key=lambda w: (w.line_no, w.word_no))
    return " ".join(w.text for w in ordered)


def _audit_source_reliability(words) -> tuple[int, float]:
    """归属 words 的非空白字符总数、坏字符(U+FFFD/PUA/非空白控制符)占比。"""
    total = 0
    bad = 0
    for w in words:
        for ch in w.text:
            if ch.isspace():
                continue
            total += 1
            if ch == "�" or _is_pua(ch) or _is_bad_control(ch):
                bad += 1
    ratio = (bad / total) if total else 0.0
    return total, ratio


def _multiset_overlap(a_items, b_items) -> int:
    """两个多重集的交集大小(逐元素取最小计数之和)——对乱序/换位不敏感的廉价初筛。"""
    return sum((Counter(a_items) & Counter(b_items)).values())


def _recall(overlap: int, total: int) -> float:
    return (overlap / total) if total else 1.0


def _weighted_ratio(pairs) -> float | None:
    """pairs: [(overlap, total), ...] → micro-average(Σoverlap/Σtotal)。

    这就是"按字符量加权"的直接实现:分母越大的块,天然占更大权重,不会被
    "每块平均"掩盖长块丢失。无贡献块时返回 None(不得除零)。
    """
    total_sum = sum(total for _, total in pairs)
    if total_sum <= 0:
        return None
    overlap_sum = sum(overlap for overlap, _ in pairs)
    return overlap_sum / total_sum


def _single_column_proxy_confirmed(words) -> bool:
    """多栏代理(计划 §6.4):源 words(source_page['words'] 原始顺序)的
    block_no 序列(fitz 原生段落分组编号)单调不减才可信——多栏/交错页返回
    False,调用方据此完全跳过 sequence_ratio(不计算、不比较、不作硬门)。
    """
    if len(words) < 2:
        return False
    seq = [w.block_no for w in words]
    return all(a <= b for a, b in zip(seq, seq[1:]))


def _block_sequence_ratio(
    words, assignments_by_block: dict[int, list], prose_block_indices
) -> float | None:
    """真实的块级顺序一致率(计划 §6.4):按 OCR 块顺序(blocks 下标升序)排列
    参与对账的正文块,取每块归属源 words 在全页原生 (block_no, line_no,
    word_no) 排序秩中的中位数作为该块的"源序秩";统计相邻块对里源序秩非
    递减的比例。可比块(有归属 words 的块)少于 2 个 → 返回 None,不发指标
    ——绝不返回占位值。
    """
    ordered_words = sorted(words, key=lambda w: (w.block_no, w.line_no, w.word_no))
    rank_of = {id(w): rank for rank, w in enumerate(ordered_words)}

    medians: list[float] = []
    for i in sorted(prose_block_indices):
        block_words = assignments_by_block.get(i, [])
        ranks = sorted(rank_of[id(w)] for w in block_words if id(w) in rank_of)
        if not ranks:
            continue
        mid = len(ranks) // 2
        if len(ranks) % 2 == 1:
            median = float(ranks[mid])
        else:
            median = (ranks[mid - 1] + ranks[mid]) / 2.0
        medians.append(median)

    if len(medians) < 2:
        return None
    agree = sum(1 for a, b in zip(medians, medians[1:]) if b >= a)
    return agree / (len(medians) - 1)


def audit_prose(
    source_page: dict,
    blocks: list[dict],
    decisions,
    assignment: dict,
    thresholds: AuditThresholds,
) -> dict:
    """页级正文双向对账审计(计划 §6.4)。只读:不改 blocks/decisions/source_page/assignment。

    按块 provenance 分派:
      - 采信块(content_source=source_text):只记录 decisions 里已有的
        block_ned,不重复告警(采信门规则 6 已兜底)。
      - 回退块(content_source=ocr)且 label 属正文白名单
        (reasons != ["label_not_adoptable"]):
          * 页面 geometry 不可标定(assignment["geometry_unscorable"])或本块
            归属源字符太少/坏码占比过高 → source_unreliable,不参与对账,
            不得把 OCR 判错。
          * 否则做完整对账:missing_prose / prose_mismatch(NED 主指标)/
            ocr_addition / numeric_mismatch。
      - 非正文 label 块(reasons==["label_not_adoptable"]):完全不参与 prose
        对账,不进 block_metrics,不污染页级聚合。

    返回 dict:status("OK"|"SUSPECT"|"UNSCORABLE") / issues(list[dict],每条
    含 code/block_id(可 None)/detail) / metrics(页级聚合) / block_metrics
    (按 block_id)。
    """
    from scripts.pipelines.textbooks.prose_adoption import block_ned as _reverse_ned

    words = list(source_page.get("words") or [])
    n = len(blocks)
    decisions_by_id = {d.block_id: d for d in decisions}

    assignment = assignment or {}
    assignments_by_block: dict[int, list] = assignment.get("assignments", {}) or {}
    unassigned_words: list = list(assignment.get("unassigned") or [])
    page_geometry_unscorable = bool(assignment.get("geometry_unscorable", False))

    # unassigned 源 words 按其原生 block_no(fitz 段落分组)聚簇——同一分组
    # 内多个 words 表示同一区域整段未被任何 OCR 块归属(计划 §6.4)。
    unassigned_groups: dict[int, list] = {}
    for w in unassigned_words:
        unassigned_groups.setdefault(w.block_no, []).append(w)

    issues: list[dict] = []
    block_metrics: dict[int, dict] = {}

    char_pairs: list[tuple[int, int]] = []
    token_pairs: list[tuple[int, int]] = []
    numeric_pairs: list[tuple[int, int]] = []
    addition_pairs: list[tuple[int, int]] = []  # (char_overlap, ocr_char_total)
    ned_pairs: list[tuple[float, int]] = []  # (ned, char_weight)

    prose_content_texts: list[str] = []
    adopted_count = 0
    fallback_prose_count = 0
    source_unreliable_count = 0

    for i, block in enumerate(blocks):
        decision = decisions_by_id.get(i)
        if decision is None:
            continue
        ocr_content = block.get("block_content", "") if isinstance(block, dict) else ""

        if decision.reasons == ["label_not_adoptable"]:
            # 非正文 label(公式/table/image/header/...)——不参与 prose 对账。
            continue

        prose_content_texts.append(ocr_content or "")

        if decision.content_source == "source_text":
            adopted_count += 1
            block_metrics[i] = {
                "content_source": "source_text",
                "block_ned": decision.block_ned,
            }
            if decision.block_ned is not None:
                adopted_chars = sum(
                    1 for ch in (decision.adopted_text or "") if not ch.isspace()
                )
                if adopted_chars > 0:
                    ned_pairs.append((decision.block_ned, adopted_chars))
            continue

        # ---- 回退块(content_source == "ocr")且 label 属正文白名单 ----
        fallback_prose_count += 1
        src_words = list(assignments_by_block.get(i, []))

        if page_geometry_unscorable:
            # 全页几何不可标定(assignment 的权威信号):归属本就不可信,不猜测,
            # 直接判 source_unreliable。
            source_unreliable_count += 1
            block_metrics[i] = {"content_source": "ocr", "source_unreliable": True}
            issues.append(
                {
                    "code": "source_unreliable",
                    "block_id": i,
                    "detail": "页面几何不可标定,该块源归属不可信,不参与对账",
                }
            )
            continue

        raw_source_text = _audit_join_words(src_words)
        src_total, bad_ratio = _audit_source_reliability(src_words)
        if (
            src_total < thresholds.minimum_reliable_chars
            or bad_ratio > thresholds.maximum_bad_char_ratio
        ):
            source_unreliable_count += 1
            block_metrics[i] = {
                "content_source": "ocr",
                "source_unreliable": True,
                "src_char_count": src_total,
                "bad_char_ratio": bad_ratio,
            }
            issues.append(
                {
                    "code": "source_unreliable",
                    "block_id": i,
                    "detail": (
                        f"源字符量={src_total},坏码占比={bad_ratio:.2f},"
                        "该块源不可信,不参与对账"
                    ),
                }
            )
            continue

        source_compare = normalize_prose_for_compare(raw_source_text)
        ocr_compare = normalize_prose_for_compare(ocr_content or "")

        source_chars = [ch for ch in source_compare if not ch.isspace()]
        ocr_chars = [ch for ch in ocr_compare if not ch.isspace()]
        char_overlap = _multiset_overlap(source_chars, ocr_chars)
        char_recall = _recall(char_overlap, len(source_chars))

        source_tokens = source_compare.split()
        ocr_tokens = ocr_compare.split()
        token_overlap = _multiset_overlap(source_tokens, ocr_tokens)
        token_recall = _recall(token_overlap, len(source_tokens))

        ned = _reverse_ned(source_compare, ocr_compare)

        source_numeric = extract_numeric_tokens(raw_source_text)
        ocr_numeric = extract_numeric_tokens(ocr_content or "")
        numeric_recall = None
        if source_numeric:
            numeric_overlap = _multiset_overlap(source_numeric, ocr_numeric)
            numeric_recall = _recall(numeric_overlap, len(source_numeric))
            numeric_pairs.append((numeric_overlap, len(source_numeric)))

        addition_ratio = (
            1.0 - _recall(char_overlap, len(ocr_chars)) if ocr_chars else 0.0
        )

        char_pairs.append((char_overlap, len(source_chars)))
        token_pairs.append((token_overlap, len(source_tokens)))
        addition_pairs.append((char_overlap, len(ocr_chars)))
        if len(source_chars) > 0:
            ned_pairs.append((ned, len(source_chars)))

        block_metrics[i] = {
            "content_source": "ocr",
            "source_unreliable": False,
            "block_ned": ned,
            "char_recall": char_recall,
            "token_recall": token_recall,
            "numeric_token_recall": numeric_recall,
            "addition_ratio": addition_ratio,
            "src_char_count": len(source_chars),
            "ocr_char_count": len(ocr_chars),
        }

        # 多重集召回只是廉价初筛;主指标是逐块 NED——两者各自独立判定,互不
        # 遮蔽(乱序场景下多重集可能满分而 NED 报警,反之亦然)。
        if (
            char_recall < thresholds.minimum_char_recall
            or token_recall < thresholds.minimum_token_recall
        ):
            issues.append(
                {
                    "code": "missing_prose",
                    "block_id": i,
                    "detail": (
                        f"字符召回={char_recall:.2f},token 召回={token_recall:.2f},"
                        "疑似 OCR 漏段"
                    ),
                }
            )
        if ned > thresholds.maximum_block_ned:
            issues.append(
                {
                    "code": "prose_mismatch",
                    "block_id": i,
                    "detail": f"块级 NED={ned:.3f} 超过上限 {thresholds.maximum_block_ned}",
                }
            )
        if addition_ratio > thresholds.maximum_addition_ratio:
            issues.append(
                {
                    "code": "ocr_addition",
                    "block_id": i,
                    "detail": f"OCR 新增比例={addition_ratio:.2f},疑似幻觉/多出内容",
                }
            )
        if (
            numeric_recall is not None
            and numeric_recall < thresholds.minimum_numeric_token_recall
        ):
            issues.append(
                {
                    "code": "numeric_mismatch",
                    "block_id": i,
                    "detail": f"数字 token 召回={numeric_recall:.2f},疑似数字/单位识别错误",
                }
            )

    # ---- 页级:unassigned 源 words 聚簇 → 疑似 OCR 漏切块 ----
    for bn in sorted(unassigned_groups):
        group = unassigned_groups[bn]
        if len(group) >= 2:
            issues.append(
                {
                    "code": "possible_missing_block",
                    "block_id": None,
                    "detail": (
                        f"未归属源 words 聚簇(标记={bn},{len(group)} 个 words),"
                        "疑似 OCR 漏切块"
                    ),
                }
            )

    # ---- 页级:VLM 重复环退化(纯 OCR 输出自检,不需要源文本) ----
    page_prose_text = " ".join(t for t in prose_content_texts if t)
    repetition_score = ngram_repetition_score(page_prose_text)
    if repetition_score > thresholds.maximum_repetition_score:
        issues.append(
            {
                "code": "ocr_degeneration",
                "block_id": None,
                "detail": f"全页正文 n-gram 重复度={repetition_score:.2f},疑似 VLM 重复环退化",
            }
        )

    weighted_addition_precision = _weighted_ratio(addition_pairs)
    metrics = {
        "block_count": n,
        "prose_block_count": adopted_count + fallback_prose_count,
        "adopted_block_count": adopted_count,
        "fallback_block_count": fallback_prose_count,
        "source_unreliable_block_count": source_unreliable_count,
        "weighted_char_recall": _weighted_ratio(char_pairs),
        "weighted_token_recall": _weighted_ratio(token_pairs),
        "weighted_numeric_token_recall": _weighted_ratio(numeric_pairs),
        "weighted_addition_ratio": (
            1.0 - weighted_addition_precision
            if weighted_addition_precision is not None
            else None
        ),
        "weighted_block_ned": (
            sum(v * w for v, w in ned_pairs) / sum(w for _, w in ned_pairs)
            if ned_pairs
            else None
        ),
        "ngram_repetition_score": repetition_score,
    }
    if _single_column_proxy_confirmed(words):
        seq_ratio = _block_sequence_ratio(words, assignments_by_block, block_metrics.keys())
        if seq_ratio is not None:
            metrics["sequence_ratio"] = seq_ratio
            if seq_ratio < thresholds.minimum_single_column_sequence_ratio:
                issues.append(
                    {
                        "code": "sequence_disorder",
                        "block_id": None,
                        "detail": (
                            f"块级源序一致率={seq_ratio:.2f} 低于下限 "
                            f"{thresholds.minimum_single_column_sequence_ratio},疑似块顺序错乱"
                        ),
                    }
                )
    # 多栏页(代理条件不满足)完全跳过:不计算、不比较、不产生 issue(计划
    # §6.4,brief 原规则不变——sequence_ratio 绝不对多栏页作硬门)。

    if page_geometry_unscorable:
        # 页级几何不可标定的独立短路:不依赖"每个受影响块的 decision.reasons
        # 都正确传播了 geometry_unscorable"这一 Task 5 上游不变量——哪怕因为
        # 某种原因一个 issue 都没产生(例如页面全是非正文 label 块),只要
        # assignment 的权威信号为真,页面就必须是 UNSCORABLE。
        status = "UNSCORABLE"
    elif not issues:
        status = "OK"
    elif (
        fallback_prose_count > 0
        and source_unreliable_count == fallback_prose_count
        and all(iss["code"] == "source_unreliable" for iss in issues)
    ):
        status = "UNSCORABLE"
    else:
        status = "SUSPECT"

    return {
        "status": status,
        "issues": issues,
        "metrics": metrics,
        "block_metrics": block_metrics,
    }


# ===========================================================================
# 文档级聚合(计划 §7.1,Task 8):把 Task 3-7 的页级能力(extract_source_page/
# source_health/assign_source_words/adopt_prose_blocks/audit_prose/audit_table/
# header_fingerprint)聚合成单文档审计报告 + 原子落盘 + 独立 CLI。
# ---------------------------------------------------------------------------
# 铁律:本节任何函数都不 import/调用 OCR 引擎(engine.py 等)——只读 PDF(fitz)
# 与已落盘的 OCR res JSON(checkpoint.py 的公开接口),只产出/写审计报告 JSON,
# 绝不改写 Markdown 或任何其它产物。采信落地(把 source_text 写回 Markdown)是
# Task 9 convert.py 编排的职责;decisions_by_page=None 时本模块只是"现场推演"
# 采信结果用于审计分派(dry-run),不据此改写任何东西。
# ===========================================================================

# 公式块 label(计划 §6.5,与 prose_adoption.NEVER_ADOPT_LABELS 中的公式子集
# 一致;独立维护,不导入 prose_adoption 的私有/内部集合)。
_FORMULA_LABELS = frozenset({"display_formula", "inline_formula", "formula_number"})

# 公式/数学符号码点区:与 prose_adoption._MATH_RANGES 的惯例一致,独立维护
# (只读参考,不导入其私有实现——本文件已有 _PUA_RANGES 同惯例先例)。
_FORMULA_MATH_RANGES = (
    (0x2190, 0x21FF),  # 箭头
    (0x2200, 0x22FF),  # 数学运算符
    (0x2A00, 0x2AFF),  # 补充数学运算符
    (0x27C0, 0x27EF),  # 杂项数学符号 A
    (0x2980, 0x29FF),  # 杂项数学符号 B
    (0x1D400, 0x1D7FF),  # 数学字母数字符号
)


def _is_formula_math_char(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _FORMULA_MATH_RANGES)


def _formula_block_audit(
    block_id: int,
    label: str | None,
    words,
    *,
    geometry_unscorable: bool,
    thresholds: AuditThresholds,
    bad_font_count: int,
) -> dict:
    """公式 bbox 内源健康记录(计划 §6.5)。只记录 PUA/控制字符/坏字体计数、
    文本层是否完全无公式字符、源是否不可靠——不做 LaTeX 对比,不给"一致/
    不一致"结论。"""
    total, bad_ratio = _audit_source_reliability(words)
    pua = sum(1 for w in words for ch in w.text if _is_pua(ch))
    control = sum(1 for w in words for ch in w.text if _is_bad_control(ch))
    has_formula_chars = any(_is_formula_math_char(ch) for w in words for ch in w.text)
    source_unreliable = (
        geometry_unscorable
        or total < thresholds.minimum_reliable_chars
        or bad_ratio > thresholds.maximum_bad_char_ratio
    )
    return {
        "block_id": block_id,
        "label": label,
        "source_char_count": total,
        "pua_count": pua,
        "control_char_count": control,
        "bad_font_count": bad_font_count,
        "bad_char_ratio": bad_ratio,
        "text_layer_has_no_formula_chars": not has_formula_chars,
        "source_unreliable_for_formula": source_unreliable,
    }


def _audit_one_page(
    *,
    doc: fitz.Document,
    work_dir: str,
    page_no: int,
    failed_by_page: dict,
    decisions_by_page: dict | None,
    thresholds: AuditThresholds,
    adoption_thresholds,
    adopt_prose_blocks_fn,
    audit_table_fn,
    parse_table_html_fn,
    header_fingerprint_fn,
) -> tuple[dict, str]:
    """单页审计聚合。返回 (页级报告 dict, 页面 status)。

    页面语义三分(计划 Task 8 要求):
      - 失败页(manifest.failed_pages 记录)→ page_failed,UNSCORABLE。
      - 缺 res JSON 但未被记为失败(独立重跑发现的缺页)→ page_incomplete,
        UNSCORABLE。
      - 合法空页(res JSON 存在且 parsing_res_list==[],checkpoint.write_
        empty_page 的哨兵)→ OK,不计入 suspect。
    """
    fitz_page = doc[page_no - 1]

    if not _checkpoint.is_page_done(work_dir, page_no):
        failure = failed_by_page.get(page_no)
        if failure is not None:
            issue = {
                "code": "page_failed",
                "block_id": None,
                "detail": (
                    f"引擎处理失败(kind={failure.get('kind')}):{failure.get('error')}"
                ),
            }
        else:
            issue = {
                "code": "page_incomplete",
                "block_id": None,
                "detail": "过程根缺该页 res JSON,独立重跑无法审计该页",
            }
        page_report = {
            "page": page_no,
            "status": "UNSCORABLE",
            "source_health": {},
            "blocks": [],
            "prose_audit": {},
            "formula_audit": [],
            "table_audit": [],
            "issues": [issue],
        }
        return page_report, "UNSCORABLE"

    ocr_result = _checkpoint.load_page_result(work_dir, page_no)
    ocr_blocks = ocr_result.get("parsing_res_list") or []

    source_page = extract_source_page(fitz_page)
    health = source_health(source_page)

    if not ocr_blocks:
        # 合法空页:与失败/缺页语义不同,视为 OK,不产生 issue。
        page_report = {
            "page": page_no,
            "status": "OK",
            "source_health": health,
            "blocks": [],
            "prose_audit": {"status": "OK", "issues": [], "metrics": {}, "block_metrics": {}},
            "formula_audit": [],
            "table_audit": [],
            "issues": [],
        }
        return page_report, "OK"

    geometry = page_geometry(fitz_page, ocr_result)
    assignment = assign_source_words(source_page["words"], ocr_blocks, geometry)
    geometry_unscorable = bool(assignment.get("geometry_unscorable"))

    if decisions_by_page is not None:
        decisions = decisions_by_page.get(page_no, [])
    else:
        decisions = adopt_prose_blocks_fn(
            ocr_blocks, assignment, source_page, not geometry.unscorable,
            adoption_thresholds,
        )

    prose_result = audit_prose(source_page, ocr_blocks, decisions, assignment, thresholds)

    decisions_by_id = {d.block_id: d for d in decisions}
    assignments_by_block = assignment.get("assignments", {}) or {}
    block_labels = assignment.get("block_labels", {}) or {}

    # 页面字体信息只有页粒度(fitz 不按块暴露字体归属)——诚实复用页级信号,
    # 不伪造块级精度(计划 §6.5"坏字体计数",详见 report 中的说明)。
    bad_font_count = 1 if health.get("suspected_missing_tounicode_cid") else 0

    blocks_out: list[dict] = []
    formula_out: list[dict] = []
    table_out: list[dict] = []
    issues: list[dict] = list(prose_result["issues"])

    for i, block in enumerate(ocr_blocks):
        label = block.get("block_label") if isinstance(block, dict) else None
        if label is None:
            label = block_labels.get(i)

        decision = decisions_by_id.get(i)
        if decision is None:
            # honest 兜底:调用方给了 decisions_by_page 但这一页/这一块没有对应
            # 条目——不得编造 content_source,显式标 no_decision。
            content_source, reasons, block_ned = "ocr", ["no_decision"], None
        else:
            content_source = decision.content_source
            reasons = decision.reasons
            block_ned = decision.block_ned
        blocks_out.append(
            {
                "block_id": i,
                "label": label,
                "content_source": content_source,
                "reasons": reasons,
                "block_ned": block_ned,
            }
        )

        words = assignments_by_block.get(i, [])
        if label in _FORMULA_LABELS:
            formula_entry = _formula_block_audit(
                i, label, words,
                geometry_unscorable=geometry_unscorable,
                thresholds=thresholds,
                bad_font_count=bad_font_count,
            )
            formula_out.append(formula_entry)
            if formula_entry["source_unreliable_for_formula"]:
                issues.append(
                    {
                        "code": "source_unreliable_for_formula",
                        "block_id": i,
                        "detail": "公式块源文本不可靠,仅记录,不作 LaTeX 对比结论",
                    }
                )
        elif label == "table":
            table_result = audit_table_fn(block, words)
            content = block.get("block_content", "") if isinstance(block, dict) else ""
            table = parse_table_html_fn(content)
            table_out.append(
                {
                    "block_id": i,
                    "status": table_result["status"],
                    "structure_issues": table_result["structure_issues"],
                    "content_issues": table_result["content_issues"],
                    "metrics": table_result["metrics"],
                    "header_fingerprint": header_fingerprint_fn(table),
                }
            )
            for si in table_result["structure_issues"]:
                issues.append({"code": si["code"], "block_id": i, "detail": si.get("detail", "")})
            for ci in table_result["content_issues"]:
                issues.append({"code": ci["code"], "block_id": i, "detail": ci.get("detail", "")})
            if table_result["status"] == "table_unscorable":
                issues.append(
                    {
                        "code": "table_unscorable",
                        "block_id": i,
                        "detail": "表格源不可信/为空,不参与数值对账",
                    }
                )

    if prose_result["status"] == "UNSCORABLE":
        status = "UNSCORABLE"
    elif issues:
        status = "SUSPECT"
    else:
        status = "OK"

    page_report = {
        "page": page_no,
        "status": status,
        "source_health": health,
        "blocks": blocks_out,
        "prose_audit": prose_result,
        "formula_audit": formula_out,
        "table_audit": table_out,
        "issues": issues,
    }
    return page_report, status


def audit_document(
    pdf_path: str,
    layout: DocLayout,
    thresholds: AuditThresholds,
    decisions_by_page: dict[int, list] | None,
) -> dict:
    """文档级审计聚合(计划 §7.1,Task 8)。只读 PDF + 已落盘 OCR res JSON,
    绝不调用 OCR 引擎、绝不改写 Markdown/任何产物——独立重跑安全。

    decisions_by_page:
      - None:无 Task 9 记录的采信决策(ocr 模式或本模块独立重跑)——逐页现场
        跑 assign_source_words + adopt_prose_blocks 得 dry-run 决策,仅用于
        审计分派;报告标 adoption_source="dry_run",born_digital_mode="ocr"。
        绝不据此改写任何产物。
      - dict:{page(1-based) -> list[AdoptionDecision]},Task 9 hybrid 主链
        实际落地的决策;报告标 adoption_source="recorded",
        born_digital_mode="hybrid"。audit_document 本身不知道 Task 9 的路由
        状态机,这是由"是否提供了已记录决策"这一本函数唯一可观察的信号决定的
        诚实推断,不是猜测 Task 9 内部状态。
    """
    # prose_adoption/table_audit 在模块顶层 import 本模块(source_audit),
    # 这里若在模块顶层反向 import 会成环——沿用 audit_prose 已有的局部 import
    # 惯例。
    from scripts.pipelines.textbooks.prose_adoption import (
        AdoptionThresholds as _AdoptionThresholds,
        adopt_prose_blocks as _adopt_prose_blocks,
    )
    from scripts.pipelines.textbooks.table_audit import (
        audit_table as _audit_table,
        header_fingerprint as _header_fingerprint,
        parse_table_html as _parse_table_html,
    )

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    dry_run = decisions_by_page is None
    adoption_thresholds = _AdoptionThresholds(
        adoption_min_char_ratio=_DRY_RUN_ADOPTION_MIN_CHAR_RATIO,
        adoption_max_char_ratio=_DRY_RUN_ADOPTION_MAX_CHAR_RATIO,
        adoption_max_ned=_DRY_RUN_ADOPTION_MAX_NED,
    )

    manifest = _checkpoint.load_manifest(layout.work_dir) or {}
    dpi = manifest.get("dpi", _checkpoint.DEFAULT_DPI)
    failed_by_page = {
        f["page"]: f for f in (manifest.get("failed_pages") or [])
    }

    doc = fitz.open(pdf_path)
    try:
        page_count = doc.page_count

        pages_out: list[dict] = []
        prose_blocks_total = 0
        adopted_total = 0
        fallback_total = 0
        fallback_reason_counter: Counter = Counter()
        issue_counter: Counter = Counter()
        suspect_pages: list[int] = []
        scorable_count = 0

        for page_no in range(1, page_count + 1):
            page_report, status = _audit_one_page(
                doc=doc,
                work_dir=layout.work_dir,
                page_no=page_no,
                failed_by_page=failed_by_page,
                decisions_by_page=decisions_by_page,
                thresholds=thresholds,
                adoption_thresholds=adoption_thresholds,
                adopt_prose_blocks_fn=_adopt_prose_blocks,
                audit_table_fn=_audit_table,
                parse_table_html_fn=_parse_table_html,
                header_fingerprint_fn=_header_fingerprint,
            )
            pages_out.append(page_report)

            if status != "UNSCORABLE":
                scorable_count += 1
            if status == "SUSPECT":
                suspect_pages.append(page_no)

            prose_metrics = (page_report.get("prose_audit") or {}).get("metrics") or {}
            prose_blocks_total += prose_metrics.get("prose_block_count", 0) or 0
            adopted_total += prose_metrics.get("adopted_block_count", 0) or 0
            fallback_total += prose_metrics.get("fallback_block_count", 0) or 0

            for block_report in page_report.get("blocks", []):
                if block_report["content_source"] == "ocr" and block_report["reasons"] not in (
                    [], ["label_not_adoptable"], ["no_decision"],
                ):
                    fallback_reason_counter[block_report["reasons"][0]] += 1

            for issue in page_report.get("issues", []):
                issue_counter[issue["code"]] += 1
    finally:
        doc.close()

    if scorable_count == 0:
        doc_status = "UNSCORABLE"
    elif suspect_pages or scorable_count < page_count:
        doc_status = "SUSPECT"
    else:
        doc_status = "OK"

    pdf_fingerprint = {
        "size_bytes": len(pdf_bytes),
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "page_count": page_count,
    }

    return {
        "schema_version": 2,
        "stem": layout.stem,
        "route": "B",
        "born_digital_mode": "ocr" if dry_run else "hybrid",
        "pdf_fingerprint": pdf_fingerprint,
        "ocr_fingerprint": {"dpi": dpi, "page_count": page_count},
        "threshold_profile": THRESHOLD_PROFILE_UNCALIBRATED,
        "adoption_source": "dry_run" if dry_run else "recorded",
        "summary": {
            "status": doc_status,
            "pages": page_count,
            "scorable_pages": scorable_count,
            "suspect_pages": suspect_pages,
            "adoption": {
                "prose_blocks": prose_blocks_total,
                "adopted": adopted_total,
                "fallback_ocr": fallback_total,
                "fallback_reasons": dict(fallback_reason_counter),
            },
            "issue_counts": dict(issue_counter),
        },
        "pages": pages_out,
    }


def write_audit_report(report: dict, path: str) -> None:
    """原子写审计报告(计划 §7.1):先写 <path>.tmp 再 os.replace,进程崩溃/
    断电场景下目标文件要么是旧内容要么是完整新内容,绝不留半截 JSON。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def main(argv: list[str] | None = None) -> int:
    """独立 source audit CLI(计划 Task 8):只读 PDF + 已落盘 OCR res JSON,
    产出文档级审计报告——不调用 OCR 引擎、不改写 Markdown。

    用法:
      python -X utf8 -m scripts.pipelines.textbooks.source_audit \
          --src <PDF> --out <DELIVERABLES> --work-dir <WORK> --stem <STEM>
    """
    ap = argparse.ArgumentParser(
        description="路线 B source audit 独立 CLI(只读已落盘 OCR 结果,产出文档级审计报告)"
    )
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", required=True, help="交付根(DocLayout.deliverables_root)")
    ap.add_argument("--work-dir", required=True, help="过程根(含已落盘的 OCR res JSON)")
    ap.add_argument("--stem", required=True, help="文档 stem")
    ap.add_argument(
        "--dry-run-adoption",
        action="store_true",
        help="仅报告采信推演结果,不落盘改写任何产物(本 CLI 独立重跑恒为此语义)",
    )
    args = ap.parse_args(argv)

    layout = resolve_layout(args.stem, args.out, args.work_dir)
    report = audit_document(
        args.src, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )
    write_audit_report(report, layout.source_audit_path)

    summary = report["summary"]
    print(f"[textbooks] source audit 报告已写: {layout.source_audit_path}")
    print(
        f"  status={summary['status']} pages={summary['pages']} "
        f"scorable={summary['scorable_pages']} suspect_pages={summary['suspect_pages']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
