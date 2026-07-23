from __future__ import annotations

import re

from ..models import DetectorContext, Finding, Severity, read_text_exact
from ._shared import page_number, page_result_paths, read_json


_FURNITURE = {"header", "footer", "page_number", "number", "seal", "watermark"}
_STRUCTURAL_VISUAL = {"image", "chart", "table", "figure_title", "footer_image"}
_PAGE_SAMPLE_LIMIT = 50
_UNRESOLVED = {"review_content", "unknown_visual"}


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _aside_is_furniture(content: str, block: dict, page_width: float | None) -> bool:
    compact = _normalized(content)
    if not compact:
        return True
    if compact.startswith("licensedcopy:"):
        return True
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", compact):
        return True
    if re.fullmatch(r"\d{1,3}", compact):
        return True
    bbox = block.get("block_bbox")
    at_extreme_edge = (
        page_width is not None
        and isinstance(bbox, (list, tuple))
        and len(bbox) == 4
        and (bbox[2] <= page_width * 0.06 or bbox[0] >= page_width * 0.93)
    )
    # Narrow page-edge text is furniture only with an identity/revision
    # signal. A genuine marginal warning remains unresolved for review.
    identity_signal = (
        "licensedcopy:" in compact
        or "copyright" in compact
        or "iso" in compact
        or "2018" in compact
        or "©" in compact
    )
    return at_extreme_edge and identity_signal


def _classification(block: dict, page_width: float | None = None) -> str:
    content = str(block.get("block_content") or "").strip()
    label = str(block.get("block_label") or "unknown")
    if not content:
        return "empty_visual"
    if label in _FURNITURE:
        return "likely_furniture"
    if label in _STRUCTURAL_VISUAL:
        return "structural_visual"
    if label == "aside_text" and _aside_is_furniture(content, block, page_width):
        return "likely_furniture"
    if label == "vision_footnote":
        return "review_content"
    if label in {"text", "reference_content", "paragraph_title", "table_caption",
                 "figure_caption", "display_formula", "inline_formula"}:
        return "review_content"
    return "unknown_visual"


def detect_unordered_blocks(context: DetectorContext) -> list[Finding]:
    final_md = _normalized(read_text_exact(context.md_path))
    grouped: dict[str, list[dict]] = {}
    for path in page_result_paths(context.work_dir):
        page = page_number(path)
        result = read_json(path)
        page_width = result.get("width")
        if not isinstance(page_width, (int, float)) or page_width <= 0:
            page_width = None
        for index, block in enumerate(result.get("parsing_res_list") or []):
            if not isinstance(block, dict) or block.get("block_order") is not None:
                continue
            classification = _classification(block, page_width)
            # Known furniture/structural visuals are already handled by the
            # reconstruction policy. Meaningful unordered text ceases to be a
            # finding once its normalized content is represented in final MD.
            if classification not in _UNRESOLVED:
                continue
            content = str(block.get("block_content") or "")
            normalized = _normalized(content)
            if normalized and normalized in final_md:
                continue
            grouped.setdefault(classification, []).append({
                "page": page,
                "block_id": block.get("block_id", index),
                "label": block.get("block_label"),
                "content_sample": content[:120],
            })
    findings: list[Finding] = []
    for classification, items in sorted(grouped.items()):
        pages = sorted({item["page"] for item in items})
        findings.append(Finding.create(
            capability="unordered_blocks", kind="unordered_block",
            severity=(Severity.P1 if classification == "review_content" else Severity.P2),
            message="未进入 reading order 的块已按类别汇总，未自动删除",
            target={"classification": classification},
            evidence={"classification": classification, "count": len(items),
                      "page_count": len(pages), "pages": pages[:_PAGE_SAMPLE_LIMIT],
                      "pages_truncated": len(pages) > _PAGE_SAMPLE_LIMIT,
                      "samples": items[:20]},
        ))
    return findings
