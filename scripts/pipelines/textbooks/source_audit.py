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

import math
import re
import unicodedata
from dataclasses import dataclass

import fitz

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
