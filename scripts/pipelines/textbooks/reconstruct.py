"""parsing_res_list(PaddleOCR-VL) → Markdown 的确定性重组。"""
from __future__ import annotations

import re

_NUM_RE = re.compile(r"^\(?([\w.\-]+)\)?$")   # (5.30) / 5.30 → 5.30
_EMPH_RE = re.compile(r"\\underset\{\\cdot\}\{([^{}]*)\}")
_EMPH_WRAP_RE = re.compile(r"\$\s*((?:\\underset\{\\cdot\}\{[^{}]*\}\s*)*)\s*\$")

# KaTeX(Typora 默认渲染器)不认、但在 $$ display 模式里语义冗余的命令。
# 新踩坑命令追加到这里即可;清洗层(sanitize_latex)与 Tier0 lint(selfcheck) 共用此单一清单。
# L-T16:PaddleOCR-VL 把 display 积分输出成 \int\displaylimits_{下}^{上},KaTeX 报红。
KATEX_INCOMPAT_COMMANDS = [r"\displaylimits"]
# (?![a-zA-Z]) 负向边界:删 \displaylimits 不误伤前缀相近的 \displaystyle
_KATEX_SUB = [(re.compile(re.escape(cmd) + r"(?![a-zA-Z])"), "") for cmd in KATEX_INCOMPAT_COMMANDS]


def sanitize_latex(s: str) -> str:
    r"""删除 KaTeX 不支持、但语义冗余的 LaTeX 命令(引擎方言清洗)。"""
    for pat, repl in _KATEX_SUB:
        s = pat.sub(repl, s)
    return s


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
            # pending: text 内联 $...$ 公式也可能夹带 KaTeX 不兼容命令,但暂无实例、
            # 且 sanitize 对纯文字的影响未验证,故此路暂不接 sanitize_latex。
            # 待出现 text 块内公式红字实例再评估接入。见 TODO / lessons L-T16。
            parts.append(restore_emphasis_dots(content))
        elif label == "display_formula":
            body = sanitize_latex(_formula_body(content))
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
