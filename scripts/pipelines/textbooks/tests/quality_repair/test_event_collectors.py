from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import fitz

from scripts.pipelines.textbooks.quality_repair.event_collectors import (
    collect_detection_batch,
    collect_final_delimiter_events,
    collect_formula_events,
    collect_source_audit_events,
    collect_unordered_events,
)
from scripts.pipelines.textbooks import checkpoint
from scripts.pipelines.textbooks.quality_repair.models import DetectorContext
from scripts.pipelines.textbooks.vision_repair import content_fingerprint


_FIXTURES = Path(__file__).parents[1] / "fixtures"


def _context(tmp_path: Path, md: str = "converted prose\n") -> DetectorContext:
    deliverable = tmp_path / "deliverable" / "Demo"
    deliverable.mkdir(parents=True)
    md_path = deliverable / "Demo.md"
    with md_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(md)
    work = tmp_path / "work" / "Demo" / "_work"
    work.mkdir(parents=True)
    return DetectorContext.from_paths(
        stem="Demo",
        md_path=md_path,
        work_dir=work,
        run_dir=tmp_path / "work" / "Demo" / "Demo_quality_repair" / "run-1",
    )


def _install_real_page(ctx: DetectorContext) -> Path:
    target = ctx.work_dir / "page_0031_res.json"
    shutil.copy2(_FIXTURES / "page_0031_res.json", target)
    return target


def _write_source_audit(
    ctx: DetectorContext,
    issues: list[dict],
    *,
    page: int = 31,
) -> Path:
    pdf_path = ctx.doc_work_dir / "Demo.pdf"
    document = fitz.open()
    try:
        for _ in range(page):
            document.new_page()
        document.save(pdf_path)
    finally:
        document.close()
    pdf_bytes = pdf_path.read_bytes()
    manifest = {
        "dpi": 150,
        "failed_pages": [],
        "pdf_path": str(pdf_path),
    }
    (ctx.work_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    report_path = ctx.doc_work_dir / "Demo_source_audit.json"
    report_path.write_text(json.dumps({
        "schema_version": 6,
        "stem": "Demo",
        "pdf_fingerprint": {
            "size_bytes": len(pdf_bytes),
            "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "page_count": page,
        },
        "ocr_fingerprint": checkpoint.ocr_results_fingerprint(
            str(ctx.work_dir), page, 150),
        "summary": {
            "status": "SUSPECT",
            "pages": page,
            "issue_counts": {
                issue["code"]: 1 for issue in issues
            },
        },
        "pages": [{"page": page, "status": "SUSPECT", "issues": issues}],
    }), encoding="utf-8")
    return report_path


def test_unordered_collector_uses_real_block_ids_and_keeps_furniture_as_metrics(tmp_path):
    ctx = _context(tmp_path)
    page_path = _install_real_page(ctx)
    payload = json.loads(page_path.read_text(encoding="utf-8"))
    payload["parsing_res_list"].append({
        "block_label": "reference_content",
        "block_content": "A valuable unordered note absent from the final Markdown.",
        "block_bbox": [50, 700, 500, 740],
        "block_id": 99,
        "block_order": None,
    })
    page_path.write_text(json.dumps(payload), encoding="utf-8")

    events, metrics = collect_unordered_events(ctx)

    assert len(events) == 1
    event = events[0]
    assert (event.page, event.block_id) == (31, 99)
    assert event.bbox == (50.0, 700.0, 500.0, 740.0)
    assert event.target == {"scope": "block", "page": 31, "block_id": 99}
    # Real fixture page 31 contains header + number furniture, an empty chart,
    # and a figure_title structural visual. They remain metrics, not Agent spam.
    assert metrics["unordered:likely_furniture"] == 2
    assert metrics["unordered:empty_visual"] == 1
    assert metrics["unordered:structural_visual"] == 1


def test_formula_collector_merges_fresh_candidate_and_render_error_per_real_block(tmp_path):
    ctx = _context(tmp_path)
    page_path = _install_real_page(ctx)
    block = json.loads(page_path.read_text(encoding="utf-8"))["parsing_res_list"][5]
    repair = ctx.doc_work_dir / "Demo_repair"
    repair.mkdir()
    render_path = ctx.doc_work_dir / "Demo_render_errors.json"
    render_path.write_text(json.dumps({
        "errors": [{
            "page": 31, "block_ids": [5],
            "error": "Undefined control sequence", "latex_head": "\\nabla",
        }]
    }), encoding="utf-8")
    (repair / "worklist.json").write_text(json.dumps({
        "items": [{"page": 31, "block_id": 5}]
    }), encoding="utf-8")
    candidate_path = repair / "formula_candidates.jsonl"
    candidate_path.write_text(json.dumps({
        "candidate_id": "p0031-b0005",
        "page": 31,
        "block_id": 5,
        "bbox": block["block_bbox"],
        "engine_latex": block["block_content"],
        "reasons": ["worklist:bare_op", "katex_error:Undefined control sequence"],
        "crop_path": "run-specific/crop.png",
    }) + "\n", encoding="utf-8")
    # selfcheck aggregation must not create another formula event.
    (ctx.doc_work_dir / "Demo_selfcheck.json").write_text(json.dumps({
        "formula_suspicions": [{"op": "\\int", "count": 99}]
    }), encoding="utf-8")

    events, metrics = collect_formula_events(ctx)

    assert len(events) == 1
    event = events[0]
    assert (event.page, event.block_id) == (31, 5)
    assert event.bbox == tuple(float(value) for value in block["block_bbox"])
    assert event.severity.value == "P1"
    assert event.evidence["reasons"] == [
        "katex_error:Undefined control sequence", "worklist:bare_op",
    ]
    assert len(event.evidence["render_errors"]) == 1
    assert metrics["formula:candidate_records"] == 1
    assert metrics["formula:error_block_hits"] == 1
    assert metrics["formula:deduped_events"] == 1


def test_formula_collector_ignores_stale_materialized_inputs(tmp_path):
    ctx = _context(tmp_path)
    page_path = _install_real_page(ctx)
    repair = ctx.doc_work_dir / "Demo_repair"
    repair.mkdir()
    candidate_path = repair / "formula_candidates.jsonl"
    candidate_path.write_text(json.dumps({
        "page": 31, "block_id": 5, "reasons": ["worklist:bare_op"],
    }) + "\n", encoding="utf-8")
    render_path = ctx.doc_work_dir / "Demo_render_errors.json"
    render_path.write_text(json.dumps({
        "errors": [{"page": 31, "block_ids": [5], "error": "old"}],
    }), encoding="utf-8")
    future = time.time() + 5
    os.utime(page_path, (future, future))

    events, metrics = collect_formula_events(ctx)

    assert events == []
    assert metrics["formula:stale_candidates"] == 1
    assert metrics["formula:stale_render_records"] == 1


def test_formula_collector_excludes_terminal_accept_candidate(tmp_path):
    ctx = _context(tmp_path)
    _install_real_page(ctx)
    repair = ctx.doc_work_dir / "Demo_repair"
    repair.mkdir()
    candidate_path = repair / "formula_candidates.jsonl"
    candidate_path.write_text(json.dumps({
        "candidate_id": "p0031-b0005", "page": 31, "block_id": 5,
        "engine_latex": "x+1", "reasons": ["worklist:bare_op"],
    }) + "\n", encoding="utf-8")
    (repair / "formula_agent_verdicts.jsonl").write_text(json.dumps({
        "candidate_id": "p0031-b0005", "verdict": "accept",
        "latex": "x+1", "confidence": 0.99,
        "content_fingerprint": content_fingerprint("x+1"),
    }) + "\n", encoding="utf-8")

    events, metrics = collect_formula_events(ctx)

    assert events == []
    assert metrics["formula:terminal_candidates"] == 1


def test_source_audit_collector_expands_only_severe_block_findings(tmp_path):
    ctx = _context(tmp_path)
    page_path = _install_real_page(ctx)
    block = json.loads(page_path.read_text(encoding="utf-8"))[
        "parsing_res_list"][5]
    _write_source_audit(ctx, [
        {
            "code": "numeric_missing", "block_id": 5,
            "detail": {"missing": ["0.25"]},
        },
        {
            "code": "visual_table_unscorable", "block_id": 6,
            "detail": "image-only table",
        },
        {
            "code": "furniture_prose_noise", "block_id": 0,
            "detail": "running header",
        },
    ])

    events, metrics = collect_source_audit_events(ctx)

    assert len(events) == 1
    event = events[0]
    assert event.capability == "source_audit"
    assert event.kind == "source_audit_numeric_missing"
    assert event.route == "quality_agent:source_grounded_repair"
    assert (event.page, event.block_id) == (31, 5)
    assert event.bbox == tuple(float(value) for value in block["block_bbox"])
    assert event.evidence["source_audit"] == {
        "code": "numeric_missing",
        "detail": {"missing": ["0.25"]},
        "page": 31,
        "block_id": 5,
        "source_index": None,
        "bbox": block["block_bbox"],
    }
    assert event.evidence["source_grounded"] is True
    assert metrics["source_audit:numeric_missing"] == 1
    assert metrics["source_audit:non_actionable_issue"] == 2


def test_source_audit_collector_rejects_report_after_page_input_changes(tmp_path):
    ctx = _context(tmp_path)
    page_path = _install_real_page(ctx)
    _write_source_audit(ctx, [{
        "code": "numeric_mismatch", "block_id": 5, "detail": "1 -> 7",
    }])
    payload = json.loads(page_path.read_text(encoding="utf-8"))
    payload["parsing_res_list"][5]["block_content"] = "changed after audit"
    page_path.write_text(json.dumps(payload), encoding="utf-8")

    events, metrics = collect_source_audit_events(ctx)

    assert events == []
    assert metrics["source_audit:stale_or_invalid_report"] == 1


def test_source_audit_collector_binds_schema_and_pdf_sha256(tmp_path):
    ctx = _context(tmp_path)
    _install_real_page(ctx)
    report_path = _write_source_audit(ctx, [{
        "code": "numeric_mismatch", "block_id": 5, "detail": "1 -> 7",
    }])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["pdf_fingerprint"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")

    events, metrics = collect_source_audit_events(ctx)

    assert events == []
    assert metrics["source_audit:stale_or_invalid_report"] == 1

    report["pdf_fingerprint"]["sha256"] = hashlib.sha256(
        (ctx.doc_work_dir / "Demo.pdf").read_bytes()).hexdigest()
    report["schema_version"] = 2
    report_path.write_text(json.dumps(report), encoding="utf-8")

    events, metrics = collect_source_audit_events(ctx)

    assert events == []
    assert metrics["source_audit:stale_or_invalid_report"] == 1


def test_final_delimiter_collector_emits_exact_occurrence_offsets(tmp_path):
    md = "formula $ x + 1 $ and $y$\nprice $20\nbroken $z\n"
    ctx = _context(tmp_path, md)

    events, metrics = collect_final_delimiter_events(ctx)
    by_kind = {event.kind: event for event in events}

    assert set(by_kind) == {
        "inline_math_delimiter_whitespace",
        "ambiguous_currency_delimiter",
        "unpaired_math_delimiter",
    }
    whitespace = by_kind["inline_math_delimiter_whitespace"]
    start = md.index("$ x + 1 $")
    assert whitespace.target["md_start"] == start
    assert whitespace.target["md_end"] == start + len("$ x + 1 $")
    assert md[whitespace.target["md_start"]:whitespace.target["md_end"]] == "$ x + 1 $"
    currency = by_kind["ambiguous_currency_delimiter"]
    assert md[currency.target["md_start"]:currency.target["md_end"]] == "$"
    assert currency.target["line"] == 2
    assert metrics["delimiter:inline_math_whitespace"] == 1


def test_combined_collector_returns_detection_batch(tmp_path):
    ctx = _context(tmp_path, "broken $x\n")
    _install_real_page(ctx)

    batch = collect_detection_batch(ctx)

    assert batch.stem == "Demo"
    assert batch.baseline_sha256 == ctx.baseline_sha256
    assert len(batch.events) == 1
    assert batch.events[0].kind == "unpaired_math_delimiter"
    assert batch.metrics["unordered:likely_furniture"] == 2
