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
# L-T18:PaddleOCR-VL 会把 LaTeX 文本声明 \boldmath 放进数学环境;KaTeX 不实现。
KATEX_INCOMPAT_COMMANDS = [r"\displaylimits", r"\boldmath"]
# (?![a-zA-Z]) 负向边界:删 \displaylimits 不误伤前缀相近的 \displaystyle
_KATEX_SUB = [(re.compile(re.escape(cmd) + r"(?![a-zA-Z])"), "") for cmd in KATEX_INCOMPAT_COMMANDS]

_PASSTHROUGH_UNORDERED_LABELS = {"table", "footnote", "figure_title"}
_KNOWN_NOISE_LABELS = {"header", "number", "header_image"}
_LATEX_ENTITY_REPL = {
    "&#x27;": "'",
    "&#39;": "'",
    "&lt;": "<",
    "&gt;": ">",
}
_TAG_RE = re.compile(r"\\tag\{[^{}]*\}")


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


def _collapse_double_superscript(s: str) -> str:
    r"""合并同一节点上相邻的多个上标 ^{A}^{B}(…) → ^{A\ B(…)}。

    与双下标清洗同样只处理 braced 形态,用于消除 KaTeX double superscript 硬报错;
    `x'^{2}` 这类合法 prime+上标不触发。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "^" and i + 1 < n and s[i + 1] == "{":
            end = _match_braced(s, i + 1)
            if end == -1:
                out.append(s[i])
                i += 1
                continue
            inners = [s[i + 2:end - 1]]
            j = end
            while True:
                k = j
                while k < n and s[k] in " \t":
                    k += 1
                if not (k < n and s[k] == "^" and k + 1 < n and s[k + 1] == "{"):
                    break
                nxt = _match_braced(s, k + 1)
                if nxt == -1:
                    break
                inners.append(s[k + 2:nxt - 1])
                j = nxt
            if len(inners) > 1:
                joiner = " " if all(part in (r"\prime", r"\prime\prime") or part.isdigit()
                                    for part in inners) else r"\ "
                out.append("^{" + joiner.join(inners) + "}")
                i = j
            else:
                out.append(s[i:end])
                i = end
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _decode_latex_entities(s: str) -> str:
    for src, dst in _LATEX_ENTITY_REPL.items():
        s = s.replace(src, dst)
    return s


def _strip_latex_tags(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


def _drop_unmatched_closing_braces(s: str) -> str:
    """Drop only top-level closing braces that cannot be paired in math mode."""
    out: list[str] = []
    depth = 0
    for i, c in enumerate(s):
        if c in "{}" and _is_escaped(s, i):
            out.append(c)
        elif c == "{":
            depth += 1
            out.append(c)
        elif c == "}":
            if depth > 0:
                depth -= 1
                out.append(c)
        else:
            out.append(c)
    return "".join(out)


def _drop_unescaped_dollar_tokens(s: str) -> str:
    """Inside an extracted math body, unescaped '$' is never a delimiter."""
    return "".join(c for i, c in enumerate(s) if c != "$" or _is_escaped(s, i))


def _is_escaped(s: str, pos: int) -> bool:
    n = 0
    i = pos - 1
    while i >= 0 and s[i] == "\\":
        n += 1
        i -= 1
    return n % 2 == 1


def _find_display_math_end(s: str, start: int) -> int:
    i = start
    while i < len(s) - 1:
        if s.startswith("$$", i) and not _is_escaped(s, i):
            return i
        i += 1
    return -1


def _find_inline_math_end(s: str, start: int) -> int:
    i = start
    while i < len(s):
        if s[i] == "$" and not _is_escaped(s, i):
            if (i > 0 and s[i - 1] == "$") or (i + 1 < len(s) and s[i + 1] == "$"):
                return -1
            return i
        i += 1
    return -1


def _sanitize_markdown_math_spans(text: str) -> str:
    """对普通 Markdown/HTML 片段里的 $...$/$$...$$ 公式应用同一 LaTeX 清洗。

    表格块来自 OCR 的 raw HTML,但其中仍混有 Markdown math。只清洗 math span 内部,
    避免改写 HTML 标签或正文实体。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "$" and not _is_escaped(text, i):
            display = i + 1 < n and text[i + 1] == "$"
            if not display and ((i > 0 and text[i - 1] == "$") or (i + 1 < n and text[i + 1] == "$")):
                out.append(text[i])
                i += 1
                continue
            delim = "$$" if display else "$"
            start = i + len(delim)
            end = _find_display_math_end(text, start) if display else _find_inline_math_end(text, start)
            if end != -1:
                out.append(delim)
                out.append(sanitize_latex(text[start:end]))
                out.append(delim)
                i = end + len(delim)
                continue
        out.append(text[i])
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
_EMPTY_LEFT_RIGHT_PAIR_RE = re.compile(r"\\left\.((?:(?!\\left|\\right).)*?)\\right\.", re.DOTALL)
_SPLIT_EMPTY_DELIM_RE = re.compile(r"\\right\.(\s*}\s*\\\\\s*&\{\}&\{)\\left\.")
_RIGHT_DOT_BEFORE_CELL_END_RE = re.compile(r"\\right\.(?=\s*})")
_MOMENT_ROW_MISSING_CLOSE_RE = re.compile(
    r"(\\frac\{t\^\{2\}\}\{2!\}f\(t\)d t-\\cdots)}}(\\\\\s+\{\}&\{=\})"
)
_PSEUDO_BMATRIX_RE = re.compile(
    r"\\mathrm\{~\\bmatrix\{~(?P<body>.*?)\\mathbf\{~\}\}\}"
    r"\s*\\\\\s*\\end\{bmatrix\}(?P<script>_\{[^{}]*\})",
    re.DOTALL,
)
_INTEGRAL_FRAC_RUN_RE = re.compile(
    r"\\frac\{(?P<body>(?:\\displaystyle\\int)+)\}(?=\\[A-Za-z])"
)
_APRIME_INTEGRAL_SPLIT_RE = re.compile(
    r"a\^\{\\prime\}\\\\\s*\\int\\limits_\{a\}"
    r"\^\{\\overrightarrow\{\\hat\{E\}\}\}_\{\\mathrm\{t\}\}\^\{\\mathrm\{inc\}\}"
)
_SPACING_SCRIPT_RE = re.compile(r"(\\(?:quad|qquad))_(\{[^{}]*\})")
_MISSING_ENDARRAY_BEFORE_RIGHT_RE = re.compile(
    r"(\\left\[\\begin\{array\}\{[^{}]*\}(?:(?!\\end\{array\}).)*?)(\\right\](?=\}?_\{))",
    re.DOTALL,
)
_ARRAY_RE = re.compile(
    r"\\begin\{array\}\{(?P<spec>[^{}]*)\}(?P<body>(?:(?!\\begin\{array\}).)*?)\\end\{array\}",
    re.DOTALL,
)


def _drop_empty_left_right_delimiters(s: str) -> str:
    """Remove OCR-only invisible delimiter pairs that make KaTeX lose balance."""
    had_split = _SPLIT_EMPTY_DELIM_RE.search(s) is not None
    if had_split:
        s = _SPLIT_EMPTY_DELIM_RE.sub(r"\1", s)
        s = _RIGHT_DOT_BEFORE_CELL_END_RE.sub("", s)
        # Dynamic delimiters cannot cross the array-cell group boundary that
        # the OCR row split introduced. Plain delimiters keep visible content.
        s = s.replace(r"\left[", "[")
        s = s.replace(r"\right]", "]")
        s = s.replace(r"\left\{", r"\{")
        s = s.replace(r"\right\}", r"\}")
    prev = None
    while prev != s:
        prev = s
        s = _EMPTY_LEFT_RIGHT_PAIR_RE.sub(r"\1", s)
    return s


def _repair_pseudo_bmatrix(s: str) -> str:
    r"""Repair OCR-only ``\bmatrix{...}\end{bmatrix}`` pseudo syntax."""
    return _PSEUDO_BMATRIX_RE.sub(
        lambda m: r"\left[" + m.group("body") + r"\right]" + m.group("script"),
        s,
    )


def _unwrap_malformed_integral_frac_runs(s: str) -> str:
    """Paddle sometimes emits one-argument ``\frac{integral-run}`` chunks."""
    return _INTEGRAL_FRAC_RUN_RE.sub(lambda m: "{" + m.group("body") + "}", s)


def _repair_orphan_aprime_integral_limits(s: str) -> str:
    """Repair the observed split ``a'`` upper-limit row before an E-field integral."""
    return _APRIME_INTEGRAL_SPLIT_RE.sub(
        r"\\int\\limits_{a}^{a^{\\prime}}\\overrightarrow{\\hat{E}}_{\\mathrm{t}}^{\\mathrm{inc}}",
        s,
    )


def _fix_spacing_command_scripts(s: str) -> str:
    """Give scripts attached to spacing commands a legal empty-group base."""
    return _SPACING_SCRIPT_RE.sub(r"\1{}_\2", s)


def _insert_missing_endarray_before_right(s: str) -> str:
    r"""OCR sometimes closes ``\left[`` before closing the nested array."""
    prev = None
    while prev != s:
        prev = s
        s = _MISSING_ENDARRAY_BEFORE_RIGHT_RE.sub(r"\1\\end{array}\2", s)
    return s


def _expand_array_colspecs(s: str) -> str:
    """Pad underspecified simple array column specs to the widest observed row."""
    def _fix(m: re.Match) -> str:
        spec = m.group("spec")
        body = m.group("body")
        align = [c for c in spec if c in "lcr"]
        if not align:
            return m.group(0)
        rows = re.split(r"(?<!\\)\\\\", body)
        needed = max((row.count("&") + 1 for row in rows), default=len(align))
        if needed <= len(align):
            return m.group(0)
        fixed = "".join(align) + align[-1] * (needed - len(align))
        return r"\begin{array}{" + fixed + "}" + body + r"\end{array}"

    prev = None
    while prev != s:
        prev = s
        s = _ARRAY_RE.sub(_fix, s)
    return s


def _downgrade_split_invisible_delimiters(s: str) -> str:
    """Downgrade row-split invisible delimiters to fixed visible delimiters."""
    if r"\left" not in s and r"\right" not in s:
        return s

    def _delim_end(pos: int) -> tuple[int, str]:
        if pos >= len(s):
            return pos, ""
        if s[pos] == "\\" and pos + 1 < len(s):
            return pos + 2, s[pos:pos + 2]
        return pos + 1, s[pos]

    repl: list[tuple[int, int, str]] = []
    stack: list[tuple[int, int, str]] = []
    i = 0
    while i < len(s):
        if s.startswith(r"\left", i):
            end, delim = _delim_end(i + len(r"\left"))
            if delim == ".":
                repl.append((i, end, ""))
            else:
                stack.append((i, end, delim))
            i = end
            continue
        if s.startswith(r"\right", i):
            end, delim = _delim_end(i + len(r"\right"))
            if stack:
                stack.pop()
            else:
                repl.append((i, end, "" if delim == "." else delim))
            i = end
            continue
        i += 1

    for start, end, delim in stack:
        repl.append((start, end, "" if delim == "." else delim))
    if not repl:
        return s
    repl.sort()
    out: list[str] = []
    last = 0
    for start, end, text in repl:
        out.append(s[last:start])
        out.append(text)
        last = end
    out.append(s[last:])
    return "".join(out)


def _close_known_moment_row(s: str) -> str:
    """Repair the p477 moment-expansion row after empty delimiter cleanup."""
    return _MOMENT_ROW_MISSING_CLOSE_RE.sub(r"\1}}}\2", s)


# CJK 教材公式里混进的裸露"文字类" Unicode 字符——KaTeX 严格模式对这些字符会报
# unicodeTextInMathMode / unknownSymbol 警告(不是硬错,但 debug 卡片里成百上千条噪音)。
# 区段选取只覆盖"明显是文字/标点/符号,而非数学记号"的范围:
#   　-〿  CJK 符号与标点(。、（）「」等全角标点)
#   㐀-䶿  CJK 扩展 A
#   一-鿿  CJK 统一表意文字(汉字本体)
#   豈-﫿  CJK 兼容表意文字
#   ︰-﹏  CJK 兼容形式
#   ＀-￯  全角/半角形式(全角标点、全角字母数字等)
#   ①-⓿  带圈字母数字
#   ■-◿  几何图形(○● 等实心/空心记号,教材里常用作注号)
# 希腊字母(α β)、数学算符(∑ ∫ × ≤ 等)不在这些区段内,天然不受影响。
_TEXTISH = (
    "　-〿"
    "㐀-䶿"
    "一-鿿"
    "豈-﫿"
    "︰-﹏"
    "＀-￯"
    "①-⓿"
    "■-◿"
)
_TEXTISH_RUN_RE = re.compile(f"[{_TEXTISH}]+")
_TEXTISH_WRAP_SAFE_PREFIXES = ("\\text{", "\\mathrm{", "\\mathbf{")


def wrap_cjk_in_text(latex: str) -> str:
    r"""把数学模式里连续的"文字类" Unicode 字符段包进 \text{...}。

    只在这些字符已经身处 \text{}/\mathrm{}/\mathbf{} 内时跳过(幂等,不双包);
    其余情况一律原地包裹,不改变字符本身、不改变周围的数学结构。纯 ASCII/希腊
    字母/数学算符不落在 _TEXTISH 区段内,逐字节不变地穿过本函数。"""
    out: list[str] = []
    i, n = 0, len(latex)
    while i < n:
        m = _TEXTISH_RUN_RE.match(latex, i)
        if m:
            run = m.group(0)
            prefix = latex[:i].rstrip()
            if prefix.endswith(_TEXTISH_WRAP_SAFE_PREFIXES):
                out.append(run)
            else:
                out.append("\\text{" + run + "}")
            i = m.end()
        else:
            out.append(latex[i])
            i += 1
    return "".join(out)


def sanitize_latex(s: str) -> str:
    r"""引擎方言清洗:删冗余命令 + 合并非法相邻双脚本 + 链式 atop→substack + \cdotd 拆合。"""
    s = _decode_latex_entities(s)
    s = _drop_unescaped_dollar_tokens(s)
    s = _repair_pseudo_bmatrix(s)
    s = _unwrap_malformed_integral_frac_runs(s)
    s = _repair_orphan_aprime_integral_limits(s)
    s = _fix_spacing_command_scripts(s)
    for pat, repl in _KATEX_SUB:
        s = pat.sub(repl, s)
    s = _CDOTD_RE.sub(r"\\cdot d", s)
    s = _collapse_chained_atop(s)
    s = _collapse_double_subscript(s)
    s = _collapse_double_superscript(s)
    s = _insert_missing_endarray_before_right(s)
    s = _expand_array_colspecs(s)
    s = _drop_empty_left_right_delimiters(s)
    s = _downgrade_split_invisible_delimiters(s)
    s = _close_known_moment_row(s)
    s = _drop_unmatched_closing_braces(s)
    s = wrap_cjk_in_text(s)
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


def _render_ordered(ordered: list[dict]) -> list[dict]:
    """按 block_order 渲染,含公式吸收。返回 [{"y0","bids","md"}, ...],绝不重排——
    y0 只用于后续归并时判断 extra 该插在哪;bids 是该片段归属的块 id(公式吸收编号时
    含两个),供 debug_view 左右双向联动。md 内容与改动前逐字节一致。"""
    has_paragraph_title = any(b.get("block_label") == "paragraph_title" for b in ordered)
    fragments: list[dict] = []
    i = 0

    def emit(y0, bids, md):
        fragments.append({"y0": y0, "bids": bids, "md": md})

    while i < len(ordered):
        b = ordered[i]
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        bbox = b.get("block_bbox")
        y0 = bbox[1] if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else 0
        bid = b.get("block_id")
        if not content:
            i += 1
            continue
        if label == "paragraph_title":
            emit(y0, [bid], f"## {content}")
        elif label in ("text", "abstract", "reference_content"):
            emit(y0, [bid], _sanitize_markdown_math_spans(restore_emphasis_dots(content)))
        elif label == "content":
            emit(y0, [bid], _hard_breaks(content))
        elif label == "algorithm":
            emit(y0, [bid], _code_fence(content))
        elif label == "doc_title":
            if has_paragraph_title:
                # 同页存在 paragraph_title 兄弟块(不一定是章节序号,可能是完整节标题,
                # 实测 p93 样本):经验规则——同页有 paragraph_title 时 doc_title 是被
                # 误标的正文标题,不是封面。100 页语料 4/4 验证成立,非因果机制。
                emit(y0, [bid], f"## {content}")
            else:
                emit(y0, [bid], _hard_breaks(content))
        elif label == "display_formula":
            body = sanitize_latex(_formula_body(content))
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            if nxt and nxt.get("block_label") == "formula_number":
                m = _NUM_RE.match((nxt.get("block_content") or "").strip())
                tag = m.group(1) if m else (nxt.get("block_content") or "").strip()
                body = _strip_latex_tags(body)
                emit(y0, [bid, nxt.get("block_id")], f"$$ {body} \\tag{{{tag}}} $$")
                i += 1                      # 吸收编号块
            else:
                emit(y0, [bid], f"$$ {body} $$")
        elif label == "formula_number":
            emit(y0, [bid], content)
        else:
            print(f"[reconstruct] 未知 block_label={label!r},按纯文本兜底落段", file=sys.stderr)
            emit(y0, [bid], content)
        i += 1
    return fragments


def _render_unordered(blocks: list[dict], stem: str | None,
                       page: int | None) -> tuple[list[dict], list[dict]]:
    """渲染 block_order is None 的块(spec §2 三层分类)。返回 (extras, warnings)。
    extras 未排序,元素 {"y0": float|None, "seq": int, "bids": list, "md": str};seq 是块在
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
            extras.append({"y0": y0, "seq": seq, "bids": [block_id], "md": fragment})
        elif label in _PASSTHROUGH_UNORDERED_LABELS:
            if not content:
                continue
            extras.append({"y0": y0, "seq": seq, "bids": [block_id],
                           "md": _sanitize_markdown_math_spans(restore_emphasis_dots(content))})
        elif label in _KNOWN_NOISE_LABELS:
            continue
        elif content:
            warnings.append({"kind": "unhandled_label", "label": label, "page": page,
                              "block_id": block_id, "sample": content[:40]})
        # else: 都不命中且内容为空 → 静默丢弃(无害)
    return extras, warnings


def _merge(ordered_fragments: list[dict], extras: list[dict]) -> list[dict]:
    """两阶段归并(spec §3):ordered 内部顺序绝不重排。对每个有 y0 的 extra,插在第一个
    y0 严格大于它的 ordered 片段之前;等价的共享指针实现要求 extra.y0 < 片段.y0 用严格 `<`
    (spec §3"等价性条款"——用 `<=` 会导致 y0 相等的 tie 排在错误一侧,已有单测锁死)。
    缺 y0 的 extra 归入页尾组,按原始列表顺序(seq)排在最后。返回 [{"bids","md"}, ...]。"""
    positioned = sorted((e for e in extras if e["y0"] is not None),
                         key=lambda e: (e["y0"], e["seq"]))
    tail = sorted((e for e in extras if e["y0"] is None), key=lambda e: e["seq"])
    parts: list[dict] = []
    ei = 0
    for frag in ordered_fragments:
        while ei < len(positioned) and positioned[ei]["y0"] < frag["y0"]:
            parts.append({"bids": positioned[ei]["bids"], "md": positioned[ei]["md"]})
            ei += 1
        parts.append({"bids": frag["bids"], "md": frag["md"]})
    while ei < len(positioned):
        parts.append({"bids": positioned[ei]["bids"], "md": positioned[ei]["md"]})
        ei += 1
    parts.extend({"bids": e["bids"], "md": e["md"]} for e in tail)
    return parts


def reconstruct_fragments(blocks: list[dict], stem: str | None = None,
                          page: int | None = None) -> tuple[list[dict], list[dict]]:
    """与 reconstruct_markdown 同逻辑,但返回带块归属的片段列表 [{"bids": [id,...],
    "md": str}, ...](最终归并顺序)+ warnings。debug_view 用它做左右双向联动;
    reconstruct_markdown 即在此基础上 join 出整段 md,输出逐字节等价。"""
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    ordered_fragments = _render_ordered(ordered)
    extras, warnings = _render_unordered(blocks, stem, page)
    return _merge(ordered_fragments, extras), warnings


def reconstruct_markdown(blocks: list[dict], stem: str | None = None,
                         page: int | None = None) -> tuple[str, list[dict]]:
    """按 block_order 排序渲染有序块(不重排);block_order is None 的块按 spec §2 三层分类,
    真内容按 spec §3 两阶段归并按 y0 插入正文流。返回 (markdown, warnings)。"""
    parts, warnings = reconstruct_fragments(blocks, stem, page)
    return "\n\n".join(p["md"] for p in parts) + "\n", warnings
