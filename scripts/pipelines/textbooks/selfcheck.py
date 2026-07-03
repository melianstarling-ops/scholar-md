"""Tier0 确定性自检:扫描件无源文本层,改用 block 覆盖率(每个有序块都进了 md)。"""
from __future__ import annotations

import re

from scripts.pipelines.textbooks.reconstruct import KATEX_INCOMPAT_COMMANDS


def katex_incompat_scan(md: str) -> list[str]:
    """Tier0 lint:md 不应残留已知 KaTeX 不兼容命令(与清洗层同源清单)。返回命中命令。"""
    return [c for c in KATEX_INCOMPAT_COMMANDS if c in md]


def _probe(content: str) -> str:
    """取块内容一段稳定的可检子串(去 LaTeX 包裹与空白,取前 12 个非空字符)。"""
    s = re.sub(r"[\s$]", "", content or "")
    return s[:12]


def block_coverage(blocks: list[dict], md: str) -> dict:
    ordered = [b for b in blocks if b.get("block_order") is not None]
    md_flat = re.sub(r"[\s$]", "", md)
    missing = []
    in_md = 0
    skipped_empty = 0
    for b in ordered:
        content = b.get("block_content", "")
        # 对 formula_number 块去括号再探(reconstruct 把编号吸收成 \tag{5.31},无括号)
        if b.get("block_label") == "formula_number":
            content = content.strip().strip("()")
        probe = _probe(content)
        if not probe:
            # block_content 本身为空(OCR 未识别出文字,常见于 text/seal 等 label):
            # 探针恒空,不能算"丢失"(没内容可核对),但也不能悄悄不计数——单独归入
            # skipped_empty,使 total 恒等于 in_md+missing+skipped_empty,不留隐藏数字。
            skipped_empty += 1
            continue
        if probe in md_flat:
            in_md += 1
        else:
            missing.append((b.get("block_content") or "")[:40])
    return {"total": len(ordered), "in_md": in_md, "missing": missing,
            "skipped_empty": skipped_empty}
