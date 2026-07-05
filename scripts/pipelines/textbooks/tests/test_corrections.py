import json
import os

from scripts.pipelines.textbooks.corrections import apply_corrections, load_corrections
from scripts.pipelines.textbooks.vision_repair import content_fingerprint


def _block(block_id, content):
    return {"block_id": block_id, "block_label": "display_formula", "block_content": content}


def _correction(page, block_id, corrected_latex, fingerprint, status="accepted"):
    return {"page": page, "block_id": block_id, "corrected_latex": corrected_latex,
            "content_fingerprint": fingerprint, "status": status}


def test_apply_corrections_replaces_matching_accepted_block_content():
    original = "$$ c\\Delta z=\\frac{a}{c^{\\prime}} $$"
    blocks = [_block(3, original)]
    corrections = [_correction(49, 3, "$$ fixed $$", content_fingerprint(original))]
    out = apply_corrections(blocks, page=49, corrections=corrections)
    assert out[0]["block_content"] == "$$ fixed $$"


def test_apply_corrections_skips_pending_status():
    original = "$$ c\\Delta z=\\frac{a}{c^{\\prime}} $$"
    blocks = [_block(3, original)]
    corrections = [_correction(49, 3, "$$ fixed $$", content_fingerprint(original),
                                status="pending")]
    out = apply_corrections(blocks, page=49, corrections=corrections)
    assert out[0]["block_content"] == original          # 人工确认门:待审不生效


def test_apply_corrections_skips_rejected_status():
    original = "$$ c\\Delta z=\\frac{a}{c^{\\prime}} $$"
    blocks = [_block(3, original)]
    corrections = [_correction(49, 3, "$$ fixed $$", content_fingerprint(original),
                                status="rejected")]
    out = apply_corrections(blocks, page=49, corrections=corrections)
    assert out[0]["block_content"] == original


def test_apply_corrections_skips_when_status_missing():
    # 旧 corrections.json(加 status 字段之前产出)默认视为未确认,不自动生效
    original = "$$ c\\Delta z=\\frac{a}{c^{\\prime}} $$"
    blocks = [_block(3, original)]
    corrections = [{"page": 49, "block_id": 3, "corrected_latex": "$$ fixed $$",
                    "content_fingerprint": content_fingerprint(original)}]
    out = apply_corrections(blocks, page=49, corrections=corrections)
    assert out[0]["block_content"] == original


def test_apply_corrections_skips_on_fingerprint_mismatch():
    original = "$$ c\\Delta z=\\frac{a}{c^{\\prime}} $$"
    blocks = [_block(3, original)]
    corrections = [_correction(49, 3, "$$ fixed $$", "stale-does-not-match")]
    out = apply_corrections(blocks, page=49, corrections=corrections)
    assert out[0]["block_content"] == original         # res.json 漂移,宁可不修


def test_apply_corrections_leaves_unmatched_blocks_untouched():
    original = "$$ E=mc^2 $$"
    blocks = [_block(9, original)]
    corrections = [_correction(49, 3, "$$ fixed $$", content_fingerprint(original))]
    out = apply_corrections(blocks, page=49, corrections=corrections)
    assert out[0]["block_content"] == original


def test_apply_corrections_ignores_corrections_for_other_pages():
    original = "$$ c\\Delta z=\\frac{a}{c^{\\prime}} $$"
    blocks = [_block(3, original)]
    corrections = [_correction(50, 3, "$$ fixed $$", content_fingerprint(original))]
    out = apply_corrections(blocks, page=49, corrections=corrections)
    assert out[0]["block_content"] == original


def test_apply_corrections_does_not_mutate_input_blocks():
    original = "$$ c\\Delta z=\\frac{a}{c^{\\prime}} $$"
    block = _block(3, original)
    blocks = [block]
    corrections = [_correction(49, 3, "$$ fixed $$", content_fingerprint(original))]
    apply_corrections(blocks, page=49, corrections=corrections)
    assert block["block_content"] == original          # 原 dict 未被原地改写


def test_load_corrections_returns_empty_list_when_file_missing(tmp_path):
    assert load_corrections(str(tmp_path / "book")) == []


def test_load_corrections_reads_existing_file(tmp_path):
    doc_dir = tmp_path / "book"
    doc_dir.mkdir()
    payload = {"stem": "book", "corrections": [{"page": 1, "block_id": 1}]}
    (doc_dir / "book_corrections.json").write_text(json.dumps(payload), encoding="utf-8")
    assert load_corrections(str(doc_dir)) == [{"page": 1, "block_id": 1}]


from scripts.pipelines.textbooks.corrections import set_correction_status


def _write_corrections_file(doc_dir, stem, corrections):
    os.makedirs(doc_dir, exist_ok=True)
    path = os.path.join(doc_dir, f"{stem}_corrections.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"stem": stem, "corrections": corrections}, f)
    return path


def test_set_correction_status_updates_matching_record(tmp_path):
    doc_dir = str(tmp_path / "book")
    path = _write_corrections_file(doc_dir, "book", [
        {"page": 49, "block_id": 3, "status": "pending"}])
    ok = set_correction_status(doc_dir, page=49, block_id=3, status="accepted")
    assert ok is True
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["corrections"][0]["status"] == "accepted"


def test_set_correction_status_returns_false_when_no_match(tmp_path):
    doc_dir = str(tmp_path / "book")
    _write_corrections_file(doc_dir, "book", [{"page": 49, "block_id": 3, "status": "pending"}])
    ok = set_correction_status(doc_dir, page=49, block_id=999, status="accepted")
    assert ok is False


def test_set_correction_status_returns_false_when_file_missing(tmp_path):
    ok = set_correction_status(str(tmp_path / "book"), page=1, block_id=1, status="accepted")
    assert ok is False


def test_set_correction_status_rejects_invalid_status(tmp_path):
    doc_dir = str(tmp_path / "book")
    _write_corrections_file(doc_dir, "book", [{"page": 49, "block_id": 3, "status": "pending"}])
    import pytest
    with pytest.raises(ValueError):
        set_correction_status(doc_dir, page=49, block_id=3, status="maybe")
