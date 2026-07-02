import json
import os
import fitz
from scripts.pipelines.textbooks import checkpoint as cp


def _make_pdf(tmp_path, n_pages):
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / "book.pdf"
    doc.save(str(p))
    return str(p)


def test_pdf_fingerprint(tmp_path):
    pdf = _make_pdf(tmp_path, 5)
    fp = cp.pdf_fingerprint(pdf)
    assert fp["page_count"] == 5
    assert fp["size_bytes"] == os.path.getsize(pdf)


def test_manifest_roundtrip(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    m = cp.new_manifest("book.pdf", {"page_count": 5, "size_bytes": 100}, 150, "A")
    cp.save_manifest(work, m)
    loaded = cp.load_manifest(work)
    assert loaded["fingerprint"]["page_count"] == 5
    assert loaded["dpi"] == 150
    assert loaded["route"] == "A"
    assert loaded["failed_pages"] == []
    assert loaded["in_progress"] is None
    assert loaded["restarts"] == 0
    assert "updated" in loaded


def test_load_manifest_absent(tmp_path):
    assert cp.load_manifest(str(tmp_path)) is None


def test_fingerprint_ok_matches(tmp_path):
    pdf = _make_pdf(tmp_path, 3)
    fp = cp.pdf_fingerprint(pdf)
    m = cp.new_manifest(pdf, fp, 150, "A")
    assert cp.fingerprint_ok(m, pdf, 150) is True


def test_fingerprint_ok_dpi_mismatch(tmp_path):
    pdf = _make_pdf(tmp_path, 3)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 150, "A")
    assert cp.fingerprint_ok(m, pdf, 200) is False   # DPI 变 → 失配


def test_fingerprint_ok_size_mismatch(tmp_path):
    pdf = _make_pdf(tmp_path, 3)
    m = cp.new_manifest(pdf, {"page_count": 3, "size_bytes": 999999}, 150, "A")
    assert cp.fingerprint_ok(m, pdf, 150) is False   # size 变 → 失配


def test_reset_work_dir(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    with open(os.path.join(work, "stale.json"), "w") as f:
        f.write("{}")
    cp.reset_work_dir(work)
    assert os.path.isdir(work)
    assert os.listdir(work) == []


def test_page_stem_and_res_path(tmp_path):
    assert cp.page_stem(7) == "page_0007"
    assert cp.page_res_path("/w", 7).endswith(os.path.join("/w", "page_0007_res.json").replace("/", os.sep))


def _write_res(work, page, blocks):
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, page), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": blocks}, f)


def test_is_page_done_true_false(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [{"block_order": 0}])
    assert cp.is_page_done(work, 1) is True
    assert cp.is_page_done(work, 2) is False


def test_is_page_done_corrupt_json(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        f.write('{"parsing_res_list": [')   # 半截
    assert cp.is_page_done(work, 1) is False   # 损坏 → 未完成


def test_write_empty_page_marks_done(tmp_path):
    work = str(tmp_path / "_work")
    cp.write_empty_page(work, 3)
    assert cp.is_page_done(work, 3) is True
    assert cp.load_page_blocks(work, 3) == []


def test_load_page_blocks(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [{"block_order": 0, "block_content": "hi"}])
    assert cp.load_page_blocks(work, 1) == [{"block_order": 0, "block_content": "hi"}]
    assert cp.load_page_blocks(work, 9) == []          # 缺失
    with open(cp.page_res_path(work, 2), "w") as f:
        f.write("broken")
    assert cp.load_page_blocks(work, 2) == []          # 损坏


def test_pages_todo(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [])
    _write_res(work, 3, [])
    assert cp.pages_todo(work, 4) == [2, 4]
