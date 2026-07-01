"""Tier0 确定性自检:扫描件无源文本层,改用 block 覆盖率(每个有序块都进了 md)。"""
from __future__ import annotations

import re


def _probe(content: str) -> str:
    """取块内容一段稳定的可检子串(去 LaTeX 包裹与空白,取前 12 个非空字符)。"""
    s = re.sub(r"[\s$]", "", content or "")
    return s[:12]


def block_coverage(blocks: list[dict], md: str) -> dict:
    ordered = [b for b in blocks if b.get("block_order") is not None]
    md_flat = re.sub(r"[\s$]", "", md)
    missing = []
    in_md = 0
    for b in ordered:
        probe = _probe(b.get("block_content", ""))
        if probe and probe in md_flat:
            in_md += 1
        else:
            missing.append((b.get("block_content") or "")[:40])
    return {"total": len(ordered), "in_md": in_md, "missing": missing}
