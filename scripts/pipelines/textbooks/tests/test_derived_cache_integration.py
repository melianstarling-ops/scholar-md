from __future__ import annotations

import json
import os

import fitz
import pytest

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import convert as cv
from scripts.pipelines.textbooks import derived_cache as dc
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.prose_adoption import AdoptionDecision


def _pdf(tmp_path, name="book.pdf", pages=2):
    path = tmp_path / name
    doc = fitz.open()
    for page in range(1, pages + 1):
        current = doc.new_page()
        current.insert_text((72, 72), f"page {page} source text")
    doc.save(path)
    doc.close()
    return str(path)


def _block(page):
    return {
        "block_id": page * 10,
        "block_order": 0,
        "block_label": "text",
        "block_content": f"page {page} content",
        "block_bbox": [0, 0, 100, 100],
    }


def _write_checkpoint(layout, pdf_path, pages=2, route="B"):
    os.makedirs(layout.work_dir, exist_ok=True)
    for page in range(1, pages + 1):
        with open(cp.page_res_path(layout.work_dir, page), "w", encoding="utf-8") as handle:
            json.dump({
                "width": 100,
                "height": 100,
                "parsing_res_list": [_block(page)],
            }, handle)
    cp.save_manifest(
        layout.work_dir,
        cp.new_manifest(pdf_path, cp.pdf_fingerprint(pdf_path), 100, route),
    )


def _write_hybrid_audit(layout, pdf_path):
    with open(layout.source_audit_path, "w", encoding="utf-8") as handle:
        json.dump({
            "schema_version": cv.AUDIT_SCHEMA_VERSION,
            "born_digital_mode": "hybrid",
            "adoption_source": "recorded",
            "pdf_fingerprint": {
                **cp.pdf_fingerprint(pdf_path),
                "sha256": dc.sha256_file(pdf_path),
            },
            "ocr_fingerprint": {"dpi": 100, "page_count": 2},
            "summary": {"issue_counts": {}},
        }, handle)


def _adopt_passthrough_calls(monkeypatch):
    calls = []

    def passthrough(self, blocks, page):
        calls.append(page)
        self.decisions_by_page[page] = [
            AdoptionDecision(
                block_id=0,
                content_source="ocr",
                reasons=["adoption_disagreement"],
                block_ned=0.5,
                adopted_text=None,
            )
        ]
        return blocks

    monkeypatch.setattr(cv._AdoptContext, "adopt_page", passthrough)
    return calls


def _baseline_md(pages=2):
    page_md = [f"page {page} content\n" for page in range(1, pages + 1)]
    return "\n\n".join(page_md) + "\n"


def test_old_hybrid_equal_migration_seeds_cache_then_only_rebuilds_affected_page(
        tmp_path, monkeypatch):
    pdf_path = _pdf(tmp_path)
    out = str(tmp_path / "out")
    layout = resolve_layout("book", out)
    _write_checkpoint(layout, pdf_path)
    _write_hybrid_audit(layout, pdf_path)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    with open(layout.md_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(_baseline_md())
    calls = _adopt_passthrough_calls(monkeypatch)

    assert cv.reassemble_md(layout, pdf_path, 100) == layout.md_path
    assert calls == [1, 2]
    assert dc.read_page_cache(layout.work_dir, 1) is not None
    assert dc.read_page_cache(layout.work_dir, 2) is not None
    with open(layout.md_path, encoding="utf-8", newline="") as handle:
        final_md = handle.read()
    assert dc.read_document_index(
        layout.work_dir,
        expected_final_sha256=dc.sha256_text(final_md),
    ) is not None

    calls.clear()
    assert cv.build_reassembled_markdown(
        layout, pdf_path, 100, affected_pages={2}) == _baseline_md()
    assert calls == [2]


def test_old_hybrid_unequal_migration_fails_without_overwriting_or_cache(
        tmp_path, monkeypatch):
    pdf_path = _pdf(tmp_path)
    out = str(tmp_path / "out")
    layout = resolve_layout("book", out)
    _write_checkpoint(layout, pdf_path)
    _write_hybrid_audit(layout, pdf_path)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    with open(layout.md_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("existing repaired truth\n")
    _adopt_passthrough_calls(monkeypatch)

    with pytest.raises(RuntimeError, match="legacy_cache_migration_unresolved"):
        cv.reassemble_md(layout, pdf_path, 100)

    assert open(layout.md_path, encoding="utf-8").read() == "existing repaired truth\n"
    assert dc.read_page_cache(layout.work_dir, 1) is None
    assert dc.read_document_index(layout.work_dir) is None


def test_old_hybrid_single_page_edit_migrates_overlay_without_overwriting_md(
        tmp_path, monkeypatch):
    pdf_path = _pdf(tmp_path)
    out = str(tmp_path / "out")
    layout = resolve_layout("book", out)
    _write_checkpoint(layout, pdf_path)
    _write_hybrid_audit(layout, pdf_path)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    repaired = _baseline_md().replace("page 1 content", "page 1 repaired", 1)
    with open(layout.md_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(repaired)
    before = open(layout.md_path, "rb").read()
    _adopt_passthrough_calls(monkeypatch)

    assert cv.reassemble_md(layout, pdf_path, 100) == layout.md_path

    assert open(layout.md_path, "rb").read() == before
    records = [
        dc.read_page_cache(layout.work_dir, page)
        for page in (1, 2)
    ]
    assert all(record is not None for record in records)
    assert dc.assemble_document(records) == repaired
    assert records[0]["page_overlays"][0]["kind"] == "exact_page_replacement"


def test_new_conversion_materializes_page_cache_and_document_index(tmp_path, monkeypatch):
    pdf_path = _pdf(tmp_path, pages=1)
    out = str(tmp_path / "out")
    monkeypatch.setattr(cv, "triage", lambda _path: "A")

    def fake_png(_pdf, page, work_dir, dpi):
        path = os.path.join(work_dir, f"page_{page:04d}.png")
        with open(path, "wb") as handle:
            handle.write(b"png")
        return path

    def fake_predict(png_path, work_dir):
        page = int(os.path.basename(png_path).split("_")[1].split(".")[0])
        result = {
            "width": 100,
            "height": 100,
            "parsing_res_list": [_block(page)],
        }
        with open(cp.page_res_path(work_dir, page), "w", encoding="utf-8") as handle:
            json.dump(result, handle)
        return result["parsing_res_list"]

    monkeypatch.setattr(cv, "pdf_page_to_png", fake_png)
    monkeypatch.setattr(cv, "predict_page", fake_predict)
    result = cv.convert_pdf(
        pdf_path,
        out,
        dpi=100,
        formula_repair="off",
        quality_repair="off",
    )
    layout = resolve_layout("book", out)

    cache = dc.read_page_cache(layout.work_dir, 1)
    assert cache is not None
    assert cache["page_markdown"] == "page 1 content\n"
    assert dc.read_document_index(
        layout.work_dir,
        expected_final_sha256=dc.sha256_file(result["md_path"]),
    ) is not None


def test_convert_resume_reconciles_old_hybrid_repair_without_touching_md_bytes(
        tmp_path, monkeypatch):
    pdf_path = _pdf(tmp_path)
    out = str(tmp_path / "out")
    layout = resolve_layout("book", out)
    _write_checkpoint(layout, pdf_path)
    _write_hybrid_audit(layout, pdf_path)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    repaired = _baseline_md().replace("page 2 content", "page 2 repaired", 1)
    with open(layout.md_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(repaired)
    before = open(layout.md_path, "rb").read()
    monkeypatch.setattr(cv, "triage", lambda _path: "B")
    calls = _adopt_passthrough_calls(monkeypatch)

    result = cv.convert_pdf(
        pdf_path,
        out,
        dpi=100,
        born_digital_mode="hybrid",
        formula_repair="off",
        quality_repair="off",
    )

    assert result["md_path"] == layout.md_path
    assert calls == [1, 2]
    assert open(layout.md_path, "rb").read() == before
    records = [
        dc.read_page_cache(layout.work_dir, page)
        for page in (1, 2)
    ]
    assert all(record is not None for record in records)
    assert dc.assemble_document(records) == repaired


def test_old_hybrid_crlf_final_migrates_and_preserves_exact_bytes(
        tmp_path, monkeypatch):
    pdf_path = _pdf(tmp_path)
    out = str(tmp_path / "out")
    layout = resolve_layout("book", out)
    _write_checkpoint(layout, pdf_path)
    _write_hybrid_audit(layout, pdf_path)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    repaired_crlf = _baseline_md().replace(
        "page 1 content", "page 1 repaired", 1).replace("\n", "\r\n")
    with open(layout.md_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(repaired_crlf)
    before = open(layout.md_path, "rb").read()
    calls = _adopt_passthrough_calls(monkeypatch)

    assert cv.reassemble_md(layout, pdf_path, 100) == layout.md_path

    assert open(layout.md_path, "rb").read() == before
    assert calls == [1, 2]
    index = dc.read_document_index(
        layout.work_dir, expected_final_sha256=dc.sha256_bytes(before))
    assert index is not None
    assert index["newline_style"] == dc.NEWLINE_CRLF
    records = [dc.read_page_cache(layout.work_dir, page) for page in (1, 2)]
    assert dc.assemble_document(
        records, newline_style=index["newline_style"]) == repaired_crlf

    calls.clear()
    assert cv.build_reassembled_markdown(
        layout, pdf_path, 100) == repaired_crlf
    assert calls == []


def test_old_hybrid_mixed_newline_final_fails_loud_and_preserves_exact_bytes(
        tmp_path, monkeypatch):
    pdf_path = _pdf(tmp_path)
    out = str(tmp_path / "out")
    layout = resolve_layout("book", out)
    _write_checkpoint(layout, pdf_path)
    _write_hybrid_audit(layout, pdf_path)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    mixed = _baseline_md().replace("\n", "\r\n", 1)
    with open(layout.md_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(mixed)
    before = open(layout.md_path, "rb").read()
    _adopt_passthrough_calls(monkeypatch)

    with pytest.raises(RuntimeError, match="legacy_cache_migration_unresolved"):
        cv.reassemble_md(layout, pdf_path, 100)

    assert open(layout.md_path, "rb").read() == before
    assert dc.read_page_cache(layout.work_dir, 1) is None
    assert dc.read_document_index(layout.work_dir) is None
