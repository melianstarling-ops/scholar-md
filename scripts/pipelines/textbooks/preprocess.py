"""PDF → PNG 预处理(扫描件直喂引擎会崩,必须先栅格化)。"""
from __future__ import annotations

import os

import fitz


def pdf_to_pngs(pdf_path: str, out_dir: str, dpi: int = 200) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        paths = []
        for i in range(doc.page_count):
            pix = doc[i].get_pixmap(dpi=dpi)
            p = os.path.join(out_dir, f"page_{i + 1:04d}.png")
            pix.save(p)
            paths.append(p)
        return paths
    finally:
        doc.close()
