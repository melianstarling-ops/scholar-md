import json
from pathlib import Path

import fitz
import pytest

from scripts.pipelines.textbooks import batch as bp
from scripts.pipelines.textbooks import checkpoint as cp


def _make_pdf(tmp_path, n_pages, name="book"):
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / f"{name}.pdf"
    doc.save(str(p))
    return p


def _mark_page_done(work: Path, page: int, content: str = "x") -> None:
    work.mkdir(parents=True, exist_ok=True)
    with open(cp.page_res_path(str(work), page), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "text", "block_content": content}]}, f)


def test_discover_dir_and_file_mixed_dedup(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    a = d / "A.pdf"
    a.write_bytes(b"%PDF-1.4")
    b = d / "B.pdf"
    b.write_bytes(b"%PDF-1.4")
    result = bp.discover([str(d), str(a)])   # 目录+文件混用,a 只应出现一次
    assert result == [a, b]


def test_discover_skips_non_pdf(tmp_path, capsys):
    d = tmp_path / "src"
    d.mkdir()
    (d / "notes.txt").write_text("x", encoding="utf-8")
    result = bp.discover([str(d / "notes.txt")])
    assert result == []
    assert "跳过" in capsys.readouterr().err


def test_discover_cross_dir_stem_collision_raises(tmp_path):
    d1 = tmp_path / "s1"
    d1.mkdir()
    d2 = tmp_path / "s2"
    d2.mkdir()
    (d1 / "A.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "A.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="跨目录同名"):
        bp.discover([str(d1), str(d2)])


def test_already_done_false_when_no_manifest(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    assert bp._already_done(tmp_path / "out", pdf, 150) is False


def test_already_done_true_when_all_pages_done(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    _mark_page_done(work, 2)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 150) is True


def test_already_done_false_on_dpi_mismatch(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 200) is False   # 请求 DPI 200 ≠ 记录 150


def test_already_done_false_on_source_replaced(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    # 同名文件被替换成不同页数(指纹变了)
    doc = fitz.open()
    doc.new_page(); doc.new_page(); doc.new_page()
    doc.save(str(pdf))
    assert bp._already_done(out_root, pdf, 150) is False


def test_already_done_true_when_only_poisoned_page_remains(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.record_failure(m, 2, "process killed repeatedly", "process-killed")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 150) is True     # 毒页不算"未完成"


def test_already_done_false_when_page_exception_pending(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.record_failure(m, 2, "transient", "page-exception")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 150) is False    # 瞬时失败页仍算未完成,允许重试
