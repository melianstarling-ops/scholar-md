from scripts.pipelines.textbooks.check_annotations import check_one, _block_under


def test_cat1_render_error_passes_when_page_clean():
    a = {"category": 1, "page": 31, "bbox": [0, 0, 10, 10], "note": ""}
    r = check_one(a, page_md="", blocks=[], error_pages=set())
    assert r["ok"] is True


def test_cat1_render_error_fails_when_page_still_red():
    a = {"category": 1, "page": 48, "bbox": [0, 0, 10, 10], "note": ""}
    r = check_one(a, page_md="", blocks=[], error_pages={48})
    assert r["ok"] is False


def test_cat3_missing_content_passes_when_now_present():
    a = {"category": 3, "page": 5, "bbox": [0, 0, 10, 10], "note": "unit vectors"}
    r = check_one(a, page_md="text with unit vectors here", blocks=[], error_pages=set())
    assert r["ok"] is True


def test_cat3_missing_content_fails_when_still_absent():
    a = {"category": 3, "page": 5, "bbox": [0, 0, 10, 10], "note": "unit vectors"}
    r = check_one(a, page_md="unrelated body", blocks=[], error_pages=set())
    assert r["ok"] is False


def test_cat4_wrong_label_checks_block_under_bbox():
    blocks = [{"block_id": 7, "block_label": "text", "block_bbox": [10, 10, 90, 90]}]
    # 人工标注:这块应是 display_formula(note),但当前 label 是 text → 回归应失败
    a = {"category": 4, "page": 9, "bbox": [20, 20, 80, 80], "note": "display_formula"}
    r = check_one(a, page_md="", blocks=blocks, error_pages=set())
    assert r["ok"] is False
    # 若当前 label 已改成期望值 → 通过
    blocks[0]["block_label"] = "display_formula"
    assert check_one(a, page_md="", blocks=blocks, error_pages=set())["ok"] is True


def test_cat2_and_cat5_are_manual_only():
    for cat in (2, 5):
        a = {"category": cat, "page": 1, "bbox": [0, 0, 10, 10], "note": "x"}
        assert check_one(a, page_md="", blocks=[], error_pages=set())["ok"] is None


def test_block_under_picks_center_containing_block():
    blocks = [
        {"block_id": 1, "block_bbox": [0, 0, 50, 50]},
        {"block_id": 2, "block_bbox": [100, 100, 200, 200]},
    ]
    # 标注框中心 (150,150) 落在 block 2 内
    assert _block_under([120, 120, 180, 180], blocks)["block_id"] == 2
