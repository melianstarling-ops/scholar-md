from scripts.pipelines.textbooks.selfcheck import (
    block_coverage, katex_incompat_scan, detect_column_layout, aggregate_warnings,
    scan_formula_suspicions, summarize_suspicions,
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
