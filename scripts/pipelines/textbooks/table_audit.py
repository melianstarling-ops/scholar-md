"""OCR 表格结构与数值审计(计划 §6.6,Task 7)。

表格内容**永远以 OCR HTML 为最终产物**——本模块全部只审计:对 OCR 输出的
HTML 表格做结构 lint(标准库 html.parser,禁止正则解析嵌套 HTML),对映射到
表格 bbox 内的源 words 做数值保真对账(复用 source_audit.extract_numeric_
tokens)。只产生 issue/报告,绝不修改 raw OCR table 内容。

跨页表头指纹比较(相邻页表头相似判定)属 Task 8/9 编排职责,本模块只提供
header_fingerprint 单表指纹,不做任何跨页合并/聚合落盘(YAGNI)。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser

from scripts.pipelines.textbooks.source_audit import (
    SourceWord,
    extract_numeric_tokens,
    normalize_prose_for_compare,
)

# ---- PUA/坏码判定:与 source_audit 的惯例一致,独立维护(只读参考,不导入其
# 私有实现——计划约束 7 只允许复用 extract_numeric_tokens / normalize_prose_
# for_compare / SourceWord 这三个公开接口)。
_PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))

# inline math 包装标记:cell 文本去包装后仍要能抽出数字(计划 §6.6)。
_INLINE_MATH_WRAPPERS = ("$$", "$", r"\(", r"\)", r"\[", r"\]")

_WS_RE = re.compile(r"\s+")
_NUMERIC_START_RE = re.compile(r"^[+\-]\d|^\d")
_EXPONENT_CARET_RE = re.compile(r"\^([+\-]?\d+)$")
_EXPONENT_E_RE = re.compile(r"[eE]([+\-]?\d+)$")
_DIGITS_ONLY_RE = re.compile(r"[^0-9]")
_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
_INLINE_MATH_RE = re.compile(
    r"(?P<delimiter>\$\$|\$)(?P<body>.*?)(?P=delimiter)",
    re.DOTALL,
)
_BRACED_DECIMAL_COMMA_RE = re.compile(
    r"(?<=\d)\s*\{\s*,\s*\}\s*(?=\d)"
)
_SCIENTIFIC_E_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<mantissa>\d+(?:\.\d+)?)\s*"
    r"[eE]\s*(?P<sign>[+\-−])\s*(?P<exp>\d+)"
)
_LATEX_BRACED_EXPONENT_RE = re.compile(
    r"(?P<base>10)\s*\^\s*\{\s*(?P<exp>[+\-−]?\d+)\s*\}"
)
_TIMES_PLAIN_EXPONENT_RE = re.compile(
    r"(?P<prefix>[×xX*]\s*10)\s*(?P<sign>[+\-−])\s*(?P<exp>\d+)"
)
_DECIMAL_COMMA_SPACE_RE = re.compile(r"(?<=\d),\s+(?=\d)")
_SPACED_PERCENT_RE = re.compile(r"(?<=\d)\s+(?=%)")
_ATTACHED_DASH_MINUS_RE = re.compile(r"(?<![A-Za-z0-9])[‒–−](?=\d)")
_SHORT_DECIMAL_COMMA_RE = re.compile(
    r"(?<![\d,])(?P<integer>[+\-−]?\d+),(?P<fraction>\d{1,2})(?!\d)"
)
_DASH_WRAPPED_NUMBER_RE = re.compile(
    r"^\s*[—–\-]\s*(?P<number>\d+(?:[.,]\d+)?%?)\s*[—–\-]\s*$"
)
_ATTACHED_MICRO_T_PROSE_RE = re.compile(
    r"(?<=\d)(?P<unit>[µμ]T)(?=[A-Za-z]{3,})"
)
_SINGLE_ASCII_IDENTIFIER_TAIL_RE = re.compile(r"(?<![A-Za-z])([A-Za-z])$")
_LEADING_IDENTIFIER_INDEX_RE = re.compile(
    r"^(?P<index>\d{1,3})(?=[A-Za-z\u3400-\u9fff])"
)
_ANNEX_ZERO_RE = re.compile(
    r"(?P<label>附录|annex|appendix)(?P<space>\s*)(?P<zero>0)"
    r"(?=[A-Za-z\u3400-\u9fff])",
    re.IGNORECASE,
)
_AMBIGUOUS_ONE_RE = re.compile(r"(?<![A-Za-z0-9])1(?![A-Za-z0-9])")
_AMBIGUOUS_L_SQUARED_RE = re.compile(r"(?<!\d)12(?=\s*/)")
_FORMULA_EVIDENCE_STRIP_RE = re.compile(r"[^A-Za-z0-9\u3400-\u9fff.\-]+")
_PLAIN_DECIMAL_VALUE_RE = re.compile(r"^\d+(?:[.,]\d+)*$")
_LATEX_SYMBOL_COMMANDS = {
    r"\times": "×",
    r"\pm": "±",
    r"\leq": "≤",
    r"\le": "≤",
    r"\geq": "≥",
    r"\ge": "≥",
    r"\mu": "μ",
    r"\sigma": "σ",
    r"\lambda": "λ",
    r"\pi": "π",
}


def _is_pua(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _PUA_RANGES)


def _is_bad_control(ch: str) -> bool:
    return unicodedata.category(ch) == "Cc" and not ch.isspace()


def _has_bad_codes(text: str) -> bool:
    """粗略坏码判定:U+FFFD/PUA/非空白控制符任一命中即坏(计划 §6.6)。"""
    for ch in text:
        if ch.isspace():
            continue
        if ch == "�" or _is_pua(ch) or _is_bad_control(ch):
            return True
    return False


# ===========================================================================
# 数据结构
# ===========================================================================


@dataclass(frozen=True)
class TableCell:
    row: int
    col: int
    rowspan: int
    colspan: int
    text: str


@dataclass
class ParsedTable:
    cells: list[TableCell]
    n_rows: int
    n_cols: int
    caption: str | None
    warnings: list[str] = field(default_factory=list)
    # 额外字段(不在接口"至少含"清单内,内部/下游诊断用):
    root_count: int = 1
    header_row_count: int = 0
    visual_node_count: int = 0


# ===========================================================================
# HTML 解析:标准库 html.parser 状态机(禁止正则解析嵌套 HTML)
# ===========================================================================


def _parse_span_attr(raw: str | None) -> int:
    """rowspan/colspan 属性 → int。缺失→1;非数字→0(哨兵,交给 lint 判非法)。"""
    if raw is None:
        return 1
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return 0


class _TableHTMLParser(HTMLParser):
    """只解析**第一个**顶层 `<table>` 根的网格;其余顶层根只计数,不合并
    (计划 §6.6"多个 table 根必须分别计数报告,不悄悄拼成一个")。

    嵌套在某个 cell 内的 `<table>` 视为该 cell 的不透明内容:其文字仍流入
    外层 cell 的文本缓冲,但不参与外层网格的行列结构。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root_count = 0
        self._table_depth = 0
        self._active_root = 0  # 当前处于第几个顶层根(0=不在任何顶层根内)

        self._section: str | None = None  # thead/tbody/tfoot,仅顶层根1有效
        self._seen_thead = False

        self._in_row = False
        self._current_row: dict | None = None
        self.rows_raw: list[dict] = []

        self._current_cell: dict | None = None
        self._active_cell_buffer: list[str] | None = None

        self._in_caption = False
        self._caption_parts: list[str] = []
        self.caption: str | None = None

        self.orphan_cell_count = 0
        self.visual_node_count = 0

    # -- 结构上下文判定:仅顶层根 1、且未处于嵌套子表内时才记入网格 --------
    def _structural_context_ok(self) -> bool:
        return self._active_root == 1 and self._table_depth == 1

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self.root_count += 1
                self._active_root = self.root_count
            return

        if not self._structural_context_ok():
            return

        if tag in ("img", "svg", "canvas", "chart"):
            self.visual_node_count += 1
            return

        if tag in ("thead", "tbody", "tfoot"):
            self._section = tag
            if tag == "thead":
                self._seen_thead = True
            return

        if tag == "caption":
            self._in_caption = True
            self._caption_parts = []
            return

        if tag == "tr":
            if self._in_row:
                self._finalize_row()
            self._in_row = True
            self._current_row = {"cells": [], "section": self._section}
            return

        if tag in ("td", "th"):
            if self._current_cell is not None:
                self._finalize_cell()
            if not self._in_row:
                self.orphan_cell_count += 1
                return
            self._current_cell = {
                "tag": tag,
                "rowspan_raw": attrs_d.get("rowspan"),
                "colspan_raw": attrs_d.get("colspan"),
            }
            self._active_cell_buffer = []
            return

    def handle_endtag(self, tag):
        if tag == "table":
            if self._table_depth == 1:
                if self._current_cell is not None:
                    self._finalize_cell()
                if self._in_row:
                    self._finalize_row()
                self._active_root = 0
            self._table_depth = max(0, self._table_depth - 1)
            return

        if not self._structural_context_ok():
            return

        if tag in ("thead", "tbody", "tfoot"):
            self._section = None
            return
        if tag == "caption":
            if self._in_caption:
                text = _WS_RE.sub(" ", "".join(self._caption_parts)).strip()
                if self.caption is None:
                    self.caption = text
            self._in_caption = False
            return
        if tag == "tr":
            if self._in_row:
                self._finalize_row()
            return
        if tag in ("td", "th"):
            if self._current_cell is not None:
                self._finalize_cell()
            return

    def handle_data(self, data):
        if self._active_cell_buffer is not None:
            self._active_cell_buffer.append(data)
        elif self._in_caption:
            self._caption_parts.append(data)

    def _finalize_cell(self) -> None:
        cell = self._current_cell
        text = "".join(self._active_cell_buffer or [])
        cell["text"] = text
        if self._current_row is not None:
            self._current_row["cells"].append(cell)
        self._current_cell = None
        self._active_cell_buffer = None

    def _finalize_row(self) -> None:
        if self._current_row is not None:
            self.rows_raw.append(self._current_row)
        self._in_row = False
        self._current_row = None

    def close(self) -> None:
        # 文档结尾时若仍有未闭合的 cell/row(容错畸形 HTML),补齐落盘。
        if self._current_cell is not None:
            self._finalize_cell()
        if self._in_row:
            self._finalize_row()
        super().close()


def _expand_grid(rows_raw: list[dict]) -> tuple[list[TableCell], int, int]:
    """按 HTML 表格规则展开有效网格:同行内遇到已占用列则跳到下一个空列
    (标准"找下一个空闲格"算法——按此算法构造出的网格本身不会自相重叠;
    lint_table_structure 对 overlap 的校验是独立于本函数的通用防御性校验,
    可直接喂手工构造的 ParsedTable)。
    """
    n_rows_total = len(rows_raw)
    grid: list[list[bool]] = [[] for _ in range(n_rows_total)]
    cells: list[TableCell] = []
    max_col = 0

    def _ensure_row_len(r: int, upto: int) -> None:
        while len(grid[r]) <= upto:
            grid[r].append(False)

    for r, row in enumerate(rows_raw):
        col_cursor = 0
        for raw_cell in row["cells"]:
            _ensure_row_len(r, col_cursor)
            while grid[r][col_cursor]:
                col_cursor += 1
                _ensure_row_len(r, col_cursor)

            rowspan_val = _parse_span_attr(raw_cell["rowspan_raw"])
            colspan_val = _parse_span_attr(raw_cell["colspan_raw"])
            eff_rowspan = rowspan_val if rowspan_val >= 1 else 1
            eff_colspan = colspan_val if colspan_val >= 1 else 1

            start_col = col_cursor
            for rr in range(r, min(r + eff_rowspan, n_rows_total)):
                _ensure_row_len(rr, start_col + eff_colspan - 1)
                for cc in range(start_col, start_col + eff_colspan):
                    grid[rr][cc] = True

            text = _WS_RE.sub(" ", raw_cell.get("text", "")).strip()
            cells.append(
                TableCell(
                    row=r,
                    col=start_col,
                    rowspan=rowspan_val,
                    colspan=colspan_val,
                    text=text,
                )
            )
            max_col = max(max_col, start_col + eff_colspan)
            col_cursor = start_col + eff_colspan

    n_cols = max_col
    return cells, n_rows_total, n_cols


def parse_table_html(content: str) -> ParsedTable:
    """解析 OCR table block 的 HTML(标准库 html.parser,禁止正则解析嵌套结构)。

    只接受第一个顶层 `<table>` 根构建网格;若存在多个顶层根,分别计数
    (root_count),不悄悄拼成一个。空 HTML / 非 table HTML / 空表均合法
    返回(cells=[]、n_rows=n_cols=0),不抛异常。
    """
    parser = _TableHTMLParser()
    warnings: list[str] = []
    if content:
        try:
            # LaTeX 比较式常直接写在 HTML cell 中（如 ``$10<l<63$``）。
            # 裸 ``<`` 会被 HTMLParser 当成畸形标签并吞掉余下 cell/闭合标签；
            # 这里只在数学定界符内部转义，供审计解析使用，不改写原始内容。
            audit_content = _INLINE_MATH_RE.sub(
                lambda m: (
                    m.group("delimiter")
                    + m.group("body").replace("<", "&lt;")
                    + m.group("delimiter")
                ),
                content,
            )
            parser.feed(audit_content)
            parser.close()
        except Exception:
            # 畸形 HTML 兜底:不抛异常,按已解析到的部分继续,记一条警告。
            warnings.append("html_parse_error")

    if parser.orphan_cell_count:
        warnings.append("orphan_cell")

    cells, n_rows, n_cols = _expand_grid(parser.rows_raw)

    header_row_count = 0
    if parser._seen_thead:
        header_row_count = sum(1 for row in parser.rows_raw if row.get("section") == "thead")
    elif parser.rows_raw:
        # 无显式 thead:若首行全部由 <th> 构成,按惯例视为表头行。
        first_cells = parser.rows_raw[0]["cells"]
        if first_cells and all(c["tag"] == "th" for c in first_cells):
            header_row_count = 1

    return ParsedTable(
        cells=cells,
        n_rows=n_rows,
        n_cols=n_cols,
        caption=parser.caption,
        warnings=warnings,
        root_count=parser.root_count,
        header_row_count=header_row_count,
        visual_node_count=parser.visual_node_count,
    )


# ===========================================================================
# 结构 lint(计划 §6.6):纯函数,只依赖 ParsedTable 字段——可直接喂手工构造
# 的 ParsedTable,不要求其一定来自 parse_table_html。
# ===========================================================================


def lint_table_structure(table: ParsedTable) -> list[dict]:
    """OCR table HTML 结构 lint。只读:不修改 table,只产生 issue 列表。"""
    issues: list[dict] = []

    if table.root_count > 1:
        issues.append(
            {
                "code": "multiple_table_roots",
                "severity": "warning",
                "detail": f"检测到 {table.root_count} 个顶层 <table> 根,分别计数,未合并",
            }
        )

    if "orphan_cell" in table.warnings:
        issues.append(
            {
                "code": "orphan_cell",
                "severity": "warning",
                "detail": "存在未被 <tr> 包裹的孤立 td/th,已从网格中剔除",
            }
        )

    if table.n_rows == 0 or table.n_cols == 0 or not table.cells:
        issues.append(
            {
                "code": "empty_table",
                "severity": "warning",
                "detail": "表格为空(无有效行列或无根 <table>)",
            }
        )
        return issues

    # ---- 独立重建占用网格:校验 span 合法性 + 越界 + 重叠 + 洞 -----------
    occupancy: dict[tuple[int, int], int] = {}
    for idx, cell in enumerate(table.cells):
        if cell.rowspan < 1 or cell.colspan < 1:
            issues.append(
                {
                    "code": "invalid_span",
                    "severity": "error",
                    "row": cell.row,
                    "col": cell.col,
                    "detail": (
                        f"cell(row={cell.row},col={cell.col}) span 非法:"
                        f"rowspan={cell.rowspan},colspan={cell.colspan}"
                    ),
                }
            )
        eff_rowspan = cell.rowspan if cell.rowspan >= 1 else 1
        eff_colspan = cell.colspan if cell.colspan >= 1 else 1

        if cell.row + eff_rowspan > table.n_rows or cell.col + eff_colspan > table.n_cols:
            issues.append(
                {
                    "code": "invalid_span",
                    "severity": "error",
                    "row": cell.row,
                    "col": cell.col,
                    "detail": (
                        f"cell(row={cell.row},col={cell.col}) span 越界超出表格范围 "
                        f"(n_rows={table.n_rows},n_cols={table.n_cols})"
                    ),
                }
            )

        for rr in range(cell.row, min(cell.row + eff_rowspan, table.n_rows)):
            for cc in range(cell.col, min(cell.col + eff_colspan, table.n_cols)):
                if (rr, cc) in occupancy:
                    issues.append(
                        {
                            "code": "overlapping_cells",
                            "severity": "error",
                            "row": rr,
                            "col": cc,
                            "detail": (
                                f"格 (row={rr},col={cc}) 被 cell#{occupancy[(rr, cc)]} 与 "
                                f"cell#{idx} 同时声称占据"
                            ),
                        }
                    )
                else:
                    occupancy[(rr, cc)] = idx

    for rr in range(table.n_rows):
        for cc in range(table.n_cols):
            if (rr, cc) not in occupancy:
                issues.append(
                    {
                        "code": "grid_hole",
                        "severity": "warning",
                        "row": rr,
                        "col": cc,
                        "detail": f"格 (row={rr},col={cc}) 无任何 cell 覆盖,疑似行列不一致",
                    }
                )

    # ---- 空表头 / 整行空白 -------------------------------------------------
    rows_text: dict[int, list[str]] = {}
    for cell in table.cells:
        rows_text.setdefault(cell.row, []).append(cell.text)

    if table.header_row_count > 0:
        header_texts = [
            t for r in range(table.header_row_count) for t in rows_text.get(r, [])
        ]
        if header_texts and not any(t.strip() for t in header_texts):
            issues.append(
                {
                    "code": "empty_header",
                    "severity": "warning",
                    "detail": "表头行文本全为空白",
                }
            )

    for rr in range(table.header_row_count, table.n_rows):
        texts = rows_text.get(rr, [])
        if texts and not any(t.strip() for t in texts):
            issues.append(
                {
                    "code": "empty_row",
                    "severity": "warning",
                    "row": rr,
                    "detail": f"第 {rr} 行全部 cell 文本为空白",
                }
            )

    return issues


# ===========================================================================
# 内容/数值对账(计划 §6.6):复用 source_audit.extract_numeric_tokens。
# ===========================================================================


def _strip_inline_math_wrappers(text: str) -> str:
    """去掉 cell 内 inline math 包装标记(`$...$`/`\\(...\\)`/`\\[...\\]`等),
    保留内部内容以便仍能抽取数字(计划 §6.6)。只剥符号,不做任何数学求值。
    """
    out = text
    for wrapper in _INLINE_MATH_WRAPPERS:
        out = out.replace(wrapper, "")
    return out


def _canonicalize_numeric_audit_text(text: str) -> str:
    """把同一数值的 OCR/源层表示折叠到审计专用视图。

    只供数值对账使用，不回写 OCR HTML：
    - LaTeX 符号命令先换成 Unicode，剩余命令换成分隔空格，避免
      ``\\le0.44`` 的命令尾字母吞掉前导 ``0.``；
    - ``\\%``、数字间小数逗号空格、短小数逗号做保守等价折叠；
    - 双侧破折号包裹的数字视为表格占位符，不当作独立数值；
    - ``10^{-12}`` 与源层常见的 ``×10−12`` 统一为 caret 指数。
    """
    out = _strip_inline_math_wrappers(text or "")
    out = out.replace(r"\%", "%")
    for command, symbol in _LATEX_SYMBOL_COMMANDS.items():
        out = re.sub(re.escape(command) + r"(?![A-Za-z])", symbol, out)

    out = _LATEX_BRACED_EXPONENT_RE.sub(
        lambda m: (
            f"{m.group('base')}^"
            f"{m.group('exp').replace('−', '-').lstrip('+')}"
        ),
        out,
    )
    out = _TIMES_PLAIN_EXPONENT_RE.sub(
        lambda m: (
            f"{m.group('prefix')}^"
            f"{m.group('sign').replace('−', '-').replace('+', '')}"
            f"{m.group('exp')}"
        ),
        out,
    )

    # LaTeX ``16{,}0`` 中的花括号只用于抑制逗号后的数学间距；它与
    # born-digital 文字层的 ``16,0`` 是同一小数，不能先拆括号再分成
    # 两个独立数值 token。
    out = _BRACED_DECIMAL_COMMA_RE.sub(",", out)
    out = _SCIENTIFIC_E_RE.sub(
        lambda m: (
            ("" if m.group("mantissa") == "1" else m.group("mantissa") + "×")
            + "10^"
            + m.group("sign").replace("−", "-").replace("+", "")
            + m.group("exp")
        ),
        out,
    )

    # 未识别的 LaTeX 命令仅作 token 分隔，不吃其花括号内的普通数字。
    out = _LATEX_COMMAND_RE.sub(" ", out)
    out = out.replace("{", " ").replace("}", " ")
    out = _DECIMAL_COMMA_SPACE_RE.sub(",", out)
    # born-digital 表层常用 en dash 表示负号；只接受“紧贴数字且左侧不是
    # 标识符/数字”的强局部形状，`Table – 3` 之类版式破折号不会命中。
    out = _ATTACHED_DASH_MINUS_RE.sub("-", out)
    # `±5 %` 与 LaTeX `\pm 5\%` 是同一 cell 内的表示差异。
    out = _SPACED_PERCENT_RE.sub("", out)
    out = _SHORT_DECIMAL_COMMA_RE.sub(
        lambda m: (
            f"{m.group('integer').replace('−', '-')}.{m.group('fraction')}"
        ),
        out,
    )
    out = _ATTACHED_MICRO_T_PROSE_RE.sub(
        lambda m: f"{m.group('unit')} ", out
    )

    wrapped = _DASH_WRAPPED_NUMBER_RE.fullmatch(out)
    if wrapped:
        out = ""
    return out


def _is_numeric_value_token(tok: str) -> bool:
    """extract_numeric_tokens 返回的 token 里,数值 token 与紧随其后的单位
    token 混在同一个列表里、无标记区分——用 token 是否以数字/正负号开头
    这一结构特征区分两者(单位 token 恒以字母/符号开头)。
    """
    return bool(_NUMERIC_START_RE.match(tok))


def _split_sign(tok: str) -> tuple[str, str]:
    if tok[:1] in ("+", "-"):
        return tok[0], tok[1:]
    return "", tok


def _digits_only(tok: str) -> str:
    return _DIGITS_ONLY_RE.sub("", tok)


def _exponent_info(tok: str) -> tuple[str, str | None]:
    m = _EXPONENT_CARET_RE.search(tok)
    if m:
        return tok[: m.start()], m.group(1)
    m = _EXPONENT_E_RE.search(tok)
    if m:
        return tok[: m.start()], m.group(1)
    return tok, None


def _classify_numeric_mismatch(source_tok: str, ocr_tok: str) -> str | None:
    """两个未能精确匹配的数值 token 之间是否存在可解释的关系:
    sign_flip(符号反转)/ decimal_shift(小数点移位)/ exponent_change(指数变化)。
    均不满足则返回 None(调用方据此判 numeric_missing)。
    """
    s_sign, s_rest = _split_sign(source_tok)
    o_sign, o_rest = _split_sign(ocr_tok)
    if s_rest == o_rest and s_sign != o_sign:
        return "sign_flip"

    s_digits = _digits_only(s_rest)
    o_digits = _digits_only(o_rest)
    if (
        _PLAIN_DECIMAL_VALUE_RE.fullmatch(s_rest)
        and _PLAIN_DECIMAL_VALUE_RE.fullmatch(o_rest)
        and s_digits
        and o_digits
        and s_digits.lstrip("0") == o_digits.lstrip("0")
        and s_rest != o_rest
    ):
        return "decimal_shift"

    s_mant, s_exp = _exponent_info(source_tok)
    o_mant, o_exp = _exponent_info(ocr_tok)
    if (s_exp is not None or o_exp is not None):
        s_mant_digits = _digits_only(s_mant).lstrip("0")
        o_mant_digits = _digits_only(o_mant).lstrip("0")
        if s_mant_digits and s_mant_digits == o_mant_digits and s_exp != o_exp:
            return "exponent_change"

    return None


def _unique_mismatch_pairs(
    source_tokens: list[str], ocr_tokens: list[str]
) -> dict[int, tuple[int, str]]:
    """只返回双方唯一兼容的 mismatch 配对。

    旧实现按源 token 顺序拿第一个兼容 OCR token，会在大表中跨行乱配，
    既伪造 sign/decimal 告警，也可能吃掉真正 missing。这里要求：
    source 仅有一个候选，且该 OCR 也仅被这一个 source 兼容。
    """
    source_candidates: dict[int, list[tuple[int, str]]] = {}
    ocr_candidates: dict[int, list[int]] = {}
    for source_idx, source_tok in enumerate(source_tokens):
        for ocr_idx, ocr_tok in enumerate(ocr_tokens):
            code = _classify_numeric_mismatch(source_tok, ocr_tok)
            if code is None:
                continue
            source_candidates.setdefault(source_idx, []).append((ocr_idx, code))
            ocr_candidates.setdefault(ocr_idx, []).append(source_idx)

    pairs: dict[int, tuple[int, str]] = {}
    for source_idx, candidates in source_candidates.items():
        if len(candidates) != 1:
            continue
        ocr_idx, code = candidates[0]
        if len(ocr_candidates.get(ocr_idx, [])) == 1:
            pairs[source_idx] = (ocr_idx, code)
    return pairs


def _group_words_by_line(words: list) -> list[str]:
    """按 (block_no, line_no) 分组、组内按 word_no 排序拼接——与 source_audit
    的分行惯例一致。排序保证多重集对账结果与输入 source_words 列表顺序无关
    (计划 §6.6"文字顺序差异只作 warning——数值对账基于多重集,不基于顺序")。
    接受 SourceWord 或等价 dict(以 SourceWord 字段名为准)。
    """
    lines: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for w in words:
        if isinstance(w, SourceWord):
            text, block_no, line_no, word_no = w.text, w.block_no, w.line_no, w.word_no
        elif isinstance(w, dict):
            text = w.get("text", "")
            block_no = w.get("block_no", 0)
            line_no = w.get("line_no", 0)
            word_no = w.get("word_no", 0)
        else:
            continue
        lines.setdefault((block_no, line_no), []).append((word_no, text))

    line_texts: list[str] = []
    for key in sorted(lines):
        ordered = sorted(lines[key], key=lambda t: t[0])
        line_texts.append(" ".join(t for _, t in ordered))
    return line_texts


def _compact_compare_text(text: str) -> str:
    return _WS_RE.sub("", normalize_prose_for_compare(text or ""))


def _stitch_source_identifier_wraps(
    source_lines: list[str], table: ParsedTable
) -> list[str]:
    """只在 OCR cell 提供完整局部证据时拼回源层跨行变量下标。

    born-digital 表格偶尔把 ``B1`` 的 ``B`` 与 ``1`` 分进相邻 PDF text
    block；逐行抽数会把下一行开头的 ``1`` 错当独立数值。合并门刻意很窄：
    上一行须以单个 ASCII 变量字母结束，下一行须以 1--3 位数字紧接正文，
    且同一个 OCR cell 必须含边界两侧的局部上下文。仅在别处出现 ``B1``
    不够，因此真正独立的数值 ``1`` 不会被吞掉。
    """
    cell_texts = [_compact_compare_text(cell.text) for cell in table.cells]
    stitched: list[str] = []
    for line in source_lines:
        if stitched:
            previous = stitched[-1]
            previous_compact = _compact_compare_text(previous)
            current_compact = _compact_compare_text(line)
            tail = _SINGLE_ASCII_IDENTIFIER_TAIL_RE.search(previous_compact)
            index = _LEADING_IDENTIFIER_INDEX_RE.match(current_compact)
            if tail and index:
                # 两侧都带少量正文上下文，避免只凭一个常见的 B1 在全表误并。
                left_context = previous_compact[-8:]
                right_context = current_compact[:8]
                needle = left_context + right_context
                if (
                    len(left_context) >= 2
                    and len(right_context) > len(index.group("index"))
                    and any(needle in cell_text for cell_text in cell_texts)
                ):
                    stitched[-1] = previous + line
                    continue
        stitched.append(line)
    return stitched


def _reconcile_source_annex_o_glyphs(
    source_lines: list[str], table: ParsedTable
) -> list[str]:
    """在强局部证据下把引用标签中的源层 ``Annex 0`` 视作字母 ``O``。

    只处理 ``附录/Annex/Appendix + 0``，且把 0 换成 O 后的两侧局部文本
    必须出现在同一个 OCR cell。普通正文数值 0 不命中引用标签，因此仍会
    正常参与 missing 对账。
    """
    cell_texts = [_compact_compare_text(cell.text) for cell in table.cells]
    reconciled: list[str] = []
    for original in source_lines:
        line = original
        while True:
            match = _ANNEX_ZERO_RE.search(line)
            if match is None:
                break
            zero_start, zero_end = match.span("zero")
            candidate = line[:zero_start] + "O" + line[zero_end:]
            candidate_compact = _compact_compare_text(candidate)
            compact_match = _ANNEX_ZERO_RE.search(_compact_compare_text(line))
            if compact_match is None:
                break
            compact_zero = compact_match.start("zero")
            needle = candidate_compact[
                max(0, compact_zero - 12): compact_zero + 13
            ]
            if len(needle) >= 5 and any(
                needle in cell_text for cell_text in cell_texts
            ):
                line = candidate
                continue
            break
        reconciled.append(line)
    return reconciled


def _formula_evidence_key(text: str) -> str:
    """公式局部证据键：只折叠排版符号，不折叠数字本身。"""
    canonical = _canonicalize_numeric_audit_text(text).replace("_", "")
    return _FORMULA_EVIDENCE_STRIP_RE.sub("", canonical)


def _reconcile_source_formula_digit_glyphs(
    source_lines: list[str], table: ParsedTable
) -> tuple[list[str], int]:
    """仅在同 cell 局部证据 + 表内重复证据下调和源层 ``l/1`` 字形。

    中文标准 PDF 的 born-digital 字体有时把变量小写 ``l`` 导出为数字
    ``1``，并把 ``l²`` 导出为 ``12``。直接把所有 1 当作 l 会吞掉真实数值，
    所以门限刻意不是比例阈值，而是两个离散证据：

    1. 把疑似字形替换后，整条公式的数字/字母键必须出现在同一个 OCR cell；
    2. 同一张表至少有两条不同源行获得这种局部证据。

    命中时只修改审计视图，并由调用方留下 warning；不回写 OCR/Markdown。
    """
    cell_keys = [_formula_evidence_key(cell.text) for cell in table.cells]
    candidates: dict[int, str] = {}
    for index, original in enumerate(source_lines):
        canonical = _canonicalize_numeric_audit_text(original)
        if not any(op in canonical for op in ("=", "<", ">", "≤", "≥", "×", "/", "^")):
            continue

        candidate = _AMBIGUOUS_L_SQUARED_RE.sub("l^2", canonical)

        def _one_to_l(match: re.Match) -> str:
            prefix = candidate[max(0, match.start() - 3): match.start()]
            # 指数位是真数值，不属于 l/1 字形歧义。
            if prefix.endswith(("^", "^-", "^+")):
                return match.group(0)
            return "l"

        candidate = _AMBIGUOUS_ONE_RE.sub(_one_to_l, candidate)
        if candidate == canonical:
            continue
        key = _formula_evidence_key(candidate)
        if len(key) >= 5 and any(key in cell_key for cell_key in cell_keys):
            candidates[index] = candidate

    if len(candidates) < 2:
        return source_lines, 0
    return [
        candidates.get(index, line) for index, line in enumerate(source_lines)
    ], len(candidates)


def _word_text(w) -> str:
    if isinstance(w, SourceWord):
        return w.text
    if isinstance(w, dict):
        return w.get("text", "")
    return ""


def header_fingerprint(table: ParsedTable) -> str:
    """单表表头指纹(计划 §6.6)。跨页表头相似度比较属 Task 8/9 编排职责,
    本函数只提供确定性指纹:相同表头文本 → 相同指纹,不同 → 不同。
    """
    header_rows = table.header_row_count if table.header_row_count > 0 else (1 if table.n_rows > 0 else 0)
    header_cells = sorted(
        (c for c in table.cells if c.row < header_rows),
        key=lambda c: (c.row, c.col),
    )
    normalized = " ".join(normalize_prose_for_compare(c.text) for c in header_cells)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def audit_table(
    block: dict,
    source_words: list,
    *,
    unit_missing_severity: str = "warning",
) -> dict:
    """OCR table block 的结构 + 数值双审计(计划 §6.6)。只读:绝不修改
    block/source_words,只产生 issue/报告。

    block: OCR table block dict(block_label="table",block_content 为 HTML)。
    source_words: 归属到该块 bbox 的源 words(SourceWord 或等价 dict)。
    """
    content = (block.get("block_content") or "") if isinstance(block, dict) else ""
    table = parse_table_html(content)
    structure_issues = lint_table_structure(table)

    words = list(source_words or [])
    raw_texts = [_word_text(w) for w in words]

    metrics: dict = {
        "n_rows": table.n_rows,
        "n_cols": table.n_cols,
        "structure_issue_count": len(structure_issues),
        "visual_node_count": table.visual_node_count,
    }

    # ---- 含图片/图表节点：文字 token 无法覆盖视觉真值，改路由视觉复核 ----
    if table.visual_node_count:
        metrics["source_numeric_token_count"] = None
        metrics["ocr_numeric_token_count"] = None
        return {
            "status": "visual_unscorable",
            "structure_issues": structure_issues,
            "content_issues": [
                {
                    "code": "visual_table_unscorable",
                    "severity": "warning",
                    "detail": (
                        f"表格含 {table.visual_node_count} 个图片/图表节点，"
                        "不以纯文本数值缺失作高严重度结论，需视觉复核"
                    ),
                }
            ],
            "metrics": metrics,
        }

    # ---- 源不可信/为空 → table_unscorable,绝不对 OCR 数值误报 ------------
    if not words or any(_has_bad_codes(t) for t in raw_texts):
        metrics["source_numeric_token_count"] = None
        metrics["ocr_numeric_token_count"] = None
        return {
            "status": "table_unscorable",
            "structure_issues": structure_issues,
            "content_issues": [],
            "metrics": metrics,
        }

    # ---- 源数值 token:按行分组抽取(与 shuffle 无关,见 _group_words_by_line) --
    source_numeric_tokens: list[str] = []
    source_lines = _stitch_source_identifier_wraps(
        _group_words_by_line(words), table
    )
    source_lines = _reconcile_source_annex_o_glyphs(source_lines, table)
    source_lines, formula_glyph_reconciliation_count = (
        _reconcile_source_formula_digit_glyphs(source_lines, table)
    )
    for line_text in source_lines:
        source_numeric_tokens.extend(
            extract_numeric_tokens(_canonicalize_numeric_audit_text(line_text))
        )

    # ---- OCR 数值 token:逐 cell 抽取(去 inline math 包装后),按 (row,col) 顺序 --
    ocr_numeric_tokens: list[str] = []
    for cell in sorted(table.cells, key=lambda c: (c.row, c.col)):
        canonical = _canonicalize_numeric_audit_text(cell.text)
        ocr_numeric_tokens.extend(extract_numeric_tokens(canonical))

    metrics["source_numeric_token_count"] = len(source_numeric_tokens)
    metrics["ocr_numeric_token_count"] = len(ocr_numeric_tokens)

    content_issues: list[dict] = []
    if formula_glyph_reconciliation_count:
        content_issues.append(
            {
                "code": "source_formula_glyph_ambiguous",
                "severity": "warning",
                "detail": (
                    "源层公式中的 l/1 字形仅在同 cell 局部证据与表内重复证据"
                    f"同时成立时调和（{formula_glyph_reconciliation_count} 行）"
                ),
            }
        )

    # ---- 数值 token(以数字/符号开头) vs 单位 token,分别多重集对账 --------
    source_values = [t for t in source_numeric_tokens if _is_numeric_value_token(t)]
    ocr_values = [t for t in ocr_numeric_tokens if _is_numeric_value_token(t)]
    source_units = [t for t in source_numeric_tokens if not _is_numeric_value_token(t)]
    ocr_units = [t for t in ocr_numeric_tokens if not _is_numeric_value_token(t)]

    value_overlap = Counter(source_values) & Counter(ocr_values)
    leftover_source_values = list((Counter(source_values) - value_overlap).elements())
    leftover_ocr_values = list((Counter(ocr_values) - value_overlap).elements())

    mismatch_pairs = _unique_mismatch_pairs(
        leftover_source_values, leftover_ocr_values
    )
    for source_idx, src_tok in enumerate(leftover_source_values):
        pair = mismatch_pairs.get(source_idx)
        if pair is not None:
            _ocr_idx, matched_code = pair
            content_issues.append(
                {
                    "code": matched_code,
                    "severity": "high",
                    "detail": f"源数值 {src_tok!r} 与 OCR 数值疑似发生 {matched_code}",
                }
            )
        else:
            content_issues.append(
                {
                    "code": "numeric_missing",
                    "severity": "high",
                    "detail": f"源数值 token {src_tok!r} 在 OCR 表格中缺失",
                }
            )

    unit_overlap = Counter(source_units) & Counter(ocr_units)
    leftover_source_units = list((Counter(source_units) - unit_overlap).elements())
    for unit_tok in leftover_source_units:
        content_issues.append(
            {
                "code": "unit_missing",
                "severity": unit_missing_severity,
                "detail": f"源单位 token {unit_tok!r} 在 OCR 表格中缺失",
            }
        )

    if structure_issues or content_issues:
        status = "SUSPECT"
    else:
        status = "OK"

    return {
        "status": status,
        "structure_issues": structure_issues,
        "content_issues": content_issues,
        "metrics": metrics,
    }
