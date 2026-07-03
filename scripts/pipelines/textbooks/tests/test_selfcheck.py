from scripts.pipelines.textbooks.selfcheck import block_coverage, katex_incompat_scan, detect_column_layout


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
