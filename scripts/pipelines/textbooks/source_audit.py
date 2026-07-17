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
