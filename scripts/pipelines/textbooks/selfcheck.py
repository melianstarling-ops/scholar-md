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


def detect_column_layout(blocks: list[dict]) -> bool:
    """双栏启发式(spec §5.6):同页 ordered 的 text/display_formula 块两两比较,存在一对
    y 区间重叠比例 > 0.5(相对较矮块的高度)且 x 区间完全分离 → 判定疑似双栏。"""
    candidates = [b for b in blocks
                  if b.get("block_label") in ("text", "display_formula")
                  and b.get("block_order") is not None
                  and isinstance(b.get("block_bbox"), (list, tuple)) and len(b.get("block_bbox")) == 4]
    for i in range(len(candidates)):
        x0a, y0a, x1a, y1a = candidates[i]["block_bbox"]
        for j in range(i + 1, len(candidates)):
            x0b, y0b, x1b, y1b = candidates[j]["block_bbox"]
            overlap = min(y1a, y1b) - max(y0a, y0b)
            if overlap <= 0:
                continue
            shorter = min(y1a - y0a, y1b - y0b)
            if shorter <= 0:
                continue
            if overlap / shorter > 0.5 and (x1a < x0b or x1b < x0a):
                return True
    return False


def aggregate_warnings(warnings: list[dict]) -> dict:
    """reconstruct_markdown 逐页告警汇总成 selfcheck 报告字段(spec §5.5/§5.6):
    unhandled_labels 专指没见过的 label(按 label 分组计数);visual_warnings 是
    "认识的 label 但行为超预期"(缺 bbox / 意外带文本),原样列出不聚合。"""
    unhandled_labels: dict[str, dict] = {}
    visual_warnings: list[dict] = []
    for w in warnings:
        if w["kind"] == "unhandled_label":
            entry = unhandled_labels.setdefault(w["label"], {"count": 0, "sample": w["sample"]})
            entry["count"] += 1
        else:
            visual_warnings.append(w)
    return {"unhandled_labels": unhandled_labels, "visual_warnings": visual_warnings}
