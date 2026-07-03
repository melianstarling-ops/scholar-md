"""parsing_res_list(PaddleOCR-VL) → Markdown 的确定性重组。"""
from __future__ import annotations

import re
import sys

from scripts.pipelines.textbooks.images import crop_filename, is_visual_block

_NUM_RE = re.compile(r"^\(?([\w.\-]+)\)?$")   # (5.30) / 5.30 → 5.30
_EMPH_RE = re.compile(r"\\underset\{\\cdot\}\{([^{}]*)\}")
_EMPH_WRAP_RE = re.compile(r"\$\s*((?:\\underset\{\\cdot\}\{[^{}]*\}\s*)*)\s*\$")

# KaTeX(Typora 默认渲染器)不认、但在 $$ display 模式里语义冗余的命令。
# 新踩坑命令追加到这里即可;清洗层(sanitize_latex)与 Tier0 lint(selfcheck) 共用此单一清单。
# L-T16:PaddleOCR-VL 把 display 积分输出成 \int\displaylimits_{下}^{上},KaTeX 报红。
KATEX_INCOMPAT_COMMANDS = [r"\displaylimits"]
# (?![a-zA-Z]) 负向边界:删 \displaylimits 不误伤前缀相近的 \displaystyle
_KATEX_SUB = [(re.compile(re.escape(cmd) + r"(?![a-zA-Z])"), "") for cmd in KATEX_INCOMPAT_COMMANDS]

_PASSTHROUGH_UNORDERED_LABELS = {"table", "footnote", "figure_title"}
_KNOWN_NOISE_LABELS = {"header", "number", "header_image"}


def _match_braced(s: str, i: int) -> int:
    r"""s[i] 必须是 '{';返回其配对 '}' 之后一位的下标。花括号不配对时返回 -1。"""
    depth = 0
    while i < len(s):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _collapse_double_subscript(s: str) -> str:
    r"""合并同一节点上相邻的多个下标 _{A}_{B}(…) → _{A\ B(…)}。

    §3 已证正则区分不出"真双下标"和"多层嵌套后接单下标",故走括号配对扫描:
    只有当一个 _{…}(花括号配对完整)后紧跟(允许中间空白)另一个 _{…} 时才合并;
    单下标、下标后接上标(_{A}^{B})、嵌套花括号(\overrightarrow{\mathcal{H}}_{\mathrm{t}})
    都不会误触。合并只为消除 KaTeX 硬报错(double subscript),不猜原书断行。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "_" and i + 1 < n and s[i + 1] == "{":
            end = _match_braced(s, i + 1)
            if end == -1:                       # 花括号不配对,原样放行不改写
                out.append(s[i])
                i += 1
                continue
            inners = [s[i + 2:end - 1]]         # 第一个下标体
            j = end
            while True:
                k = j
                while k < n and s[k] in " \t":  # 跳过下标间空白
                    k += 1
                if not (k < n and s[k] == "_" and k + 1 < n and s[k + 1] == "{"):
                    break
                nxt = _match_braced(s, k + 1)
                if nxt == -1:
                    break
                inners.append(s[k + 2:nxt - 1])
                j = nxt
            if len(inners) > 1:                 # 命中相邻双(多)下标 → 合并
                out.append("_{" + r"\ ".join(inners) + "}")
                i = j
            else:                               # 仅单下标,原样保留
                out.append(s[i:end])
                i = end
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _split_top_level_atop(inner: str) -> list[str]:
    r"""按 brace-depth 0 处的 \atop(排除 \atopwithdelims)切分 inner。"""
    parts: list[str] = []
    depth = last = i = 0
    n = len(inner)
    while i < n:
        c = inner[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
        elif (depth == 0 and inner.startswith(r"\atop", i)
              and not (i + 5 < n and inner[i + 5].isalpha())):
            parts.append(inner[last:i])
            i += 5
            last = i
        else:
            i += 1
    parts.append(inner[last:])
    return parts


def _collapse_chained_atop(s: str) -> str:
    r"""一个花括号 group 里出现 2+ 个 \atop 是 KaTeX 硬报错(only one infix per
    group)。把这种链式 {A\atop B\atop C} 归一成 {\substack{A\\B\\C}}。单个 \atop
    合法,不动;\atopwithdelims 不误伤。仅消红,不补 \text{}(裸文字斜体属软问题)。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "{":
            end = _match_braced(s, i)
            if end == -1:
                out.append(s[i])
                i += 1
                continue
            inner = s[i + 1:end - 1]
            parts = _split_top_level_atop(inner)
            if len(parts) >= 3:                 # 2+ 个 \atop → 3+ 段
                fixed = [_collapse_chained_atop(p).strip() for p in parts]
                out.append("{\\substack{" + "\\\\".join(fixed) + "}}")
            else:                               # 递归进组内(可能有嵌套 group)
                out.append("{" + _collapse_chained_atop(inner) + "}")
            i = end
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


# OCR 把 \cdot d(点积 + 微分 d)粘成未定义控制序列 \cdotd(L-T17,p48 语料实测)。
# (?![a-zA-Z]) 负向边界:只改 \cdotd 本身,不误伤合法 \cdot。
_CDOTD_RE = re.compile(r"\\cdotd(?![a-zA-Z])")


def sanitize_latex(s: str) -> str:
    r"""引擎方言清洗:删冗余命令 + 合并非法相邻双下标 + 链式 atop→substack + \cdotd 拆合。"""
    for pat, repl in _KATEX_SUB:
        s = pat.sub(repl, s)
    s = _CDOTD_RE.sub(r"\\cdot d", s)
    s = _collapse_chained_atop(s)
    s = _collapse_double_subscript(s)
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


def _render_ordered(ordered: list[dict]) -> list[tuple[float, str]]:
    """按 block_order 渲染,含公式吸收,逻辑与改动前完全一致。返回 [(y0, fragment), ...],
    绝不重排——y0 只用于后续归并时判断 extra 该插在哪,不影响这里的相对顺序。"""
    has_paragraph_title = any(b.get("block_label") == "paragraph_title" for b in ordered)
    fragments: list[tuple[float, str]] = []
    i = 0
    while i < len(ordered):
        b = ordered[i]
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        bbox = b.get("block_bbox")
        y0 = bbox[1] if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else 0
        if not content:
            i += 1
            continue
        if label == "paragraph_title":
            fragments.append((y0, f"## {content}"))
        elif label in ("text", "abstract", "reference_content"):
            fragments.append((y0, restore_emphasis_dots(content)))
        elif label == "content":
            fragments.append((y0, _hard_breaks(content)))
        elif label == "algorithm":
            fragments.append((y0, _code_fence(content)))
        elif label == "doc_title":
            if has_paragraph_title:
                # 同页存在 paragraph_title 兄弟块(不一定是章节序号,可能是完整节标题,
                # 实测 p93 样本):经验规则——同页有 paragraph_title 时 doc_title 是被
                # 误标的正文标题,不是封面。100 页语料 4/4 验证成立,非因果机制。
                fragments.append((y0, f"## {content}"))
            else:
                fragments.append((y0, _hard_breaks(content)))
        elif label == "display_formula":
            body = sanitize_latex(_formula_body(content))
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            if nxt and nxt.get("block_label") == "formula_number":
                m = _NUM_RE.match((nxt.get("block_content") or "").strip())
                tag = m.group(1) if m else (nxt.get("block_content") or "").strip()
                fragments.append((y0, f"$$ {body} \\tag{{{tag}}} $$"))
                i += 1                      # 吸收编号块
            else:
                fragments.append((y0, f"$$ {body} $$"))
        elif label == "formula_number":
            fragments.append((y0, content))
        else:
            print(f"[reconstruct] 未知 block_label={label!r},按纯文本兜底落段", file=sys.stderr)
            fragments.append((y0, content))
        i += 1
    return fragments


def _render_unordered(blocks: list[dict], stem: str | None,
                       page: int | None) -> tuple[list[dict], list[dict]]:
    """渲染 block_order is None 的块(spec §2 三层分类)。返回 (extras, warnings)。
    extras 未排序,元素 {"y0": float|None, "seq": int, "fragment": str};seq 是块在
    输入 blocks 里的原始下标,用于稳定排序/页尾组顺序破 tie。"""
    extras: list[dict] = []
    warnings: list[dict] = []
    for seq, b in enumerate(blocks):
        if b.get("block_order") is not None:
            continue
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        bbox = b.get("block_bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            bbox = None
        y0 = bbox[1] if bbox else None
        block_id = b.get("block_id")

        if is_visual_block(label):
            if not bbox:
                warnings.append({"kind": "visual_missing_bbox", "label": label, "page": page,
                                  "block_id": block_id, "sample": content[:40]})
                continue
            if stem is None or page is None:
                raise ValueError(
                    f"reconstruct_markdown: 遇到 {label!r} 块(block_id={block_id})但未提供 "
                    "stem/page,无法生成图片引用")
            fragment = f"![]({stem}.assets/{crop_filename(page, block_id)})"
            if content:
                warnings.append({"kind": "visual_unexpected_content", "label": label,
                                  "page": page, "block_id": block_id, "sample": content[:40]})
                fragment += "\n\n" + restore_emphasis_dots(content)
            extras.append({"y0": y0, "seq": seq, "fragment": fragment})
        elif label in _PASSTHROUGH_UNORDERED_LABELS:
            if not content:
                continue
            extras.append({"y0": y0, "seq": seq, "fragment": restore_emphasis_dots(content)})
        elif label in _KNOWN_NOISE_LABELS:
            continue
        elif content:
            warnings.append({"kind": "unhandled_label", "label": label, "page": page,
                              "block_id": block_id, "sample": content[:40]})
        # else: 都不命中且内容为空 → 静默丢弃(无害)
    return extras, warnings


def _merge(ordered_fragments: list[tuple[float, str]], extras: list[dict]) -> list[str]:
    """两阶段归并(spec §3):ordered 内部顺序绝不重排。对每个有 y0 的 extra,插在第一个
    y0 严格大于它的 ordered 片段之前;等价的共享指针实现要求 extra.y0 < 片段.y0 用严格 `<`
    (spec §3"等价性条款"——用 `<=` 会导致 y0 相等的 tie 排在错误一侧,已有单测锁死)。
    缺 y0 的 extra 归入页尾组,按原始列表顺序(seq)排在最后。"""
    positioned = sorted((e for e in extras if e["y0"] is not None),
                         key=lambda e: (e["y0"], e["seq"]))
    tail = sorted((e for e in extras if e["y0"] is None), key=lambda e: e["seq"])
    parts: list[str] = []
    ei = 0
    for y0, fragment in ordered_fragments:
        while ei < len(positioned) and positioned[ei]["y0"] < y0:
            parts.append(positioned[ei]["fragment"])
            ei += 1
        parts.append(fragment)
    while ei < len(positioned):
        parts.append(positioned[ei]["fragment"])
        ei += 1
    parts.extend(e["fragment"] for e in tail)
    return parts


def reconstruct_markdown(blocks: list[dict], stem: str | None = None,
                         page: int | None = None) -> tuple[str, list[dict]]:
    """按 block_order 排序渲染有序块(不重排);block_order is None 的块按 spec §2 三层分类,
    真内容按 spec §3 两阶段归并按 y0 插入正文流。返回 (markdown, warnings)。"""
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    ordered_fragments = _render_ordered(ordered)
    extras, warnings = _render_unordered(blocks, stem, page)
    parts = _merge(ordered_fragments, extras)
    return "\n\n".join(parts) + "\n", warnings
