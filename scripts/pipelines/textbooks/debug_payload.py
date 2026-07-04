"""逐页 debug payload(纯数据,JSON 可序列化):供 debug_view 塞进 HTML 模板。

不碰浏览器、不做渲染——只把一页的 res.json 加工成:块叠框(按 label 分色)、
逐页 reconstruct 的 md(过修复后的 sanitize)、该页 selfcheck 信号、可选页图 base64
与该页 KaTeX 报错。渲染与判红全在浏览器 JS(与 scan_katex_errors.mjs 同版本 katex)。
"""
from __future__ import annotations

from scripts.pipelines.textbooks.images import is_visual_block
from scripts.pipelines.textbooks.reconstruct import reconstruct_fragments
from scripts.pipelines.textbooks.selfcheck import detect_column_layout, scan_formula_suspicions

# 块 label → 叠框颜色。红(#ef4444)留给"渲染报错"高亮,不用于任何 label。
LABEL_COLORS: dict[str, str] = {
    "paragraph_title": "#a855f7",   # 紫
    "doc_title": "#9333ea",
    "text": "#3b82f6",              # 蓝
    "abstract": "#3b82f6",
    "reference_content": "#3b82f6",
    "content": "#60a5fa",
    "display_formula": "#14b8a6",   # 青
    "formula_number": "#5eead4",
    "algorithm": "#b45309",         # 棕
    "image": "#f97316",             # 橙
    "chart": "#fb923c",
    "table": "#22c55e",             # 绿
    "footnote": "#4ade80",
    "figure_title": "#16a34a",
    "header": "#9ca3af",            # 灰(噪声)
    "number": "#9ca3af",
    "header_image": "#9ca3af",
}
_UNKNOWN_COLOR = "#ec4899"          # 品红:没见过的 label,醒目提示
_NOISE_LABELS = {"header", "number", "header_image"}


def label_color(label: str) -> str:
    return LABEL_COLORS.get(label, _UNKNOWN_COLOR)


def _valid_bbox(b: dict):
    bbox = b.get("block_bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return list(bbox)
    return None


def _overlay(b: dict) -> dict | None:
    bbox = _valid_bbox(b)
    if bbox is None:
        return None
    label = b.get("block_label", "")
    return {
        "block_id": b.get("block_id"),
        "label": label,
        "bbox": bbox,
        "order": b.get("block_order"),
        "is_visual": is_visual_block(label),
        "is_noise": label in _NOISE_LABELS,
        "color": label_color(label),
        "content_head": (b.get("block_content") or "")[:120],
    }


def build_page_signals(blocks: list[dict], warnings: list[dict]) -> dict:
    """该页 selfcheck 信号:双栏嫌疑 + reconstruct 逐页告警(未知 label / visual 异常)。"""
    unhandled = sorted({w["label"] for w in warnings if w["kind"] == "unhandled_label"})
    visual = [w for w in warnings if w["kind"] != "unhandled_label"]
    return {
        "column_suspected": detect_column_layout(blocks),
        "unhandled_labels": unhandled,
        "visual_warnings": visual,
    }


def build_page_payload(res: dict, page: int, stem: str,
                       image_b64: str | None = None,
                       page_errors: list[dict] | None = None) -> dict:
    """把一页 res.json 加工成 HTML 模板所需的 payload dict。frags 是带块归属的
    md 片段列表(供左右双向联动);md 是其 join(供报错索引/整页渲染)。"""
    blocks = res.get("parsing_res_list", [])
    frags, warnings = reconstruct_fragments(blocks, stem=stem, page=page)
    md = "\n\n".join(f["md"] for f in frags) + "\n"
    overlays = [o for o in (_overlay(b) for b in blocks) if o is not None]
    # 疑似漏识别(裸大算符):逐片段标注,供 debug 视图橙色标出并聚合到页级
    suspicions: list[dict] = []
    for f in frags:
        ops = [s["op"] for s in scan_formula_suspicions(f["md"])]
        f["suspicions"] = ops
        for op in ops:
            suspicions.append({"op": op, "bids": f["bids"]})
    return {
        "page": page,
        "width": res.get("width"),
        "height": res.get("height"),
        "image_b64": image_b64,
        "blocks": overlays,
        "md": md,
        "frags": frags,
        "signals": build_page_signals(blocks, warnings),
        "render_errors": page_errors or [],
        "suspicions": suspicions,
    }
