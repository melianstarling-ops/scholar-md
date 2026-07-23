from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest

from scripts.pipelines.textbooks import derived_cache as dc


@dataclass(frozen=True)
class _Decision:
    block_id: int
    content_source: str
    reasons: list[str]
    block_ned: float | None
    adopted_text: str | None


def _key(tmp_path, **changes):
    pdf = tmp_path / "source.pdf"
    ocr = tmp_path / "page_0001_res.json"
    if not pdf.exists():
        pdf.write_bytes(b"same-size-A")
    if not ocr.exists():
        ocr.write_text('{"parsing_res_list":[]}', encoding="utf-8")
    values = {
        "stem": "book",
        "source_pdf_path": pdf,
        "dpi": 150,
        "ocr_page_path": ocr,
        "page_corrections": [],
        "page_overlay": [],
        "adoption_thresholds": {"max_ned": 0.2, "min_ratio": 0.5},
        "reconstruct_profile": "reconstruct-v1",
        "adoption_profile": "route-b-v1",
    }
    values.update(changes)
    return dc.build_cache_key_from_files(**values)


def _record(tmp_path, *, page=1, text="alpha"):
    fragments = [
        {"bids": [4], "md": text},
        {"block_ids": [5, 6], "md": "$$ x \\tag{1} $$"},
    ]
    page_md = f"{text}\n\n$$ x \\tag{{1}} $$\n"
    return dc.materialize_page_cache(
        page=page,
        cache_key=_key(tmp_path),
        adopted_decisions=[
            _Decision(4, "source_text", [], 0.01, text),
            {"block_id": 5, "content_source": "ocr",
             "reasons": ["label_not_adoptable"], "block_ned": None,
             "adopted_text": None},
        ],
        fragments=fragments,
        page_markdown=page_md,
        warnings=[{"kind": "sample"}],
        expected_assets=["book.assets/page_0001_b0005.png"],
        column_layout_suspected=True,
    )


def test_cache_key_uses_strong_source_hash_for_same_size_files(tmp_path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"same-size-A")
    second.write_bytes(b"same-size-B")
    assert first.stat().st_size == second.stat().st_size

    key_a = _key(tmp_path, source_pdf_path=first)
    key_b = _key(tmp_path, source_pdf_path=second)

    assert key_a["source_pdf_sha256"] != key_b["source_pdf_sha256"]
    assert key_a["digest"] != key_b["digest"]


def test_cache_key_canonicalizes_mapping_order_and_invalidates_each_page_input(tmp_path):
    key = _key(tmp_path)
    reordered = _key(
        tmp_path,
        adoption_thresholds={"min_ratio": 0.5, "max_ned": 0.2},
    )
    changed = [
        _key(tmp_path, dpi=151),
        _key(tmp_path, page_corrections=[{"block_id": 1, "text": "fixed"}]),
        _key(tmp_path, page_overlay=[{"block_id": 2, "text": "overlay"}]),
        _key(tmp_path, adoption_thresholds={"max_ned": 0.3, "min_ratio": 0.5}),
        _key(tmp_path, reconstruct_profile="reconstruct-v2"),
        _key(tmp_path, adoption_profile="route-b-v2"),
        _key(tmp_path, stem="other"),
    ]

    assert key == reordered
    assert all(candidate["digest"] != key["digest"] for candidate in changed)


def test_materialize_preserves_adopted_text_and_exact_fragment_spans(tmp_path):
    record = _record(tmp_path)

    assert record["adoption_decisions"][0]["adopted_text"] == "alpha"
    assert record["fragments"] == [
        {"block_ids": [4], "md": "alpha", "local_start": 0, "local_end": 5},
        {"block_ids": [5, 6], "md": "$$ x \\tag{1} $$",
         "local_start": 7, "local_end": 22},
    ]
    assert record["page_md_sha256"] == dc.sha256_text(record["page_markdown"])
    assert record["reconstruct_profile"] == "reconstruct-v1"
    assert record["adoption_profile"] == "route-b-v1"


def test_materialize_rejects_page_markdown_not_derived_from_fragments(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        dc.materialize_page_cache(
            page=1,
            cache_key=_key(tmp_path),
            adopted_decisions=[],
            fragments=[{"bids": [1], "md": "expected"}],
            page_markdown="different\n",
        )


def test_fragment_allows_missing_block_id_but_adoption_decision_does_not(tmp_path):
    record = dc.materialize_page_cache(
        page=1,
        cache_key=_key(tmp_path),
        adopted_decisions=[],
        fragments=[{"bids": [None], "md": "legacy fixture"}],
        page_markdown="legacy fixture\n",
    )
    assert record["fragments"][0]["block_ids"] == [None]

    with pytest.raises(ValueError, match="block_id must be an integer"):
        dc.materialize_page_cache(
            page=1,
            cache_key=_key(tmp_path),
            adopted_decisions=[{
                "block_id": None, "content_source": "ocr", "reasons": [],
                "block_ned": None, "adopted_text": None,
            }],
            fragments=[],
            page_markdown="\n",
        )


def test_page_cache_atomic_roundtrip_and_expected_key_invalidation(tmp_path):
    work = tmp_path / "_work"
    record = _record(tmp_path)

    path = dc.write_page_cache(work, record)

    assert path == work / "_derived_v1" / "page_0001.json"
    assert dc.read_page_cache(work, 1, expected_key=record["cache_key"]) == record
    assert dc.read_page_cache(work, 1, expected_key=_key(tmp_path, dpi=151)) is None


def test_atomic_write_failure_preserves_previous_page_cache(tmp_path, monkeypatch):
    work = tmp_path / "_work"
    first = _record(tmp_path, text="first")
    dc.write_page_cache(work, first)
    path = dc.page_cache_path(work, 1)
    before = path.read_bytes()
    second = _record(tmp_path, text="second")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(dc.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        dc.write_page_cache(work, second)

    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("mutation", ["corrupt", "schema", "page_md", "record_hash"])
def test_page_cache_corruption_is_a_cache_miss(tmp_path, mutation):
    work = tmp_path / "_work"
    record = _record(tmp_path)
    path = dc.write_page_cache(work, record)
    if mutation == "corrupt":
        path.write_text("{", encoding="utf-8")
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "schema":
            value["schema_version"] = 99
        elif mutation == "page_md":
            value["page_markdown"] = "tampered\n"
        else:
            value["warnings"].append({"kind": "tampered"})
        path.write_text(json.dumps(value), encoding="utf-8")

    assert dc.read_page_cache(work, 1) is None


def test_document_index_matches_assemble_semantics_and_records_spans(tmp_path):
    first = _record(tmp_path, page=1, text="first")
    empty = dc.materialize_page_cache(
        page=2,
        cache_key=_key(tmp_path),
        adopted_decisions=[],
        fragments=[],
        page_markdown="\n",
    )
    third = _record(tmp_path, page=3, text="third")
    records = [third, empty, first]
    final_md = dc.assemble_document(records)

    index = dc.build_document_index(records, final_markdown=final_md)

    assert final_md == first["page_markdown"] + "\n\n" + third["page_markdown"] + "\n"
    entries = {entry["page"]: entry for entry in index["pages"]}
    assert (entries[1]["document_start"], entries[1]["document_end"]) == (
        0, len(first["page_markdown"]))
    assert entries[2]["document_start"] is None
    third_start = len(first["page_markdown"]) + 2
    assert (entries[3]["document_start"], entries[3]["document_end"]) == (
        third_start, third_start + len(third["page_markdown"]))


def test_document_index_atomic_roundtrip_and_final_hash_validation(tmp_path):
    work = tmp_path / "_work"
    record = _record(tmp_path)
    final_md = dc.assemble_document([record])
    index = dc.build_document_index([record], final_markdown=final_md)

    path = dc.write_document_index(work, index)

    assert path == work / "_derived_v1" / "document_index.json"
    assert dc.read_document_index(
        work, expected_final_sha256=dc.sha256_text(final_md)) == index
    assert dc.read_document_index(
        work, expected_final_sha256=dc.sha256_text("other")) is None


def test_crlf_document_roundtrip_keeps_pages_canonical_and_hashes_exact(tmp_path):
    first = _record(tmp_path, page=1, text="first\nline")
    second = _record(tmp_path, page=2, text="second")
    records = [first, second]
    final_md = dc.assemble_document(records, newline_style=dc.NEWLINE_CRLF)

    index = dc.build_document_index(records, final_markdown=final_md)

    assert "\r\n" in final_md
    assert "\r" not in first["page_markdown"]
    assert index["newline_style"] == dc.NEWLINE_CRLF
    assert index["final_md_sha256"] == dc.sha256_text(final_md)
    assert dc.assemble_document(
        records, newline_style=index["newline_style"]) == final_md


def test_document_index_rejects_mixed_newlines(tmp_path):
    record = _record(tmp_path)
    mixed = dc.assemble_document([record]).replace("\n", "\r\n", 1)

    with pytest.raises(ValueError, match="mixed"):
        dc.build_document_index([record], final_markdown=mixed)


def test_document_index_rejects_nonmatching_final_markdown(tmp_path):
    record = _record(tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        dc.build_document_index([record], final_markdown="different\n")


def test_document_index_integrity_hash_detects_metadata_tampering(tmp_path):
    work = tmp_path / "_work"
    record = _record(tmp_path)
    index = dc.build_document_index(
        [record], final_markdown=dc.assemble_document([record]))
    path = dc.write_document_index(work, index)
    tampered = copy.deepcopy(index)
    tampered["pages"][0]["document_end"] += 1
    path.write_text(json.dumps(tampered), encoding="utf-8")

    assert dc.read_document_index(work) is None


def test_reconcile_page_overlays_accepts_single_page_edit_and_is_exact(tmp_path):
    first = _record(tmp_path, page=1, text="first page")
    second = _record(tmp_path, page=2, text="second page")
    current = dc.assemble_document([first, second]).replace(
        "first page", "first page repaired", 1)

    reconciled = dc.reconcile_page_overlays(
        [first, second], current_final_markdown=current)

    assert dc.assemble_document(reconciled) == current
    assert reconciled[0]["page_overlays"][0]["kind"] == "exact_page_replacement"
    assert reconciled[1].get("page_overlays") == []


def test_reconcile_page_overlays_accepts_uniform_crlf_but_keeps_page_lf(tmp_path):
    first = _record(tmp_path, page=1, text="first page")
    second = _record(tmp_path, page=2, text="second page")
    current_lf = dc.assemble_document([first, second]).replace(
        "first page", "first page repaired", 1)
    current = current_lf.replace("\n", "\r\n")

    reconciled = dc.reconcile_page_overlays(
        [first, second], current_final_markdown=current)

    assert "\r" not in reconciled[0]["page_markdown"]
    assert dc.assemble_document(
        reconciled, newline_style=dc.NEWLINE_CRLF) == current
    index = dc.build_document_index(reconciled, final_markdown=current)
    assert index["newline_style"] == dc.NEWLINE_CRLF
    assert index["final_md_sha256"] == dc.sha256_text(current)


def test_reconcile_page_overlays_rejects_cross_page_edit(tmp_path):
    first = _record(tmp_path, page=1, text="first page")
    second = _record(tmp_path, page=2, text="second page")
    baseline = dc.assemble_document([first, second])
    current = baseline.replace(
        first["page_markdown"] + "\n\n" + second["page_markdown"],
        "one replacement spanning both pages\n",
    )

    with pytest.raises(RuntimeError, match="跨页|页边界"):
        dc.reconcile_page_overlays(
            [first, second], current_final_markdown=current)


def test_restore_cache_directory_removes_new_files_and_restores_old_bytes(tmp_path):
    work = tmp_path / "_work"
    root = dc.derived_dir(work)
    root.mkdir(parents=True)
    first = dc.page_cache_path(work, 1)
    index = dc.document_index_path(work)
    first.write_bytes(b"old-page")
    index.write_bytes(b"old-index")
    snapshot = dc.snapshot_cache_directory(work)

    first.write_bytes(b"new-page")
    dc.page_cache_path(work, 2).write_bytes(b"new-page-2")
    index.write_bytes(b"new-index")
    dc.restore_cache_directory(work, snapshot)

    assert first.read_bytes() == b"old-page"
    assert index.read_bytes() == b"old-index"
    assert not dc.page_cache_path(work, 2).exists()
