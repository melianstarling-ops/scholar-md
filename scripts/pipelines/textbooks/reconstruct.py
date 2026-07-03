"""parsing_res_list(PaddleOCR-VL) → Markdown 的确定性重组。"""
from __future__ import annotations

import re
import sys

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


def _hard_breaks(content: str) -> str:
    """内部换行有意义(目录/封面多行信息):转 Markdown 硬换行,防止渲染时挤成一段。"""
    return content.replace("\n", "  \n")


def _code_fence(content: str) -> str:
    """围栏长度比 content 内最长的连续反引号串多一个,防止内嵌 ``` 提前截断代码块。"""
    runs = re.findall(r"`+", content)
    fence = "`" * (max((len(r) for r in runs), default=2) + 1)
    return f"{fence}\n{content}\n{fence}"


def reconstruct_markdown(blocks: list[dict]) -> str:
    """按 block_order 排序、剔除 order=None(页眉页脚页码)、逐块转 Markdown。"""
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    has_paragraph_title = any(b.get("block_label") == "paragraph_title" for b in ordered)
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
        elif label in ("text", "abstract", "reference_content"):
            # pending: text 内联 $...$ 公式也可能夹带 KaTeX 不兼容命令,但暂无实例、
            # 且 sanitize 对纯文字的影响未验证,故此路暂不接 sanitize_latex。
            # 待出现 text 块内公式红字实例再评估接入。见 TODO / lessons L-T16。
            parts.append(restore_emphasis_dots(content))
        elif label == "content":
            parts.append(_hard_breaks(content))
        elif label == "algorithm":
            parts.append(_code_fence(content))
        elif label == "doc_title":
            if has_paragraph_title:
                # 同页存在 paragraph_title 兄弟块(不一定是章节序号,可能是完整节标题,
                # 见 L-T? 实测 p93 样本):经验规则——同页有 paragraph_title 时 doc_title
                # 是被误标的正文标题,不是封面。100 页语料 4/4 验证成立,非因果机制。
                parts.append(f"## {content}")
            else:
                # 无兄弟块:封面元信息(书名页/作者页),不当标题
                parts.append(_hard_breaks(content))
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
        else:
            # 兜底:未预料的 label(含未来 PaddleOCR-VL 版本升级新增的),原样落段,防止静默丢失。
            # selfcheck 只验证"内容出现在 md 里",兜底内容必然通过,告警是唯一能暴露给人的信号。
            print(f"[reconstruct] 未知 block_label={label!r},按纯文本兜底落段", file=sys.stderr)
            parts.append(content)
        i += 1
    return "\n\n".join(parts) + "\n"
