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
