"""parsing_res_list(PaddleOCR-VL) → Markdown 的确定性重组。"""
from __future__ import annotations

import re

_NUM_RE = re.compile(r"^\(?([\w.\-]+)\)?$")   # (5.30) / 5.30 → 5.30
_EMPH_RE = re.compile(r"\x5cunderset\{\x5ccdot\}\{([^{}]*)\}")
_EMPH_WRAP_RE = re.compile(r"\$\s*((?:\x5cunderset\{\x5ccdot\}\{[^{}]*\}\s*)*)\s*\$")


def restore_emphasis_dots(text: str) -> str:
    r"""把 \underset{\cdot}{X}…(常被整体裹进 $…$) 还原为纯文字 XYZ。"""
    def _unwrap(m):
        content = m.group(1).strip()
        return _EMPH_RE.sub(r"\1", content)
    text = _EMPH_WRAP_RE.sub(_unwrap, text)     # 先解掉包裹的 $…$
    return _EMPH_RE.sub(r"\1", text)            # 再兜底裸露的


def _formula_body(content: str) -> str:
    """去掉外层 $$ 包裹,取纯公式体。"""
    s = content.strip()
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2].strip()
    return s


def reconstruct_markdown(blocks: list[dict]) -> str:
    """按 block_order 排序、剔除 order=None(页眉页脚页码)、逐块转 Markdown。"""
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        b = ordered[i]
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        if not content:
            i += 1
            continue
        if label == "paragraph_title":
            parts.append(f"## {content}")
        elif label == "text":
            parts.append(restore_emphasis_dots(content))
        elif label == "display_formula":
            body = _formula_body(content)
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            if nxt and nxt.get("block_label") == "formula_number":
                m = _NUM_RE.match((nxt.get("block_content") or "").strip())
                tag = m.group(1) if m else (nxt.get("block_content") or "").strip()
                parts.append(f"$$ {body} \\tag{{{tag}}} $$")
                i += 1                      # 吸收编号块
            else:
                parts.append(f"$$ {body} $$")
        elif label == "formula_number":
            parts.append(content)           # 落单编号,保留不丢
        i += 1
    return "\n\n".join(parts) + "\n"
