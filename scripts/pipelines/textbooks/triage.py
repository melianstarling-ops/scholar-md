"""输入分诊:按文本层可信度判 A(无层)/B(优质层)/C(低质层)。"""
from __future__ import annotations

import fitz


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
