import json
import os

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import debug_view as dv
from scripts.pipelines.textbooks.vision_repair import content_fingerprint


def test_build_payloads_applies_corrections(tmp_path):
    doc_dir = tmp_path / "book"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(str(work), cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                                150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "accepted"}]}
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(str(doc_dir), pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert "good" in pages[0]["md"]
    assert "bad" not in pages[0]["md"]


def test_build_payloads_does_not_apply_pending_correction(tmp_path):
    doc_dir = tmp_path / "book"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(str(work), cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                                150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(str(doc_dir), pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert "bad" in pages[0]["md"]                  # 人工确认门:待审不生效
    assert "good" not in pages[0]["md"]


def test_build_payloads_unaffected_when_no_corrections_file(tmp_path):
    doc_dir = tmp_path / "book"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": "$$ untouched $$"}]}, f)
    cp.save_manifest(str(work), cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                                150, "A"))

    stem, pages = dv.build_payloads(str(doc_dir), pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert "untouched" in pages[0]["md"]


def test_build_payloads_attaches_pending_correction_preview(tmp_path):
    doc_dir = tmp_path / "book"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(str(work), cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                                150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$", "confidence": "high",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(str(doc_dir), pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    b = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert b["correction"]["status"] == "pending"
    assert b["correction"]["corrected_latex"] == "$$ good $$"


import json as _json


def test_handle_post_corrections_updates_status(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        _json.dump({"stem": "book", "corrections": [
            {"page": 1, "block_id": 5, "status": "pending"}]}, f)

    status, body = dv.handle_post(str(doc_dir), "book", "/corrections",
                                  _json.dumps({"page": 1, "block_id": 5, "status": "accepted"}))

    assert status == 200
    with open(doc_dir / "book_corrections.json", encoding="utf-8") as f:
        data = _json.load(f)
    assert data["corrections"][0]["status"] == "accepted"


def test_handle_post_corrections_404_when_no_match(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        _json.dump({"stem": "book", "corrections": []}, f)

    status, body = dv.handle_post(str(doc_dir), "book", "/corrections",
                                  _json.dumps({"page": 1, "block_id": 5, "status": "accepted"}))
    assert status == 404


def test_handle_post_other_path_writes_annotations_file(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    status, body = dv.handle_post(str(doc_dir), "book", "/annotations", '{"a":1}')
    assert status == 200
    assert (doc_dir / "book_annotations.json").read_text(encoding="utf-8") == '{"a":1}'


import base64 as _b64


def test_build_payloads_attaches_crop_photo_to_correction(tmp_path):
    doc_dir = tmp_path / "book"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(str(work), cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                                150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        json.dump(corrections, f)
    crops_dir = doc_dir / "book_repair" / "crops"
    os.makedirs(crops_dir, exist_ok=True)
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    (crops_dir / "page_0001_block_5.png").write_bytes(png_bytes)

    stem, pages = dv.build_payloads(str(doc_dir), pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    b = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert b["correction"]["crop_b64"] == _b64.b64encode(png_bytes).decode()


def test_build_payloads_correction_has_no_crop_b64_when_file_missing(tmp_path):
    doc_dir = tmp_path / "book"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(str(work), cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                                150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(str(doc_dir), pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    b = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert b["correction"].get("crop_b64", "") == ""


def test_handle_post_reassemble_runs_when_dirty(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    calls = []
    state = {"dirty": True}
    status, body = dv.handle_post(
        str(doc_dir), "book", "/reassemble", "",
        state=state, reassemble_fn=lambda: calls.append(1))
    assert status == 200
    assert calls == [1]                 # dirty → 跑
    assert state["dirty"] is False      # 跑完清脏


def test_handle_post_reassemble_skips_when_clean(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    calls = []
    state = {"dirty": False}
    status, body = dv.handle_post(
        str(doc_dir), "book", "/reassemble", "",
        state=state, reassemble_fn=lambda: calls.append(1))
    assert status == 200
    assert calls == []                  # 无脏 → 秒回不跑


def test_handle_post_corrections_sets_dirty(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        _json.dump({"stem": "book", "corrections": [
            {"page": 1, "block_id": 5, "status": "pending"}]}, f)
    state = {"dirty": False}
    status, body = dv.handle_post(
        str(doc_dir), "book", "/corrections",
        _json.dumps({"page": 1, "block_id": 5, "status": "accepted"}),
        state=state)
    assert status == 200
    assert state["dirty"] is True       # 采纳成功 → 置脏
