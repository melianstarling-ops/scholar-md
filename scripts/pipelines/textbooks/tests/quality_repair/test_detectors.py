from __future__ import annotations

import json
from pathlib import Path

from scripts.pipelines.textbooks.quality_repair.detectors.assets import detect_assets
from scripts.pipelines.textbooks.quality_repair.detectors.final_delimiters import detect_final_delimiters
from scripts.pipelines.textbooks.quality_repair.detectors.formulas import detect_formulas
from scripts.pipelines.textbooks.quality_repair.detectors.novel_discovery import detect_novel_signals
from scripts.pipelines.textbooks.quality_repair.detectors.page_completeness import detect_page_completeness
from scripts.pipelines.textbooks.quality_repair.detectors.unordered_blocks import detect_unordered_blocks
from scripts.pipelines.textbooks.quality_repair.models import DetectorContext
from scripts.pipelines.textbooks.vision_repair import content_fingerprint


def _write_result(work: Path, page: int, blocks: list[dict],
                  *, width: int | None = None) -> None:
    payload = {"page_index": page - 1, "page_count": 3,
               "parsing_res_list": blocks}
    if width is not None:
        payload["width"] = width
    (work / f"page_{page:04d}_res.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _context(tmp_path: Path, md: str = "alpha\n") -> DetectorContext:
    doc = tmp_path / "deliverable" / "Demo"
    doc.mkdir(parents=True)
    md_path = doc / "Demo.md"
    md_path.write_text(md, encoding="utf-8")
    work = tmp_path / "work" / "Demo" / "_work"
    work.mkdir(parents=True)
    return DetectorContext.from_paths(
        stem="Demo", md_path=md_path, work_dir=work,
        run_dir=tmp_path / "work" / "Demo" / "Demo_quality_repair" / "run-1",
    )


def test_page_completeness_reports_missing_result_and_manifest_failure(tmp_path):
    ctx = _context(tmp_path)
    _write_result(ctx.work_dir, 1, [{"block_content": "alpha", "block_order": 1}])
    (ctx.work_dir / "manifest.json").write_text(json.dumps({
        "fingerprint": {"page_count": 3},
        "failed_pages": [{"page": 2, "kind": "process-killed", "error": "boom"}],
    }), encoding="utf-8")

    findings = detect_page_completeness(ctx)

    assert {f.kind for f in findings} == {"missing_page_results", "failed_pages"}
    by_kind = {f.kind: f for f in findings}
    assert by_kind["failed_pages"].evidence["ranges"] == ["2"]
    assert by_kind["failed_pages"].evidence["missing_result_count"] == 1
    assert by_kind["missing_page_results"].evidence["ranges"] == ["3"]


def test_page_completeness_aggregates_large_contiguous_failures(tmp_path):
    ctx = _context(tmp_path)
    _write_result(ctx.work_dir, 1, [{"block_content": "alpha", "block_order": 1}])
    failures = [{"page": page, "kind": "page-exception", "error": "CUDA OOM"}
                for page in range(2, 102)]
    (ctx.work_dir / "manifest.json").write_text(json.dumps({
        "fingerprint": {"page_count": 101}, "failed_pages": failures,
    }), encoding="utf-8")
    findings = detect_page_completeness(ctx)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "failed_pages"
    assert finding.evidence["count"] == 100
    assert finding.evidence["pages_truncated"] is True
    assert finding.evidence["ranges"] == ["2-101"]


def test_final_delimiters_reuses_whitespace_lint_and_flags_unpaired_math(tmp_path):
    ctx = _context(tmp_path, "price is $20\nformula $ x + 1 $\nbroken $x + 1\n")
    findings = detect_final_delimiters(ctx)
    kinds = {f.kind for f in findings}
    assert "inline_math_delimiter_whitespace" in kinds
    assert "unpaired_math_delimiter" in kinds
    # currency is evidence, not an automatic repair
    assert any(f.kind == "ambiguous_currency_delimiter" for f in findings)


def test_unordered_blocks_are_auditable_without_being_deleted(tmp_path):
    ctx = _context(tmp_path)
    _write_result(ctx.work_dir, 1, [
        {"block_id": 1, "block_label": "text", "block_content": "kept", "block_order": 1},
        {"block_id": 2, "block_label": "reference_content", "block_content": "valuable note", "block_order": None},
        {"block_id": 3, "block_label": "seal", "block_content": "", "block_order": None},
    ])

    findings = detect_unordered_blocks(ctx)

    assert len(findings) == 1
    assert {f.evidence["classification"] for f in findings} == {"review_content"}
    assert all(f.kind == "unordered_block" for f in findings)


def test_unordered_blocks_skip_preserved_text_and_known_furniture(tmp_path):
    ctx = _context(tmp_path, "X distance; Y phase\n")
    _write_result(ctx.work_dir, 1, [
        {"block_id": 1, "block_label": "vision_footnote",
         "block_content": "X distance;\nY phase", "block_order": None},
        {"block_id": 2, "block_label": "aside_text",
         "block_content": "Licensed copy: Example", "block_order": None},
        {"block_id": 3, "block_label": "header",
         "block_content": "ISO 10974", "block_order": None},
        {"block_id": 4, "block_label": "table",
         "block_content": "<table></table>", "block_order": None},
    ])

    assert detect_unordered_blocks(ctx) == []


def test_unordered_blocks_keep_unresolved_aside_text_visible(tmp_path):
    ctx = _context(tmp_path)
    _write_result(ctx.work_dir, 1, [
        {"block_id": 1, "block_label": "aside_text",
         "block_content": "Important marginal warning", "block_order": None},
    ])

    findings = detect_unordered_blocks(ctx)

    assert len(findings) == 1
    assert findings[0].evidence["classification"] == "unknown_visual"


def test_unordered_blocks_skip_revision_text_at_extreme_page_edge(tmp_path):
    ctx = _context(tmp_path)
    _write_result(ctx.work_dir, 1, [
        {"block_id": 1, "block_label": "aside_text",
         "block_content": "© ISO 2018 — All rights reserved",
         "block_bbox": [5, 100, 55, 400], "block_order": None},
        {"block_id": 2, "block_label": "aside_text",
         "block_content": "Important 2018 study result",
         "block_bbox": [200, 100, 500, 300], "block_order": None},
    ], width=1000)

    findings = detect_unordered_blocks(ctx)

    assert len(findings) == 1
    assert findings[0].evidence["samples"][0]["block_id"] == 2


def test_assets_find_missing_and_embedded_base64(tmp_path):
    ctx = _context(tmp_path, "![](Demo.assets/missing.png)\n![](data:image/png;base64,AAAA)\n")
    findings = detect_assets(ctx)
    assert {f.kind for f in findings} == {"missing_asset", "embedded_base64_asset"}


def test_assets_reject_path_escape_even_if_external_file_exists(tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    ctx = _context(tmp_path, "![](../../secret.png)\n")
    findings = detect_assets(ctx)
    assert {f.kind for f in findings} == {"asset_path_escape"}


def test_assets_can_resolve_an_isolated_candidate_against_deliverable_base(tmp_path):
    deliverable = tmp_path / "deliverable" / "Demo"
    assets = deliverable / "Demo.assets"
    assets.mkdir(parents=True)
    (assets / "page.png").write_bytes(b"png")
    candidate = tmp_path / "run" / "_candidate" / "Demo.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("![](Demo.assets/page.png)\n", encoding="utf-8")
    work = tmp_path / "work" / "Demo" / "_work"
    work.mkdir(parents=True)
    ctx = DetectorContext.from_paths(
        stem="Demo", md_path=candidate, work_dir=work,
        run_dir=tmp_path / "run", asset_base_dir=deliverable,
    )

    assert detect_assets(ctx) == []


def test_formulas_reuse_existing_selfcheck_signals(tmp_path):
    ctx = _context(tmp_path)
    selfcheck = ctx.work_dir.parent / "Demo_selfcheck.json"
    selfcheck.write_text(json.dumps({
        "katex_incompat": ["\\begin{split}"],
        "formula_suspicions": [{"op": "\\oint", "count": 2}],
    }), encoding="utf-8")
    findings = detect_formulas(ctx)
    assert {f.kind for f in findings} == {"katex_incompatible_command", "formula_suspicion"}


def test_formulas_aggregate_existing_render_errors_and_candidates(tmp_path):
    ctx = _context(tmp_path)
    (ctx.work_dir.parent / "Demo_render_errors.json").write_text(json.dumps({
        "errors": [{"page": 1, "error": "Undefined control sequence", "latex_head": "\\foo"}]
    }), encoding="utf-8")
    repair = ctx.work_dir.parent / "Demo_repair"
    repair.mkdir()
    (repair / "formula_candidates.jsonl").write_text(
        json.dumps({"candidate_id": "p0001-b0001", "page": 1,
                    "reasons": ["katex_error"]}) + "\n", encoding="utf-8")
    kinds = {f.kind for f in detect_formulas(ctx)}
    assert "katex_render_errors" in kinds
    assert "formula_candidates_pending" in kinds


def test_formulas_do_not_treat_terminal_accept_candidate_as_pending(tmp_path):
    ctx = _context(tmp_path)
    repair = ctx.work_dir.parent / "Demo_repair"
    repair.mkdir()
    candidate = {
        "candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "engine_latex": "x+1", "reasons": ["worklist:bare_op"],
    }
    (repair / "formula_candidates.jsonl").write_text(
        json.dumps(candidate) + "\n", encoding="utf-8")
    (repair / "formula_agent_verdicts.jsonl").write_text(json.dumps({
        "candidate_id": "p0001-b0001", "verdict": "accept",
        "latex": "x+1", "confidence": 0.99,
        "content_fingerprint": content_fingerprint("x+1"),
    }) + "\n", encoding="utf-8")

    kinds = {finding.kind for finding in detect_formulas(ctx)}

    assert "formula_candidates_pending" not in kinds


def test_formulas_do_not_reuse_terminal_verdict_after_candidate_content_changes(
        tmp_path):
    ctx = _context(tmp_path)
    repair = ctx.work_dir.parent / "Demo_repair"
    repair.mkdir()
    candidate = {
        "candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "engine_latex": "x+2", "reasons": ["worklist:bare_op"],
    }
    (repair / "formula_candidates.jsonl").write_text(
        json.dumps(candidate) + "\n", encoding="utf-8")
    (repair / "formula_agent_verdicts.jsonl").write_text(json.dumps({
        "candidate_id": "p0001-b0001", "verdict": "accept",
        "latex": "x+1", "confidence": 0.99,
        "content_fingerprint": content_fingerprint("x+1"),
    }) + "\n", encoding="utf-8")

    assert "formula_candidates_pending" in {
        finding.kind for finding in detect_formulas(ctx)
    }


def test_formulas_keep_correct_candidate_pending_until_current_correction_is_accepted(tmp_path):
    ctx = _context(tmp_path)
    repair = ctx.work_dir.parent / "Demo_repair"
    repair.mkdir()
    candidate = {
        "candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "engine_latex": "x+1", "reasons": ["worklist:bare_op"],
    }
    (repair / "formula_candidates.jsonl").write_text(
        json.dumps(candidate) + "\n", encoding="utf-8")
    (repair / "formula_agent_verdicts.jsonl").write_text(json.dumps({
        "candidate_id": "p0001-b0001", "verdict": "correct",
        "latex": "x+2", "confidence": 0.99,
    }) + "\n", encoding="utf-8")
    corrections = ctx.work_dir.parent / "Demo_corrections.json"
    corrections.write_text(json.dumps({"corrections": [{
        "candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "status": "pending",
        "content_fingerprint": content_fingerprint("x+1"),
    }]}), encoding="utf-8")
    assert "formula_candidates_pending" in {
        finding.kind for finding in detect_formulas(ctx)
    }

    corrections.write_text(json.dumps({"corrections": [{
        "candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "status": "accepted",
        "content_fingerprint": content_fingerprint("x+1"),
    }]}), encoding="utf-8")
    assert "formula_candidates_pending" not in {
        finding.kind for finding in detect_formulas(ctx)
    }


def test_page_completeness_includes_selfcheck_block_coverage_missing(tmp_path):
    ctx = _context(tmp_path)
    _write_result(ctx.work_dir, 1, [{"block_content": "alpha", "block_order": 1}])
    (ctx.work_dir / "manifest.json").write_text(json.dumps({
        "fingerprint": {"page_count": 1}, "failed_pages": []}), encoding="utf-8")
    (ctx.work_dir.parent / "Demo_selfcheck.json").write_text(json.dumps({
        "total": 2, "in_md": 1, "missing": ["lost paragraph"], "skipped_empty": 0,
    }), encoding="utf-8")
    findings = detect_page_completeness(ctx)
    assert any(f.kind == "block_coverage_missing" and f.severity.value == "P0"
               for f in findings)


def test_novel_discovery_emits_generic_signal_for_statistical_page_collapse(tmp_path):
    ctx = _context(tmp_path)
    normal = [{"block_id": 1, "block_label": "text", "block_order": 1,
               "block_content": "normal prose " * 80}]
    _write_result(ctx.work_dir, 1, normal)
    _write_result(ctx.work_dir, 2, [{"block_id": 1, "block_label": "text",
                                    "block_order": 1, "block_content": "tiny"}])
    _write_result(ctx.work_dir, 3, normal)

    findings = detect_novel_signals(ctx)

    collapse = [f for f in findings if f.kind == "novel_page_text_collapse"]
    assert len(collapse) == 1 and collapse[0].page == 2
    assert collapse[0].capability == "novel_discovery"


def test_novel_discovery_does_not_call_visual_dominant_page_text_collapse(tmp_path):
    ctx = _context(tmp_path)
    normal = [{"block_id": 1, "block_label": "text", "block_order": 1,
               "block_content": "normal prose " * 80}]
    _write_result(ctx.work_dir, 1, normal)
    _write_result(ctx.work_dir, 2, [
        {"block_id": 1, "block_label": "paragraph_title", "block_order": 1,
         "block_content": "Fig."},
        {"block_id": 2, "block_label": "image", "block_order": None,
         "block_content": ""},
        {"block_id": 3, "block_label": "figure_title", "block_order": None,
         "block_content": "A full-page diagram"},
    ])
    _write_result(ctx.work_dir, 3, normal)
    assert detect_novel_signals(ctx) == []


def test_novel_discovery_does_not_flag_sparse_front_matter_title_page(tmp_path):
    ctx = _context(tmp_path)
    normal = [{"block_id": 1, "block_label": "text", "block_order": 1,
               "block_content": "normal prose " * 80}]
    _write_result(ctx.work_dir, 2, normal)
    _write_result(ctx.work_dir, 3, [
        {"block_id": 0, "block_label": "paragraph_title", "block_order": 1,
         "block_content": "万水 ANSYS 技术丛书"},
        {"block_id": 1, "block_label": "doc_title", "block_order": 2,
         "block_content": "电磁兼容原理分析与设计技术"},
        {"block_id": 2, "block_label": "text", "block_order": 3,
         "block_content": "林汉年 编著"},
        {"block_id": 3, "block_label": "footer_image", "block_order": None,
         "block_content": ""},
    ])
    _write_result(ctx.work_dir, 4, normal)

    assert not [
        finding for finding in detect_novel_signals(ctx)
        if finding.kind == "novel_page_text_collapse"
    ]


def test_novel_discovery_flags_replacement_character_and_repeated_long_block(tmp_path):
    ctx = _context(tmp_path)
    repeated = "this OCR paragraph is unexpectedly repeated in a loop"
    _write_result(ctx.work_dir, 1, [
        {"block_id": 1, "block_label": "text", "block_order": 1,
         "block_content": "normal prose " * 80},
    ])
    _write_result(ctx.work_dir, 2, [
        {"block_id": 1, "block_label": "text", "block_order": 1,
         "block_content": "bad \ufffd text"},
        *[{"block_id": index + 2, "block_label": "text", "block_order": index + 2,
           "block_content": repeated} for index in range(3)],
    ])
    _write_result(ctx.work_dir, 3, [
        {"block_id": 1, "block_label": "text", "block_order": 1,
         "block_content": "normal prose " * 80},
    ])
    kinds = {finding.kind for finding in detect_novel_signals(ctx)}
    assert "novel_bad_character" in kinds
    assert "novel_repeated_block_loop" in kinds


def test_novel_repetition_ignores_legitimate_interleaved_standard_boilerplate(tmp_path):
    ctx = _context(tmp_path)
    blocks = []
    for index in range(4):
        blocks.extend([
            {"block_id": index * 2, "block_label": "text", "block_order": index * 2,
             "block_content": f"18.3.{index} A distinct normative requirement with enough text."},
            {"block_id": index * 2 + 1, "block_label": "text",
             "block_order": index * 2 + 1,
             "block_content": "Compliance is checked by inspection."},
        ])
    _write_result(ctx.work_dir, 1, blocks)
    _write_result(ctx.work_dir, 2, blocks)
    _write_result(ctx.work_dir, 3, blocks)
    assert not any(f.kind == "novel_repeated_block_loop"
                   for f in detect_novel_signals(ctx))
