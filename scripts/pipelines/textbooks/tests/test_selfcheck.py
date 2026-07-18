import json
import os

from scripts.pipelines.textbooks.selfcheck import (
    block_coverage, katex_incompat_scan, detect_column_layout, aggregate_warnings,
    scan_formula_suspicions, summarize_suspicions,
    build_source_audit_field, build_ocr_degeneration_field,
    inline_math_delimiter_ws_scan,
)


def test_summarize_suspicions_counts_by_op():
    md = r"$$ \oint A $$" + "\n" + r"$$ \oint B $$" + "\n" + r"$$ \lim C $$"
    assert summarize_suspicions(md) == [{"op": r"\oint", "count": 2}, {"op": r"\lim", "count": 1}]


def test_suspicion_flags_bare_oint():
    # 闭合积分裸用(无下标/围道) → 疑似漏识别(用户 p48 1.55 那类)
    sus = scan_formula_suspicions(r"$$ c\Delta z=\varepsilon\oint\vec{E}\cdot d s $$")
    assert [s["op"] for s in sus] == [r"\oint"]
    assert sus[0]["kind"] == "bare_op"


def test_suspicion_flags_frac_primed_denominator():
    # 引擎把积分围道 c'/曲面 s' 误当 \frac 分母(p49/p50/p53 结构错)
    sus = scan_formula_suspicions(r"c=\frac{\varepsilon\oint\vec{E}\cdot dl^{\prime}}{c^{\prime}}")
    kinds = [s["kind"] for s in sus]
    assert "frac_primed_denom" in kinds
    frac = next(s for s in sus if s["kind"] == "frac_primed_denom")
    assert "c'" in frac["detail"]          # 人可读:点名是 c'


def test_suspicion_ignores_normal_frac_denominator():
    # 合法分母(表达式/不带撇单字母)不得误报结构可疑
    assert not any(s["kind"] == "frac_primed_denom"
                   for s in scan_formula_suspicions(r"\frac{a+b}{V(z,t)} + \frac{x}{y}"))


def test_suspicion_ignores_oint_with_subscript():
    assert scan_formula_suspicions(r"\oint_{c'}\vec{E}\cdot dl") == []


def test_suspicion_ignores_oint_with_limits():
    assert scan_formula_suspicions(r"\oint\limits_{c}\vec{E}\cdot dl") == []


def test_suspicion_flags_bare_int_and_lim():
    ops = [s["op"] for s in scan_formula_suspicions(r"\int f\,dx + \lim g")]
    assert r"\int" in ops and r"\lim" in ops


def test_suspicion_ignores_definite_int_and_sum():
    assert scan_formula_suspicions(r"\int_a^b f\,dx + \sum_{i=1}^n a_i") == []


def test_suspicion_word_boundary_no_false_hit():
    # \intercal / \infty 等不得被 \int 前缀误伤
    assert scan_formula_suspicions(r"A^\intercal + \infty") == []


def test_suspicion_reports_each_occurrence():
    sus = scan_formula_suspicions(r"\oint A + \oint B")
    assert len(sus) == 2 and all(s["op"] == r"\oint" for s in sus)


def test_all_ordered_blocks_covered():
    blocks = [
        {"block_label": "text", "block_content": "alpha beta", "block_order": 1},
        {"block_label": "header", "block_content": "IGNORED", "block_order": None},
    ]
    md = "alpha beta\n"
    rep = block_coverage(blocks, md)
    assert rep["total"] == 1          # order=None 不计
    assert rep["in_md"] == 1
    assert rep["missing"] == []


def test_detects_missing_block():
    blocks = [
        {"block_label": "text", "block_content": "present text", "block_order": 1},
        {"block_label": "text", "block_content": "LOST paragraph", "block_order": 2},
    ]
    md = "present text\n"
    rep = block_coverage(blocks, md)
    assert rep["in_md"] == 1
    assert any("LOST" in m for m in rep["missing"])


def test_formula_number_absorbed_into_tag_not_missing():
    # reconstruct 把 formula_number "(5.31)" 吸收成 \tag{5.31}；Tier0 不应误报 missing
    blocks = [
        {"block_label": "display_formula", "block_content": "$$ x=1 $$", "block_order": 1},
        {"block_label": "formula_number", "block_content": "(5.31)", "block_order": 2},
    ]
    md = "$$ x=1 \\tag{5.31} $$\n"
    rep = block_coverage(blocks, md)
    assert rep["missing"] == []
    assert rep["in_md"] == rep["total"] == 2


def test_sanitized_fullwidth_formula_number_not_missing():
    blocks = [
        {"block_label": "display_formula", "block_content": "$$ x=1 $$", "block_order": 1},
        {"block_label": "formula_number", "block_content": "（1.20）", "block_order": 2},
    ]
    md = r"$$ x=1 \tag{\text{（}1.20\text{）}} $$"
    rep = block_coverage(blocks, md)
    assert rep["missing"] == []


def test_detached_fullwidth_formula_number_kept_raw_not_missing():
    blocks = [{"block_label": "formula_number", "block_content": "（6-1）", "block_order": 1}]
    rep = block_coverage(blocks, "（6-1）")
    assert rep["missing"] == []


def test_block_coverage_compares_sanitized_display_formula_probe():
    blocks = [{
        "block_label": "display_formula",
        "block_content": r"$$ \mathrm{\boldmath~r~}>\sqrt{\mathrm{A}1} $$",
        "block_order": 1,
    }]
    md = r"$$ \mathrm{~r~}>\sqrt{\mathrm{A}1} $$"
    rep = block_coverage(blocks, md)
    assert rep["missing"] == []
    assert rep["in_md"] == 1


def test_block_coverage_compares_sanitized_inline_math_probe():
    blocks = [{
        "block_label": "text",
        "block_content": "Alodine $ ^{®} $ and Iridite $ ^{™} $ are trade names",
        "block_order": 1,
    }]
    md = r"Alodine $ ^{\text{\textregistered}} $ and Iridite $ ^{\text{TM}} $ are trade names"
    rep = block_coverage(blocks, md)
    assert rep["missing"] == []
    assert rep["in_md"] == 1


def test_block_coverage_compares_sanitized_title_inline_math_probe():
    blocks = [{
        "block_label": "paragraph_title",
        "block_content": "12.4 CE Analyst $ ^{™} $",
        "block_order": 1,
    }]
    md = r"## 12.4 CE Analyst $ ^{\text{TM}} $"
    rep = block_coverage(blocks, md)
    assert rep["missing"] == []


def test_empty_block_content_not_counted_as_missing():
    # block_content 为空(如 seal/装饰性 text 块)时探针恒为空字符串,不应误判为 missing(假阳性)
    blocks = [
        {"block_label": "text", "block_content": "present text", "block_order": 1},
        {"block_label": "seal", "block_content": "", "block_order": 2},
    ]
    md = "present text\n"
    rep = block_coverage(blocks, md)
    assert rep["missing"] == []


def test_skipped_empty_counted_and_totals_reconcile():
    # skipped_empty 字段:空内容块(如 block_content='' 的 text/seal)单独计数,
    # 使 total == in_md + len(missing) + skipped_empty 恒成立(账目自洽,不留隐藏数字)
    blocks = [
        {"block_label": "text", "block_content": "present text", "block_order": 1},
        {"block_label": "text", "block_content": "", "block_order": 2},
        {"block_label": "seal", "block_content": "", "block_order": 3},
    ]
    md = "present text\n"
    rep = block_coverage(blocks, md)
    assert rep["skipped_empty"] == 2
    assert rep["total"] == rep["in_md"] + len(rep["missing"]) + rep["skipped_empty"]


def test_katex_scan_detects_residual():
    # 清洗遗漏/回归时,Tier0 lint 应检出残留的不兼容命令
    hits = katex_incompat_scan(r"$$ \int\displaylimits_{S} x $$")
    assert r"\displaylimits" in hits


def test_katex_scan_clean_returns_empty():
    assert katex_incompat_scan(r"$$ \int_{S} \displaystyle x $$") == []


# ---------------------------------------------------------------------------
# Task A:inline_math_delimiter_ws 回归哨兵。归一化(reconstruct sanitize 链)生效
# 后 md 里不应再残留 `$ X $` 形态(开 $ 后/闭 $ 前带空白的行内公式);该检测项独立
# 于 reconstruct 复查一遍,充当"清洗遗漏/回归"报警(同 katex_incompat_scan 的定位)。
# ---------------------------------------------------------------------------

def test_inline_math_delimiter_ws_detects_leading_and_trailing_space():
    md = "场强 $ B_0 $ 恒定，另见 $C_1 $ 与 $ D_2$。"
    result = inline_math_delimiter_ws_scan(md)
    assert result["count"] == 3
    assert "$ B_0 $" in result["samples"]
    assert "$C_1 $" in result["samples"]
    assert "$ D_2$" in result["samples"]


def test_inline_math_delimiter_ws_clean_md_returns_zero():
    md = r"场强 $B_0$ 恒定，公式 $$ B_0 = 1 $$ 不受影响。"
    result = inline_math_delimiter_ws_scan(md)
    assert result == {"count": 0, "samples": []}


def test_inline_math_delimiter_ws_samples_capped_at_five():
    md = " ".join(f"$ x{i} $" for i in range(8))
    result = inline_math_delimiter_ws_scan(md)
    assert result["count"] == 8
    assert len(result["samples"]) == 5


def test_inline_math_delimiter_ws_ignores_display_math():
    md = r"$$ a = 1 $$"
    assert inline_math_delimiter_ws_scan(md) == {"count": 0, "samples": []}


def test_detect_column_layout_true_for_side_by_side_blocks():
    # 两块 y 区间大幅重叠、x 区间完全分离(左右并排)→ 判定双栏
    blocks = [
        {"block_label": "text", "block_order": 1, "block_bbox": [0, 100, 200, 300]},
        {"block_label": "text", "block_order": 2, "block_bbox": [400, 110, 600, 290]},
    ]
    assert detect_column_layout(blocks) is True


def test_detect_column_layout_false_for_single_column():
    # 正常单栏:纵向排列,x 区间重叠
    blocks = [
        {"block_label": "text", "block_order": 1, "block_bbox": [0, 100, 600, 300]},
        {"block_label": "text", "block_order": 2, "block_bbox": [0, 350, 600, 550]},
    ]
    assert detect_column_layout(blocks) is False


def test_detect_column_layout_ignores_order_none_blocks():
    # header/number 等 order=None 块不参与判定(页眉页脚天然左右分布,不代表双栏正文)
    blocks = [
        {"block_label": "header", "block_order": None, "block_bbox": [0, 0, 100, 20]},
        {"block_label": "number", "block_order": None, "block_bbox": [500, 0, 600, 20]},
    ]
    assert detect_column_layout(blocks) is False


def test_detect_column_layout_ignores_non_text_labels():
    # image/figure_title 等不参与判定,只看 text/display_formula
    blocks = [
        {"block_label": "image", "block_order": None, "block_bbox": [0, 100, 200, 300]},
        {"block_label": "figure_title", "block_order": None, "block_bbox": [400, 110, 600, 290]},
    ]
    assert detect_column_layout(blocks) is False


def test_aggregate_warnings_groups_unhandled_labels_with_count():
    warnings = [
        {"kind": "unhandled_label", "label": "mystery", "page": 1, "block_id": 1, "sample": "a"},
        {"kind": "unhandled_label", "label": "mystery", "page": 5, "block_id": 2, "sample": "b"},
    ]
    result = aggregate_warnings(warnings)
    assert result["unhandled_labels"] == {"mystery": {"count": 2, "sample": "a"}}


def test_aggregate_warnings_keeps_visual_warnings_as_list():
    warnings = [
        {"kind": "visual_missing_bbox", "label": "image", "page": 3, "block_id": 9, "sample": ""},
        {"kind": "visual_unexpected_content", "label": "chart", "page": 4, "block_id": 1, "sample": "x"},
    ]
    result = aggregate_warnings(warnings)
    assert result["visual_warnings"] == warnings
    assert result["unhandled_labels"] == {}


def test_aggregate_warnings_empty_input():
    assert aggregate_warnings([]) == {"unhandled_labels": {}, "visual_warnings": []}


def test_detect_column_layout_malformed_bbox_does_not_raise():
    # 一个正常 4 元素 bbox + 一个畸形 2 元素 bbox 混在候选块里,不应崩溃
    # (畸形 bbox 应被当作缺失 bbox 一样排除出候选集)
    blocks = [
        {"block_label": "text", "block_order": 1, "block_bbox": [0, 100, 200, 300]},
        {"block_label": "text", "block_order": 2, "block_bbox": [400, 110]},
    ]
    result = detect_column_layout(blocks)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Task 10:selfcheck 的 source_audit / ocr_degeneration 紧凑字段(计划 §7.2)。
# 三个函数均独立读取落盘的 <stem>_source_audit.json(不复用 convert.py 的内部
# freshness 判定——两者是独立关注点),入参为 (report_path, pdf_fingerprint,
# dpi, schema_version)。
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 2
_FP = {"size_bytes": 100, "page_count": 3}
_DPI = 150


def _write_report(path: str, report: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f)


def _fresh_report(**summary_overrides) -> dict:
    summary = {
        "status": "SUSPECT", "pages": 3, "scorable_pages": 3,
        "suspect_pages": [2],
        "adoption": {"prose_blocks": 10, "adopted": 8, "fallback_ocr": 2,
                     "fallback_reasons": {}},
        "issue_counts": {"numeric_mismatch": 1},
    }
    summary.update(summary_overrides)
    return {
        "schema_version": _SCHEMA_VERSION,
        "stem": "book", "route": "B", "born_digital_mode": "hybrid",
        "pdf_fingerprint": dict(_FP),
        "ocr_fingerprint": {"dpi": _DPI, "page_count": 3},
        "threshold_profile": "route_b_v1_uncalibrated",
        "adoption_source": "recorded",
        "summary": summary,
        "pages": [
            {"page": 1, "prose_audit": {"metrics": {"ngram_repetition_score": 0.01},
                                        "issues": []}},
            {"page": 2, "prose_audit": {"metrics": {"ngram_repetition_score": 0.6},
                                        "issues": [{"code": "ocr_degeneration",
                                                    "block_id": None, "detail": "x"}]}},
            {"page": 3, "prose_audit": {"metrics": {"ngram_repetition_score": 0.03},
                                        "issues": []}},
        ],
    }


def test_build_source_audit_field_reads_compact_summary_from_real_report(tmp_path):
    path = str(tmp_path / "book_source_audit.json")
    _write_report(path, _fresh_report())
    field = build_source_audit_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field == {
        "status": "SUSPECT",
        "suspect_pages": [2],
        "adoption": {"adopted": 8, "fallback_ocr": 2},
        "issue_counts": {"numeric_mismatch": 1},
        "report": "book_source_audit.json",
    }


def test_build_source_audit_field_does_not_copy_page_level_detail(tmp_path):
    # 结构断言:紧凑字段绝不把逐页 pages 数组复制进来。
    path = str(tmp_path / "book_source_audit.json")
    _write_report(path, _fresh_report())
    field = build_source_audit_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert "pages" not in field
    assert "blocks" not in field


def test_build_source_audit_field_missing_report_gets_distinct_issue_code(tmp_path):
    path = str(tmp_path / "missing_source_audit.json")   # 从未写过
    field = build_source_audit_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field["issue_counts"] == {"audit_report_missing": 1}
    assert field["status"] == "UNSCORABLE"
    assert field["report"] == "missing_source_audit.json"


def test_build_source_audit_field_corrupt_report_gets_distinct_issue_code(tmp_path):
    path = str(tmp_path / "book_source_audit.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"schema_version": 2, "summary": {')       # 半截 JSON,解析失败
    field = build_source_audit_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field["issue_counts"] == {"audit_report_corrupt": 1}
    assert field["status"] == "UNSCORABLE"


def test_build_source_audit_field_stale_fingerprint_gets_distinct_issue_code(tmp_path):
    path = str(tmp_path / "book_source_audit.json")
    stale = _fresh_report()
    stale["pdf_fingerprint"]["page_count"] = 999          # 与当前 _FP 不一致 → 过期
    _write_report(path, stale)
    field = build_source_audit_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field["issue_counts"] == {"audit_report_stale": 1}
    assert field["status"] == "UNSCORABLE"


def test_build_source_audit_field_stale_schema_version_gets_distinct_issue_code(tmp_path):
    path = str(tmp_path / "book_source_audit.json")
    stale = _fresh_report()
    stale["schema_version"] = 1
    _write_report(path, stale)
    field = build_source_audit_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field["issue_counts"] == {"audit_report_stale": 1}


def test_build_ocr_degeneration_field_extracts_peak_and_flagged_pages(tmp_path):
    path = str(tmp_path / "book_source_audit.json")
    _write_report(path, _fresh_report())
    field = build_ocr_degeneration_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field == {"max_ngram_repetition": 0.6, "flagged_pages": [2]}


def test_build_ocr_degeneration_field_none_when_report_missing(tmp_path):
    # audit 缺失时的缺席语义:明确为 None,不编造/不置零蒙混。
    path = str(tmp_path / "missing_source_audit.json")
    field = build_ocr_degeneration_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field is None


def test_build_ocr_degeneration_field_none_when_report_stale(tmp_path):
    path = str(tmp_path / "book_source_audit.json")
    stale = _fresh_report()
    stale["ocr_fingerprint"]["dpi"] = 999
    _write_report(path, stale)
    field = build_ocr_degeneration_field(path, _FP, _DPI, _SCHEMA_VERSION)
    assert field is None
