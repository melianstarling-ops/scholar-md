"""parsing_res_list(PaddleOCR-VL) → Markdown 的确定性重组。"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser

from scripts.pipelines.textbooks.images import crop_filename, is_visual_block
from scripts.pipelines.textbooks.table_audit import (
    ParsedTable,
    lint_table_structure,
    parse_table_html,
)

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

# KaTeX 不认的命令 → 语义等价替换(区别于 _KATEX_SUB 的纯删除)。
# 全部来自 OCR/上游产物在真实教材里反复出现的确定性坏形态,映射均由真实语料上下文核实:
#   \upmu       upgreek 包的直立 μ(单位前缀 μA/μV/μF/μH),KaTeX 无 → \mu
#   \pit        OCR 把 "\pi t"(频率 1/\pi t_r)黏一起;字母边界排除真命令 \pitchfork
#   \[          数学内容已被 $$ 包裹,残留的 display 定界符 \[ 恒非法 → 删
# 字母边界 (?![a-zA-Z]) 防止误伤更长的合法命令。替换串内 \\ 均为字面反斜杠。
# 注:曾出现的 \omegaarrow/\sigmaarrow 并非 OCR 坏形态,而是合法 \omega\leftarrow /
# \sigma\rightarrow 被 _downgrade_split_invisible_delimiters 啃坏的产物,已在该函数根治,
# 不在此处治标。
_KATEX_CMD_MAP = [
    (re.compile(r"\\upmu(?![a-zA-Z])"), r"\\mu"),
    (re.compile(r"\\pit(?![a-zA-Z])"), r"\\pi t"),
    (re.compile(r"\\\[(?![a-zA-Z])"), ""),
]
_ENSUREMATH_RE = re.compile(r"\\ensuremath\s*\{")

_PASSTHROUGH_UNORDERED_LABELS = {"footnote", "figure_title"}
_KNOWN_NOISE_LABELS = {"header", "number", "header_image"}
_LATEX_ENTITY_REPL = {
    "&#x27;": "'",
    "&#39;": "'",
    "&lt;": "<",
    "&gt;": ">",
}
_TAG_RE = re.compile(r"\\tag\{[^{}]*\}")

# C3(Task C 顺手项):PaddleOCR-VL 偶发把上下标包成冗余双花括号 _{{X}}/^{{X}}。
# 锚定在 _/^ 之后,且要求内容 X 不含花括号(单层):\frac{{a}}{b} 的 frac 参数前面
# 不是 _/^,天然不受影响;{\alpha} 只有单层花括号,也不匹配。绝不做全局 {{ → { 盲替换。
_DOUBLE_BRACED_SCRIPT_RE = re.compile(r"([_^])\{\{([^{}]*)\}\}")


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


def _unwrap_ensuremath(s: str) -> str:
    r"""\ensuremath{X} → X。已在数学模式,该包裹恒冗余;靠括号配对(非贪婪正则)解包,
    保留内容原样。花括号不配对时放弃改写,绝不破坏(最坏 = 原样)。"""
    while True:
        m = _ENSUREMATH_RE.search(s)
        if not m:
            return s
        brace = m.end() - 1                 # 指向 '{'
        end = _match_braced(s, brace)
        if end == -1:                       # 花括号不配对 → 放弃,不破坏
            return s
        s = s[:m.start()] + s[brace + 1:end - 1] + s[end:]


def _read_script_body(s: str, i: int, n: int) -> tuple[str, int]:
    r"""s[i] 是 _ 或 ^;读其紧跟的脚本体,返回 (体内容不含花括号, 体后位置)。
    braced {A}→A;命令 \name→\name;单字符→该字符。花括号不配对/越界返回 ("", -1)。"""
    j = i + 1
    if j >= n:
        return "", -1
    if s[j] == "{":
        end = _match_braced(s, j)
        if end == -1:
            return "", -1
        return s[j + 1:end - 1], end
    if s[j] == "\\":
        cmd, k = _read_command(s, j)
        return cmd, k
    return s[j], j + 1


def _collapse_adjacent_scripts(s: str, mark: str) -> str:
    r"""合并同一节点上相邻的多个下标/上标(mark 为 '_' 或 '^')→ {A\ B}。

    首个及后续脚本体均可为 braced {A} 或裸 token(单字符/命令,如 V_g_{\max} 的 _g);
    只有紧邻(允许中间空白)的同类脚本才合并,单脚本、下标后接上标都不误触。合并只为
    消除 KaTeX double subscript/superscript 硬报错,不猜原书断行。上标全为 prime/数字时
    用空格拼接(还原 x'^2 类),否则用 \ 。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == mark:
            first, end = _read_script_body(s, i, n)
            if end == -1:                       # 花括号不配对/越界,原样放行
                out.append(s[i])
                i += 1
                continue
            inners = [first]
            j = end
            while True:
                k = j
                while k < n and s[k] in " \t":  # 跳过脚本间空白
                    k += 1
                if not (k < n and s[k] == mark):
                    break
                body, nxt = _read_script_body(s, k, n)
                if nxt == -1:
                    break
                inners.append(body)
                j = nxt
            if len(inners) > 1:                 # 命中相邻双(多)脚本 → 合并
                if mark == "^" and all(p in (r"\prime", r"\prime\prime") or p.isdigit()
                                       for p in inners):
                    joiner = " "
                else:
                    joiner = r"\ "
                out.append(mark + "{" + joiner.join(inners) + "}")
                i = j
            else:                               # 仅单脚本,原样保留
                out.append(s[i:end])
                i = end
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _collapse_double_subscript(s: str) -> str:
    return _collapse_adjacent_scripts(s, "_")


def _collapse_double_superscript(s: str) -> str:
    return _collapse_adjacent_scripts(s, "^")


def _decode_latex_entities(s: str) -> str:
    for src, dst in _LATEX_ENTITY_REPL.items():
        s = s.replace(src, dst)
    return s


def _strip_latex_tags(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


def _normalize_double_braced_scripts(s: str) -> str:
    r"""上下标双花括号归一(C3):仅 _{{X}} → _{X} 与 ^{{X}} → ^{X},且仅当 X 不含
    花括号(单层内容)。范围严格限定在 _/^ 紧跟的双花括号——\frac{{a}}{b} 的 frac
    参数前面不是 _/^,不受影响;{\alpha} 只有单层花括号,不匹配双花括号模式。"""
    return _DOUBLE_BRACED_SCRIPT_RE.sub(r"\1{\2}", s)


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
                body = text[start:end]
                if not display:
                    # PaddleOCR-VL 常吐出 `$ X $`(定界符内侧带空格);VSCode 预览/
                    # pandoc 等主流渲染器要求行内 $ 紧贴内容,否则按字面文本显示
                    # (KaTeX 门只验编译不验定界符,漏检)。只去内侧首尾空白,内容
                    # 中间的空格(词间距)不动;display $$ 无此渲染规则问题,不改。
                    stripped = body.strip()
                    # 内体全是空白(如 `$   $`)不是真公式:strip 后会拼出 "$" + ""
                    # + "$" = "$$",被后续渲染器当成 display 定界符起点,可能一路
                    # 吞掉后文直到下一个 $$(比误伤货币 $ 更危险)。保持原样不动。
                    body = stripped if stripped else body
                out.append(sanitize_latex(body))
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
        # 字母边界:\left / \right 作定界符时后接定界符(( [ . | 或 \langle 等命令),绝不
        # 后接字母。后接字母(\leftarrow / \rightarrow / \leftrightarrow / \rightharpoonup …)
        # 是合法箭头/谐波命令,若当定界符会把 \lefta… 啃成 \…arrow(实测 \omega\leftarrow
        # 被啃成未定义的 \omegaarrow),故跳过不处理。
        if s.startswith(r"\left", i) and not s[i + len(r"\left"):i + len(r"\left") + 1].isalpha():
            end, delim = _delim_end(i + len(r"\left"))
            if delim == ".":
                repl.append((i, end, ""))
            else:
                stack.append((i, end, delim))
            i = end
            continue
        if s.startswith(r"\right", i) and not s[i + len(r"\right"):i + len(r"\right") + 1].isalpha():
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
_TEXTISH_WRAP_SAFE_PREFIXES = ("\\text{",)


_KATEX_UNICODE_REPLACEMENTS = {
    "⓪": r"\textcircled{0}",
    **{chr(0x2460 + i): rf"\textcircled{{{i + 1}}}" for i in range(20)},
    "®": r"\text{\textregistered}",
    "™": r"\text{TM}",
    "–": r"\text{--}",
    "†": r"\dagger{}",
    "Ω": r"\Omega{}",
    "○": r"\textcircled{ }",
    "⊖": r"\text{\textcircled{-}}",
}


def _replace_katex_unicode(latex: str) -> str:
    r"""把 KaTeX 无字形但有确定语义等价物的 Unicode 改成受支持命令。"""
    for char, replacement in _KATEX_UNICODE_REPLACEMENTS.items():
        latex = latex.replace(char, replacement)
    return latex


def _wrap_text_mode_commands(latex: str) -> str:
    r"""把只能在文本模式使用的命令包进 \text{}；已有 \text{} 保持幂等。"""
    out: list[str] = []
    i, n = 0, len(latex)
    while i < n:
        if latex.startswith(r"\text{", i):
            brace = i + len(r"\text")
            end = _match_braced(latex, brace)
            if end != -1:
                out.append(latex[i:end])
                i = end
                continue
        command = r"\textregistered"
        if (latex.startswith(command, i)
                and (i + len(command) == n or not latex[i + len(command)].isalpha())):
            out.append(r"\text{\textregistered}")
            i += len(command)
            continue
        command = r"\textcircled"
        if latex.startswith(command + "{", i):
            brace = i + len(command)
            end = _match_braced(latex, brace)
            if end != -1:
                out.append(r"\text{" + latex[i:end] + "}")
                i = end
                continue
        out.append(latex[i])
        i += 1
    return "".join(out)


def wrap_cjk_in_text(latex: str) -> str:
    r"""把数学模式里连续的"文字类" Unicode 字符段包进 \text{...}。

    只在这些字符已经直接身处 \text{} 内时跳过；\mathrm{}/\mathbf{} 仍是数学
    字体模式，CJK 留在其中会触发 unicodeTextInMathMode，故在其内部补 \text{}。
    其余情况原地包裹，不改变周围数学结构。"""
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


# OCR 有时把数学(\ln、上下标、\circ 等)误包进 \text{...},KaTeX 严格模式下这些在 text
# 模式里是硬报错("Can't use function '\ln' in text mode"、"Can't use '^' in text mode")。
# 下面把这类误包的数学拆回数学模式,同时保留真正的文字段(CJK/单位)仍在 \text{} 内。
_MATH_OP_NAMES = frozenset((
    "ln sin cos tan cot sec csc log exp lim max min sup inf arg deg det dim gcd hom ker "
    "sqrt frac sum int prod oint sinh cosh tanh coth arcsin arccos arctan"
).split())
# text 模式硬报错的触发符:裸 ^ / _,或数学函数/算符命令(需反斜杠,\text{max} 中的 max 不触发)
_MATH_IN_TEXT_TRIGGER = re.compile(
    r"[\^_]|\\(?:" + "|".join(sorted(_MATH_OP_NAMES, key=len, reverse=True)) + r")(?![a-zA-Z])")


def _read_command(s: str, i: int) -> tuple[str, int]:
    r"""s[i]=='\\';返回 (命令串, 下一位置)。\name(字母名)或 \<单个非字母,如 \ \, \%>。"""
    j = i + 1
    if j < len(s) and s[j].isalpha():
        k = j
        while k < len(s) and s[k].isalpha():
            k += 1
        return s[i:k], k
    return s[i:j + 1], j + 1


def _read_script_arg(s: str, i: int) -> tuple[str, int]:
    r"""s[i] 是 ^ 或 _;读其参数(braced {..} / 命令 / 单字符),返回 (参数串, 下一位置)。"""
    j = i + 1
    if j >= len(s):
        return "", j
    if s[j] == "{":
        end = _match_braced(s, j)
        if end != -1:
            return s[j:end], end
        return s[j], j + 1
    if s[j] == "\\":
        return _read_command(s, j)
    return s[j], j + 1


def _split_math_from_text(content: str) -> str:
    r"""把误包进 \text{} 的数学(^ _ 及数学算符)拆回数学模式,文字段仍留 \text{}。

    上下标的"基座"取其前最长的 alnum 连续串一并入数学(172^{\circ} 不拆成 17+2);
    数学算符命令(\ln 等)移出并加尾随空格,防与后文字母黏成 \lnZ。控制空格 \ 等
    text-safe 命令留在文字段。"""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("\\text{" + "".join(buf) + "}")
            buf.clear()

    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if c in "^_":
            base = ""
            while buf and buf[-1].isalnum():
                base = buf.pop() + base
            flush()
            arg, i = _read_script_arg(content, i)
            out.append((base or "{}") + c + arg)
            continue
        if c == "\\":
            cmd, j = _read_command(content, i)
            if cmd[1:] in _MATH_OP_NAMES:
                flush()
                out.append(cmd + " ")           # 尾随空格防黏连
            else:
                buf.append(cmd)                 # \ \, \% 等 text-safe 命令留在文字
            i = j
            continue
        buf.append(c)
        i += 1
    flush()
    return "".join(out)


def _repair_math_in_text(s: str) -> str:
    r"""扫描每个 \text{...}(括号配对),含数学触发符则拆分;合法文字 \text{} 原样不动。"""
    if "\\text{" not in s:
        return s
    out: list[str] = []
    i = 0
    while True:
        k = s.find("\\text{", i)
        if k == -1:
            out.append(s[i:])
            break
        out.append(s[i:k])
        brace = k + len("\\text")                # 指向 '{'
        end = _match_braced(s, brace)
        if end == -1:                            # 花括号不配对 → 放弃,不破坏
            out.append(s[k:])
            break
        content = s[brace + 1:end - 1]
        if _MATH_IN_TEXT_TRIGGER.search(content):
            out.append(_split_math_from_text(content))
        else:
            out.append(s[k:end])
        i = end
    return "".join(out)


def sanitize_latex(s: str) -> str:
    r"""引擎方言清洗:删冗余命令 + 合并非法相邻双脚本 + 链式 atop→substack + \cdotd 拆合。"""
    s = _decode_latex_entities(s)
    s = _drop_unescaped_dollar_tokens(s)
    s = _normalize_double_braced_scripts(s)
    s = _repair_pseudo_bmatrix(s)
    s = _unwrap_malformed_integral_frac_runs(s)
    s = _repair_orphan_aprime_integral_limits(s)
    s = _fix_spacing_command_scripts(s)
    for pat, repl in _KATEX_SUB:
        s = pat.sub(repl, s)
    for pat, repl in _KATEX_CMD_MAP:
        s = pat.sub(repl, s)
    s = _unwrap_ensuremath(s)
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
    s = _replace_katex_unicode(s)
    s = _wrap_text_mode_commands(s)
    s = _repair_math_in_text(s)
    s = wrap_cjk_in_text(s)
    return s


def sanitize_formula_number(content: str) -> str:
    """按公式绑定时的同一路径清洗编号，供重建与 Tier0 共用。"""
    raw = (content or "").strip()
    m = _NUM_RE.match(raw)
    return sanitize_latex(m.group(1) if m else raw)


# ---------------------------------------------------------------------------
# Task C:表格降级导出——无合并单元格的简单表改发 Markdown 管道表格(公式
# `$...$` 才能在主流渲染器里生效);结构复杂或判不准一律保留现状 HTML 直出
# (fallback 逐字节不变)。判定与解析复用 table_audit.parse_table_html /
# lint_table_structure(不修改 table_audit.py)。
# ---------------------------------------------------------------------------
_TABLE_TAG_RE = re.compile(r"<table\b", re.IGNORECASE)

# C1 加固(review 裁定 Important):表结构以外的任何标签(br/sup/sub/b/i/…)一律
# 触发 HTML 回落——parse_table_html 只留纯文本,5<br>10 会拼成 510(数值损坏),
# <sup>2</sup> 会丢上标语义。用 html.parser 收集真实标签名(不是正则匹配子串,
# 避免属性/实体误伤),不在这七个表结构标签白名单内的任何标签都算"非结构标签"。
_TABLE_STRUCTURAL_TAGS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"})


class _TagCollector(HTMLParser):
    """收集内容里出现过的所有起始标签名(html.parser 自动转小写)。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)


def _has_non_structural_tags(html: str) -> bool:
    collector = _TagCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:
        return True                              # 判不准 → 保守当作"有非结构标签"
    return bool(collector.tags - _TABLE_STRUCTURAL_TAGS)


def _math_span_contains_pipe(text: str) -> bool:
    r"""cell 文本里任一 $...$/$$...$$ 数学区间内部是否含字面 `|`(绝对值 |x|、
    条件概率 P(A|B)、集合记号等)。管道表格按未转义 `|` 切列(块级切列先于行内
    math 规则),数学区间内的 `|` 转义会被 KaTeX 读成 \| 范数记号(语义改变),
    不转义又会被表格切列引擎错误切分(超额列被渲染器丢弃)——两难之下按判不准
    原则整表回落 HTML,不做半吊子转义(review 裁定 Critical)。定界符探测复用
    与 _sanitize_markdown_math_spans/_escape_pipes_outside_math 相同的算法。"""
    i, n = 0, len(text)
    while i < n:
        if text[i] == "$" and not _is_escaped(text, i):
            display = i + 1 < n and text[i + 1] == "$"
            if not display and ((i > 0 and text[i - 1] == "$") or (i + 1 < n and text[i + 1] == "$")):
                i += 1
                continue
            delim = "$$" if display else "$"
            start = i + len(delim)
            end = _find_display_math_end(text, start) if display else _find_inline_math_end(text, start)
            if end != -1:
                if "|" in text[start:end]:
                    return True
                i = end + len(delim)
                continue
        i += 1
    return False


def _is_simple_table(html: str) -> tuple[bool, ParsedTable]:
    r"""判定表格是否可安全降级为管道表格。保守优先:判不准(任一条件不满足或
    解析异常)一律返回 False,调用方回落到现状 HTML 直出,绝不冒险改写复杂表。

    简单表硬性条件(全部满足):单一 <table> 根;全部 cell rowspan==1 且
    colspan==1;无嵌套 <table>;无表结构以外的标签(br/sup/sub/…);结构 lint
    无 error 级问题;行列数 ≥1(空表/空网格天然被此条排除);无 cell 的数学
    区间内含字面 `|`。"""
    table = parse_table_html(html)
    if "html_parse_error" in table.warnings:
        return False, table
    if table.root_count != 1:
        return False, table
    if len(_TABLE_TAG_RE.findall(html)) != table.root_count:
        return False, table                     # 存在嵌套 <table>
    if _has_non_structural_tags(html):
        return False, table                     # 存在表结构以外的标签
    if table.n_rows < 1 or table.n_cols < 1 or not table.cells:
        return False, table
    if any(c.rowspan != 1 or c.colspan != 1 for c in table.cells):
        return False, table
    if any(issue["severity"] == "error" for issue in lint_table_structure(table)):
        return False, table
    if any(_math_span_contains_pipe(c.text) for c in table.cells):
        return False, table
    return True, table


def _escape_pipes_outside_math(text: str) -> str:
    r"""转义管道表格分隔符 `|`,但跳过 $...$/$$...$$ 数学区间内部——公式里常有
    字面 `|`(绝对值 |x|),转义成 \| 会被 KaTeX 读成范数记号,语义改变。定界符
    探测复用与 _sanitize_markdown_math_spans 相同的算法,区间内部原样透传。"""
    out: list[str] = []
    i, n = 0, len(text)
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
                out.append(text[i:end + len(delim)])   # 数学区间原样透传,不转义
                i = end + len(delim)
                continue
        if text[i] == "|":
            out.append(r"\|")
        else:
            out.append(text[i])
        i += 1
    return "".join(out)


def _render_table_cell_md(text: str) -> str:
    """管道表格单个 cell 的文本处理:先走既有 math-span 清洗(公式内容原样保留,
    只清洗定界符内部——含 C3 归一);再转义定界符外的 `|`。内部换行/连续空白
    折叠已由 table_audit._expand_grid 完成(cell.text 产出时即已折叠),此处无需
    重做。"""
    return _escape_pipes_outside_math(_sanitize_markdown_math_spans(text))


def _emit_pipe_table(table: ParsedTable) -> str:
    """把 ParsedTable(已判定为简单表)发射成 Markdown 管道表格。首行为表头,
    其余为数据行;列数取网格宽度 n_cols,缺失列补空 cell(短行不产生错列)。"""
    rows: dict[int, dict[int, str]] = {}
    for cell in table.cells:
        rows.setdefault(cell.row, {})[cell.col] = cell.text
    n_cols = table.n_cols

    def _row_line(row_idx: int) -> str:
        cells = rows.get(row_idx, {})
        texts = [_render_table_cell_md(cells.get(c, "")) for c in range(n_cols)]
        return "| " + " | ".join(texts) + " |"

    lines: list[str] = []
    if table.caption:
        lines.append(f"*{_render_table_cell_md(table.caption)}*")
        lines.append("")
    lines.append(_row_line(0))
    lines.append("| " + " | ".join(["---"] * n_cols) + " |")
    for r in range(1, table.n_rows):
        lines.append(_row_line(r))
    return "\n".join(lines)


def _render_table_block(content: str) -> str:
    """table 块发射入口:简单表 → 管道表格;判不准/复杂表 → 现状 HTML 直出
    (与改动前逐字节一致,fallback 完全不变)。"""
    is_simple, table = _is_simple_table(content)
    if is_simple:
        return _emit_pipe_table(table)
    return _sanitize_markdown_math_spans(restore_emphasis_dots(content))


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
            emit(y0, [bid], f"## {_sanitize_markdown_math_spans(content)}")
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
                tag = sanitize_formula_number(nxt.get("block_content") or "")
                body = _strip_latex_tags(body)
                emit(y0, [bid, nxt.get("block_id")], f"$$ {body} \\tag{{{tag}}} $$")
                i += 1                      # 吸收编号块
            else:
                emit(y0, [bid], f"$$ {body} $$")
        elif label == "formula_number":
            emit(y0, [bid], content)
        elif label == "inline_formula":
            # PaddleOCR-VL 的 inline_formula 内容已自带 $$...$$(或 $...$)包裹;历史上落
            # 未知分支当纯文本直落,绕过 sanitize_latex,命令映射/CJK 等触不到内部 latex。
            # 改走 math-span 清洗:有 $ 包裹则清洗内部,无包裹的裸内容原样穿过(等价旧行为)。
            emit(y0, [bid], _sanitize_markdown_math_spans(content))
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
        elif label == "table":
            # Task C:简单表降级为管道表格(公式即时渲染);复杂/判不准表原样
            # 走现状 HTML 直出——判定与解析见 _render_table_block/_is_simple_table。
            if not content:
                continue
            extras.append({"y0": y0, "seq": seq, "bids": [block_id],
                           "md": _render_table_block(content)})
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
