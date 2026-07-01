"""parsing_res_list(PaddleOCR-VL) → Markdown 的确定性重组。"""
from __future__ import annotations


def reconstruct_markdown(blocks: list[dict]) -> str:
    """按 block_order 排序、剔除 order=None(页眉页脚页码)、逐块转 Markdown。"""
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    parts: list[str] = []
    for b in ordered:
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        if not content:
            continue
        if label == "paragraph_title":
            parts.append(f"## {content}")
        elif label == "text":
            parts.append(content)
        # display_formula / formula_number 在 Task 5 处理
    return "\n\n".join(parts) + "\n"
