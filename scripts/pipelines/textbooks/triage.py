"""输入分诊:按文本层可信度判 A(无层)/B(优质层)/C(低质层)。"""
from __future__ import annotations

import fitz

COVERAGE_MIN = 50.0     # 每页平均字符 < 此 → 判无层(A)
BADNESS_MAX = 0.25      # 坏度 ≥ 此 → 判低质(C)

# 私用区(PUA)与替换符:文本层坏字形/CID 缺失的典型标志
_PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))


def _is_bad_char(ch: str) -> bool:
    o = ord(ch)
    if ch in ("�", "·"):                # replacement char or PyMuPDF substitute
        return True
    return any(lo <= o <= hi for lo, hi in _PUA_RANGES)


def sample_text_coverage(pdf_path: str, sample: int = 5) -> float:
    """采样均匀分布的若干页,返回每页平均可提取文本字符数。"""
    doc = fitz.open(pdf_path)
    n = doc.page_count
    if n == 0:
        return 0.0
    idxs = sorted({int(n * f) for f in (0.1, 0.3, 0.5, 0.7, 0.9)})[:sample]
    idxs = [min(i, n - 1) for i in idxs]
    total = sum(len(doc[i].get_text().strip()) for i in idxs)
    doc.close()
    return total / len(idxs)


def text_badness(pdf_path: str, sample: int = 5) -> float:
    """坏度分:采样文本中替换符/私用区字符占非空白字符的比例。"""
    doc = fitz.open(pdf_path)
    n = doc.page_count
    if n == 0:
        return 0.0
    idxs = sorted({int(n * f) for f in (0.1, 0.3, 0.5, 0.7, 0.9)})[:sample]
    idxs = [min(i, n - 1) for i in idxs]
    text = "".join(doc[i].get_text() for i in idxs)
    doc.close()
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for c in chars if _is_bad_char(c))
    return bad / len(chars)


def triage(pdf_path: str) -> str:
    """A=无层(OCR) / B=优质层(登记不转) / C=低质层(OCR)。"""
    if sample_text_coverage(pdf_path) < COVERAGE_MIN:
        return "A"
    return "C" if text_badness(pdf_path) >= BADNESS_MAX else "B"
