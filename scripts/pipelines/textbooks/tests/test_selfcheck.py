from scripts.pipelines.textbooks.selfcheck import block_coverage


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
