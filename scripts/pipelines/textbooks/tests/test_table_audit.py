"""table_audit 模块测试(Task 7:OCR 表格结构与数值审计)。

结构 lint 用标准库 html.parser 解析 synthetic HTML;数值对账复用
source_audit.extract_numeric_tokens,手工构造 SourceWord 列表模拟源文本层。
本模块只审计,不改 OCR 内容——测试只断言返回的 issue/状态,不涉及任何写盘。
"""
from __future__ import annotations

from scripts.pipelines.textbooks.source_audit import SourceWord
from scripts.pipelines.textbooks.table_audit import (
    ParsedTable,
    TableCell,
    audit_table,
    header_fingerprint,
    lint_table_structure,
    parse_table_html,
)


def _word(text, block_no=0, line_no=0, word_no=0):
    return SourceWord(
        text=text, bbox=(0, 0, 1, 1), block_no=block_no, line_no=line_no, word_no=word_no
    )


# ---------------------------------------------------------------------------
# 1. 基础 table/thead/tbody/tr/th/td 解析
# ---------------------------------------------------------------------------
def test_parse_basic_table_structure():
    html = """
    <table>
      <caption>Sample</caption>
      <thead><tr><th>Name</th><th>Score</th></tr></thead>
      <tbody>
        <tr><td>Alice</td><td>10</td></tr>
        <tr><td>Bob</td><td>20</td></tr>
      </tbody>
    </table>
    """
    table = parse_table_html(html)
    assert isinstance(table, ParsedTable)
    assert table.caption.strip() == "Sample"
    assert table.n_rows == 3
    assert table.n_cols == 2
    by_pos = {(c.row, c.col): c.text.strip() for c in table.cells}
    assert by_pos[(0, 0)] == "Name"
    assert by_pos[(0, 1)] == "Score"
    assert by_pos[(1, 0)] == "Alice"
    assert by_pos[(1, 1)] == "10"
    assert by_pos[(2, 0)] == "Bob"
    assert by_pos[(2, 1)] == "20"


# ---------------------------------------------------------------------------
# 2. rowspan/colspan 展开:网格坐标正确
# ---------------------------------------------------------------------------
def test_rowspan_colspan_expand_grid_coordinates():
    html = """
    <table>
      <tr><td rowspan="2">A</td><td>B</td></tr>
      <tr><td>C</td></tr>
      <tr><td colspan="2">D</td></tr>
    </table>
    """
    table = parse_table_html(html)
    by_pos = {(c.row, c.col): c for c in table.cells}
    assert by_pos[(0, 0)].text.strip() == "A"
    assert by_pos[(0, 0)].rowspan == 2
    assert by_pos[(0, 1)].text.strip() == "B"
    # 第二行 col0 被 A 的 rowspan 占用,C 应落在 col1
    assert by_pos[(1, 1)].text.strip() == "C"
    assert (1, 0) not in by_pos
    assert by_pos[(2, 0)].text.strip() == "D"
    assert by_pos[(2, 0)].colspan == 2
    assert table.n_cols == 2
    assert table.n_rows == 3


# ---------------------------------------------------------------------------
# 3a. 重叠 span → structure issue(手工构造 ParsedTable,直接测 lint_table_structure)
# ---------------------------------------------------------------------------
def test_overlapping_span_flagged():
    table = ParsedTable(
        cells=[
            TableCell(row=0, col=0, rowspan=2, colspan=1, text="A"),
            # 与上一 cell 在 (1,0) 冲突:两者都声称占据该格
            TableCell(row=1, col=0, rowspan=1, colspan=1, text="B"),
        ],
        n_rows=2,
        n_cols=1,
        caption=None,
        warnings=[],
    )
    issues = lint_table_structure(table)
    codes = {i["code"] for i in issues}
    assert "overlapping_cells" in codes


# ---------------------------------------------------------------------------
# 3b. 非法 span(0/负/非数字)→ structure issue
# ---------------------------------------------------------------------------
def test_invalid_span_zero_negative_nonnumeric_flagged():
    html = """
    <table>
      <tr><td rowspan="0">A</td><td colspan="-1">B</td><td rowspan="abc">C</td></tr>
    </table>
    """
    table = parse_table_html(html)
    issues = lint_table_structure(table)
    invalid = [i for i in issues if i["code"] == "invalid_span"]
    # 三个非法 cell 都应各自被抓到(0 / 负 / 非数字)
    assert len(invalid) >= 3


# ---------------------------------------------------------------------------
# 3c. 网格洞(行长度不一致,末尾留空)→ structure issue
# ---------------------------------------------------------------------------
def test_grid_hole_flagged():
    html = """
    <table>
      <tr><td>A</td><td>B</td><td>C</td></tr>
      <tr><td>D</td></tr>
    </table>
    """
    table = parse_table_html(html)
    issues = lint_table_structure(table)
    holes = [i for i in issues if i["code"] == "grid_hole"]
    assert len(holes) >= 1
    assert any(h.get("row") == 1 for h in holes)


# ---------------------------------------------------------------------------
# 4. 多个 table 根分别报告,不合并
# ---------------------------------------------------------------------------
def test_multiple_table_roots_reported_separately_not_merged():
    html = """
    <table><tr><td>T1</td></tr></table>
    <table><tr><td>T2</td><td>T2b</td></tr></table>
    """
    table = parse_table_html(html)
    assert table.root_count == 2
    # 不得悄悄拼成一个:第一个根的列数不应被第二个根污染
    assert table.n_cols == 1
    cell_texts = {c.text.strip() for c in table.cells}
    assert cell_texts == {"T1"}
    issues = lint_table_structure(table)
    codes = {i["code"] for i in issues}
    assert "multiple_table_roots" in codes


# ---------------------------------------------------------------------------
# 5. cell 内 inline math 包装去包装后仍可抽数字
# ---------------------------------------------------------------------------
def test_inline_math_wrapper_unwrapped_before_numeric_extraction():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>$12.5$</td></tr></table>",
    }
    words = [_word("12.5")]
    result = audit_table(block, words)
    assert result["status"] == "OK"
    assert result["metrics"]["ocr_numeric_token_count"] >= 1
    assert not any(i["code"] == "numeric_missing" for i in result["content_issues"])


# ---------------------------------------------------------------------------
# 6. sign_flip:源 −12.3% 被 OCR 成 12.3%
# ---------------------------------------------------------------------------
def test_sign_flip_detected():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>12.3%</td></tr></table>",
    }
    words = [_word("−12.3%")]
    result = audit_table(block, words)
    codes = [i["code"] for i in result["content_issues"]]
    assert "sign_flip" in codes
    assert result["status"] == "SUSPECT"


# ---------------------------------------------------------------------------
# 7. decimal_shift:源 0.042 被 OCR 成 0.42
# ---------------------------------------------------------------------------
def test_decimal_shift_detected():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>0.42</td></tr></table>",
    }
    words = [_word("0.042")]
    result = audit_table(block, words)
    codes = [i["code"] for i in result["content_issues"]]
    assert "decimal_shift" in codes
    assert result["status"] == "SUSPECT"


# ---------------------------------------------------------------------------
# 8. exponent_change:含上标字符形式 10⁻³ 的指数变化
# ---------------------------------------------------------------------------
def test_exponent_change_detected_including_superscript_form():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>Result 10 m/s</td></tr></table>",
    }
    words = [_word("Result"), _word("10⁻³", word_no=1), _word("m/s", word_no=2)]
    result = audit_table(block, words)
    codes = [i["code"] for i in result["content_issues"]]
    assert "exponent_change" in codes
    assert result["status"] == "SUSPECT"


# ---------------------------------------------------------------------------
# 9. unit_missing:W/kg、MHz 单位缺失,severity 可注入
# ---------------------------------------------------------------------------
def test_unit_missing_detected_with_injectable_severity():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>5</td></tr></table>",
    }
    words = [_word("5"), _word("MHz", word_no=1)]

    default_result = audit_table(block, words)
    unit_issues = [i for i in default_result["content_issues"] if i["code"] == "unit_missing"]
    assert len(unit_issues) == 1
    assert unit_issues[0]["severity"] == "warning"

    injected_result = audit_table(block, words, unit_missing_severity="error")
    unit_issues2 = [i for i in injected_result["content_issues"] if i["code"] == "unit_missing"]
    assert unit_issues2[0]["severity"] == "error"


# ---------------------------------------------------------------------------
# 10. 源 words 坏码 → table_unscorable,不得误报 OCR 错
# ---------------------------------------------------------------------------
def test_source_words_bad_codes_yield_table_unscorable_no_false_positive():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>999</td></tr></table>",
    }
    # 源词含 U+FFFD 坏码,且数值与 OCR 完全对不上 —— 也绝不能被判成 OCR 数值错误
    words = [_word("� 12.3")]
    result = audit_table(block, words)
    assert result["status"] == "table_unscorable"
    assert result["content_issues"] == []

    # 源 words 为空同样 unscorable
    result_empty = audit_table(block, [])
    assert result_empty["status"] == "table_unscorable"
    assert result_empty["content_issues"] == []


# ---------------------------------------------------------------------------
# 11. 源 words 顺序打乱 → 数值对账结果不变(多重集语义)
# ---------------------------------------------------------------------------
def test_numeric_reconciliation_invariant_to_source_word_order():
    # 数字与单位分属两个 source word,且同一行内还夹了一个不相关的词——
    # 若实现按输入 list 顺序拼接(而不是按 word_no canonical 排序)拼行文本,
    # 打乱 words 列表顺序会把 "5 MHz" 拼成 "MHz 5",导致单位不再紧邻数字,
    # extract_numeric_tokens 就抓不到 unit token,从而漏掉 unit_missing 与
    # token 计数——这正是本测试要抓的回归。
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>5</td></tr></table>",
    }
    ordered = [
        _word("5", line_no=0, word_no=0),
        _word("MHz", line_no=0, word_no=1),
    ]
    shuffled = [ordered[1], ordered[0]]

    r1 = audit_table(block, ordered)
    r2 = audit_table(block, shuffled)
    assert r1["status"] == r2["status"] == "SUSPECT"
    assert r1["content_issues"] == r2["content_issues"]
    assert any(i["code"] == "unit_missing" for i in r1["content_issues"])
    assert r1["metrics"]["source_numeric_token_count"] == r2["metrics"]["source_numeric_token_count"] == 2


# ---------------------------------------------------------------------------
# 12. header_fingerprint:相同表头 → 相同指纹;不同表头 → 不同指纹
# ---------------------------------------------------------------------------
def test_header_fingerprint_same_and_different():
    t1 = parse_table_html(
        "<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
        "<tbody><tr><td>Alice</td><td>10</td></tr></tbody></table>"
    )
    t2 = parse_table_html(
        "<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
        "<tbody><tr><td>Bob</td><td>20</td></tr></tbody></table>"
    )
    t3 = parse_table_html(
        "<table><thead><tr><th>Title</th><th>Value</th></tr></thead>"
        "<tbody><tr><td>Alice</td><td>10</td></tr></tbody></table>"
    )
    fp1 = header_fingerprint(t1)
    fp2 = header_fingerprint(t2)
    fp3 = header_fingerprint(t3)
    assert isinstance(fp1, str) and fp1
    assert fp1 == fp2
    assert fp1 != fp3


# ---------------------------------------------------------------------------
# 13. 空表/空 HTML/非 table HTML → 合法报告,不抛异常
# ---------------------------------------------------------------------------
def test_empty_and_invalid_html_report_without_exception():
    for html in ("", "<table></table>", "<p>no table here</p>"):
        table = parse_table_html(html)
        assert table.n_rows == 0
        assert table.n_cols == 0
        issues = lint_table_structure(table)
        assert any(i["code"] == "empty_table" for i in issues)

    # 孤立 cell(td 直接挂在 table 下,没有 tr 包裹)不应崩溃
    orphan_table = parse_table_html("<table><td>orphan</td></table>")
    orphan_issues = lint_table_structure(orphan_table)
    assert any(i["code"] == "orphan_cell" for i in orphan_issues)

    # audit_table 面对空表也不抛异常,合法报告
    block = {"block_label": "table", "block_content": ""}
    result = audit_table(block, [_word("hello")])
    assert result["status"] in {"OK", "SUSPECT", "table_unscorable"}
    assert isinstance(result["structure_issues"], list)


# ---------------------------------------------------------------------------
# 额外:纯粹数值缺失(非 sign/decimal/exponent 关系)→ numeric_missing
# ---------------------------------------------------------------------------
def test_numeric_missing_when_source_number_absent_from_ocr():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>no numbers here</td></tr></table>",
    }
    words = [_word("42")]
    result = audit_table(block, words)
    codes = [i["code"] for i in result["content_issues"]]
    assert "numeric_missing" in codes
    assert result["status"] == "SUSPECT"


# ---------------------------------------------------------------------------
# 14. 审计专用数值规范化：表示差异不得伪造 high 数值错误
# ---------------------------------------------------------------------------
def test_numeric_audit_canonicalizes_equivalent_cell_notation():
    equivalent_cases = [
        ("0.44", r"$ \le0.44 $"),
        ("0,621", "0, 621"),
        ("±5%", r"$ \pm 5\% $"),
        ("×10−12", r"$ \times 10^{-12} $"),
        ("*10-12", r"$ \times 10^{-12} $"),
        ("—1—", "-1—"),
    ]

    for source_text, ocr_text in equivalent_cases:
        block = {
            "block_label": "table",
            "block_content": f"<table><tr><td>{ocr_text}</td></tr></table>",
        }
        result = audit_table(block, [_word(source_text)])
        high_codes = {
            issue["code"]
            for issue in result["content_issues"]
            if issue["severity"] == "high"
        }
        assert high_codes == set(), (source_text, ocr_text, result)


def test_numeric_audit_canonicalization_preserves_real_decimal_shift():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>2011.2</td></tr></table>",
    }
    result = audit_table(block, [_word("201.12")])
    assert [i["code"] for i in result["content_issues"]] == ["decimal_shift"]


# ---------------------------------------------------------------------------
# 15. mismatch 只在双方唯一兼容时配对，避免全表 first-match 乱配
# ---------------------------------------------------------------------------
def test_ambiguous_mismatch_candidates_are_not_greedily_paired():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>-1</td></tr></table>",
    }
    words = [
        _word("1", block_no=0, line_no=0),
        _word("1", block_no=0, line_no=1),
    ]
    result = audit_table(block, words)
    codes = [i["code"] for i in result["content_issues"]]
    assert "sign_flip" not in codes
    assert codes.count("numeric_missing") == 2


def test_attached_en_dash_and_spaced_percent_are_local_not_numeric_changes():
    block = {
        "block_label": "table",
        "block_content": (
            r"<table><tr><td>amplifier -3 dB; tolerance $\pm 5\%$</td></tr></table>"
        ),
    }
    result = audit_table(
        block, [_word("amplifier –3 dB; tolerance ±5 %")]
    )
    high_codes = {
        issue["code"]
        for issue in result["content_issues"]
        if issue["severity"] == "high"
    }
    assert high_codes == set()


def test_percent_suffix_is_not_misclassified_as_decimal_shift():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>5%</td></tr></table>",
    }
    result = audit_table(block, [_word("5")])
    codes = [issue["code"] for issue in result["content_issues"]]
    assert "decimal_shift" not in codes
    assert "numeric_missing" in codes


# ---------------------------------------------------------------------------
# 16. 含图像节点的表格不能做纯文本数值结论，改走视觉复核
# ---------------------------------------------------------------------------
def test_image_table_is_visual_unscorable_without_high_numeric_missing():
    block = {
        "block_label": "table",
        "block_content": (
            '<table><tr><td><img src="diagram.png" alt="diagram" /></td></tr></table>'
        ),
    }
    result = audit_table(block, [_word("250")])
    assert result["status"] == "visual_unscorable"
    assert result["metrics"]["visual_node_count"] == 1
    assert [i["code"] for i in result["content_issues"]] == [
        "visual_table_unscorable"
    ]
    assert result["content_issues"][0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# 17. 源层变量跨行：只在同一 OCR cell 的局部上下文证实时拼回 B1
# ---------------------------------------------------------------------------
def test_source_identifier_wrap_rejoins_b1_without_false_numeric_missing():
    block = {
        "block_label": "table",
        "block_content": (
            "<table><tr><td>最大感应场（以B1均值归一化，该均值取自调整体积）"
            "</td></tr></table>"
        ),
    }
    words = [
        _word("最大感应场（以B", block_no=0, line_no=0),
        _word("1均值归一化，该", block_no=1, line_no=0),
        _word("均值取自调整体积）", block_no=2, line_no=0),
    ]
    result = audit_table(block, words)
    assert not any(
        issue["code"] == "numeric_missing" for issue in result["content_issues"]
    )


def test_source_identifier_wrap_never_swallows_independent_one():
    block = {
        "block_label": "table",
        "block_content": (
            "<table><tr><td>另一个变量B1</td><td>分组B</td>"
            "<td>该独立数值已被OCR漏掉</td></tr></table>"
        ),
    }
    words = [
        _word("分组B", block_no=0, line_no=0),
        _word("1个独立数值", block_no=1, line_no=0),
    ]
    result = audit_table(block, words)
    missing = [
        issue for issue in result["content_issues"]
        if issue["code"] == "numeric_missing"
    ]
    assert len(missing) == 1
    assert "'1'" in missing[0]["detail"]


# ---------------------------------------------------------------------------
# 18. locale 小数点/逗号等价，但真正移位仍须告警
# ---------------------------------------------------------------------------
def test_decimal_dot_and_short_decimal_comma_are_audit_equivalent():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>3,2</td><td>0,47</td></tr></table>",
    }
    result = audit_table(
        block,
        [
            _word("3.2", line_no=0),
            _word("0.47", line_no=1),
        ],
    )
    assert not any(
        issue["severity"] == "high" for issue in result["content_issues"]
    )


# ---------------------------------------------------------------------------
# 19. 双侧破折号数字是占位符；真正的负数仍参与对账
# ---------------------------------------------------------------------------
def test_dash_wrapped_placeholder_is_not_an_independent_number():
    placeholder = {
        "block_label": "table",
        "block_content": "<table><tr><td>−/−</td></tr></table>",
    }
    result = audit_table(placeholder, [_word("—1—")])
    assert not any(
        issue["code"] == "numeric_missing" for issue in result["content_issues"]
    )

    real_negative = {
        "block_label": "table",
        "block_content": "<table><tr><td>missing</td></tr></table>",
    }
    result_negative = audit_table(real_negative, [_word("−1")])
    assert any(
        issue["code"] == "numeric_missing"
        for issue in result_negative["content_issues"]
    )


# ---------------------------------------------------------------------------
# 20. 符号单位紧黏英文正文：μTincident → μT + incident
# ---------------------------------------------------------------------------
def test_symbol_unit_is_separated_from_attached_prose_suffix():
    block = {
        "block_label": "table",
        "block_content": (
            "<table><tr><td>Ermsmax at 1 μT incident B1 field</td></tr></table>"
        ),
    }
    result = audit_table(block, [_word("Ermsmax at 1μTincident B1 field")])
    assert not any(
        issue["code"] == "unit_missing" for issue in result["content_issues"]
    )


# ---------------------------------------------------------------------------
# 21. Annex O/0 只在引用标签 + 同 cell 局部上下文双证据时等价
# ---------------------------------------------------------------------------
def test_annex_o_source_zero_glyph_is_not_a_numeric_missing():
    block = {
        "block_label": "table",
        "block_content": (
            "<table><tr><td>采用ISO 10974：2012附录O中的性能。</td></tr></table>"
        ),
    }
    result = audit_table(block, [_word("采用ISO 10974：2012附录0中的性能。")])
    assert not any(
        issue["code"] == "numeric_missing" and "'0'" in issue["detail"]
        for issue in result["content_issues"]
    )


def test_o_zero_equivalence_does_not_hide_real_numeric_zero():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>温度O度（OCR错误）</td></tr></table>",
    }
    result = audit_table(block, [_word("温度0度（真实数值）")])
    assert any(
        issue["code"] == "numeric_missing" and "'0'" in issue["detail"]
        for issue in result["content_issues"]
    )


# ---------------------------------------------------------------------------
# 22. 表内 LaTeX 裸比较号不能被 HTML 解析器误吞
# ---------------------------------------------------------------------------
def test_inline_math_less_than_is_preserved_inside_html_cell():
    table = parse_table_html(
        "<table><tr><td>$10<l<63$</td><td>next</td></tr></table>"
    )
    assert table.n_rows == 1
    assert table.n_cols == 2
    assert [cell.text for cell in table.cells] == ["$10<l<63$", "next"]


# ---------------------------------------------------------------------------
# 23. ISO10974-CN 表 A.3：源层公式 l/1 与科学记数排版差异
# ---------------------------------------------------------------------------
def test_formula_glyph_and_scientific_notation_reconciliation_is_evidence_gated():
    block = {
        "block_label": "table",
        "block_content": (
            "<table>"
            r"<tr><td>$L_x=f(r,l)^d$</td></tr>"
            r"<tr><td>$L_x=16{,}0\times10^{-2}l$</td></tr>"
            r"<tr><td>$10<l<63$</td></tr>"
            r"<tr><td>$L_x=8{,}87\times10^{-2}l+7{,}13\times10^{-1}$</td></tr>"
            r"<tr><td>$L_x=\frac{l^2}{2\pi}\times10^2$</td></tr>"
            r"<tr><td>$L_x=0{,}5\pi a r\times10^2$</td></tr>"
            "</table>"
        ),
    }
    words = [
        _word("Lx=f(r,1)d", block_no=0, line_no=0),
        _word("=16,0 E-2×1", block_no=1, line_no=0),
        _word("10<1<63", block_no=2, line_no=0),
        _word("=8,87 E-2×1+7,13 E-1", block_no=3, line_no=0),
        _word("=12/2π×1 E+2", block_no=4, line_no=0),
        _word("=0,5×π×a×r×1E+2", block_no=5, line_no=0),
    ]
    result = audit_table(block, words)
    assert not any(
        issue["severity"] == "high" for issue in result["content_issues"]
    )
    assert any(
        issue["code"] == "source_formula_glyph_ambiguous"
        and issue["severity"] == "warning"
        for issue in result["content_issues"]
    )


def test_single_l_one_disagreement_keeps_real_numeric_missing():
    block = {
        "block_label": "table",
        "block_content": "<table><tr><td>$x=l$</td></tr></table>",
    }
    result = audit_table(block, [_word("x=1")])
    assert any(
        issue["code"] == "numeric_missing" and "'1'" in issue["detail"]
        for issue in result["content_issues"]
    )
