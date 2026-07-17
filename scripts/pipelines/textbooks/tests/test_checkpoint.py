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
    assert loaded["attempts_by_page"] == {}
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


def test_load_page_result_returns_metadata(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 200, "model_settings": {"a": 1},
                   "parsing_res_list": [{"block_order": 0}]}, f)
    res = cp.load_page_result(work, 1)
    assert res["width"] == 100
    assert res["height"] == 200
    assert res["model_settings"] == {"a": 1}
    assert res["parsing_res_list"] == [{"block_order": 0}]


def test_load_page_result_missing(tmp_path):
    assert cp.load_page_result(str(tmp_path / "_work"), 9) == {}


def test_load_page_result_empty_page(tmp_path):
    work = str(tmp_path / "_work")
    cp.write_empty_page(work, 3)
    assert cp.load_page_result(work, 3) == {"parsing_res_list": []}


def test_load_page_result_corrupt_json(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        f.write('{"parsing_res_list": [')   # 半截
    assert cp.load_page_result(work, 1) == {}


def test_load_page_blocks_delegates_to_load_page_result(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [{"block_order": 0}])
    assert cp.load_page_blocks(work, 1) == cp.load_page_result(work, 1)["parsing_res_list"]


def test_pages_todo(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [])
    _write_res(work, 3, [])
    assert cp.pages_todo(work, 4) == [2, 4]


def test_record_failure(tmp_path):
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.record_failure(m, 5, "CUDA oom", "page-exception")
    assert m["failed_pages"] == [{"page": 5, "error": "CUDA oom", "kind": "page-exception"}]


def test_set_in_progress_sets_page():
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.set_in_progress(m, 7)
    assert m["in_progress"] == 7
    cp.set_in_progress(m, 8)
    assert m["in_progress"] == 8


def test_clear_in_progress():
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.set_in_progress(m, 7)
    cp.clear_in_progress(m)
    assert m["in_progress"] is None


def test_resolve_poison_marks_failed_at_threshold(tmp_path):
    work = str(tmp_path / "_work"); os.makedirs(work)
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    m["in_progress"] = 2
    m["attempts_by_page"] = {"2": cp.MAX_HARD_ATTEMPTS - 1}   # 差一次到阈值
    cp.resolve_poison(m, work)                                # 这次再崩 → 达阈值
    assert m["in_progress"] is None
    assert m["attempts_by_page"]["2"] == cp.MAX_HARD_ATTEMPTS
    assert m["failed_pages"] == [{"page": 2, "error": "process killed repeatedly",
                                  "kind": "process-killed"}]


def test_resolve_poison_increments_under_threshold(tmp_path):
    work = str(tmp_path / "_work"); os.makedirs(work)
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    m["in_progress"] = 2                                      # attempts_by_page 为空
    cp.resolve_poison(m, work)
    assert m["attempts_by_page"]["2"] == 1
    assert m["in_progress"] is None                          # 清除,循环会重试
    assert m["failed_pages"] == []


def test_resolve_poison_page_actually_done(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 2, [])                                   # 崩在写完之后
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    m["in_progress"] = 2
    cp.resolve_poison(m, work)
    assert m["in_progress"] is None                          # 已完成 → 仅清标记
    assert m["failed_pages"] == []
    assert m["attempts_by_page"].get("2") is None            # 未计数


def test_resolve_poison_noop_when_none(tmp_path):
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.resolve_poison(m, str(tmp_path))
    assert m["in_progress"] is None


def test_resolve_poison_accumulates_across_runs(tmp_path):
    # 关键回归:跨轮累积到阈值,不被其它页的 set_in_progress 重置
    work = str(tmp_path / "_work"); os.makedirs(work)
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    m["in_progress"] = 2
    cp.resolve_poison(m, work)                    # run1 崩残留 → n=1 <阈值
    assert m["attempts_by_page"]["2"] == 1 and m["failed_pages"] == []
    cp.set_in_progress(m, 2)                      # run2 循环重试该页(breadcrumb)
    cp.resolve_poison(m, work)                    # 又崩 → n=2 达阈值 → 毒页
    assert m["attempts_by_page"]["2"] == cp.MAX_HARD_ATTEMPTS
    assert [f["page"] for f in m["failed_pages"]] == [2]
