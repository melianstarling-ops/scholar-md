from __future__ import annotations

import hashlib
import json
import threading
import time

import fitz

from scripts.pipelines.textbooks import checkpoint
from scripts.pipelines.textbooks import derived_cache as dc
from scripts.pipelines.textbooks.quality_repair import engine as repair_engine
from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec
from scripts.pipelines.textbooks.quality_repair.engine import (
    apply_document,
    audit_document,
    propose_document,
)
from scripts.pipelines.textbooks.quality_repair.detectors.unordered_blocks import (
    detect_unordered_blocks,
)
from scripts.pipelines.textbooks.quality_repair.detectors.formulas import detect_formulas
from scripts.pipelines.textbooks.quality_repair.gates import GateResult
from scripts.pipelines.textbooks.quality_repair.events import (
    DetectionBatch,
    RepairEvent,
)
from scripts.pipelines.textbooks.quality_repair.models import (
    DetectorContext,
    Severity,
    read_text_exact,
)
from scripts.pipelines.textbooks.quality_repair.registry import Capability, Registry
from scripts.pipelines.textbooks.quality_repair.detectors.final_delimiters import detect_final_delimiters
from scripts.pipelines.textbooks.vision_repair import content_fingerprint


def _context(tmp_path, text="$ x + 1 $\n"):
    doc = tmp_path / "out" / "Demo"
    doc.mkdir(parents=True)
    md = doc / "Demo.md"
    md.write_text(text, encoding="utf-8")
    work = tmp_path / "work" / "Demo" / "_work"
    work.mkdir(parents=True)
    (work / "manifest.json").write_text(json.dumps({
        "fingerprint": {"page_count": 1}, "failed_pages": []}), encoding="utf-8")
    (work / "page_0001_res.json").write_text(json.dumps({
        "parsing_res_list": [{"block_order": 1, "block_content": "x + 1"}]}),
        encoding="utf-8")
    return DetectorContext.from_paths(
        stem="Demo", md_path=md, work_dir=work,
        run_dir=tmp_path / "work" / "Demo" / "Demo_quality_repair" / "run-1")


def _registry():
    return Registry([Capability("final_delimiters", detect_final_delimiters)])


def _install_derived_cache(ctx, page_texts):
    records = []
    for page, text in enumerate(page_texts, 1):
        key = dc.build_cache_key(
            stem=ctx.stem,
            source_pdf_sha256="a" * 64,
            dpi=150,
            ocr_page_sha256=dc.sha256_text(f"ocr-{page}"),
            page_corrections=[],
            page_overlay=[],
            adoption_thresholds={},
            reconstruct_profile="reconstruct-v1",
            adoption_profile="route-b-v1",
        )
        record = dc.materialize_page_cache(
            page=page,
            cache_key=key,
            adopted_decisions=[],
            fragments=[{"block_ids": [page], "md": text[:-1]}],
            page_markdown=text,
        )
        dc.write_page_cache(ctx.work_dir, record)
        records.append(record)
    final_markdown = dc.assemble_document(records)
    ctx.md_path.write_text(final_markdown, encoding="utf-8", newline="")
    dc.write_document_index(
        ctx.work_dir,
        dc.build_document_index(records, final_markdown=final_markdown),
    )
    return records, final_markdown


def _cache_bytes(ctx):
    return {
        path.name: path.read_bytes()
        for path in sorted(dc.derived_dir(ctx.work_dir).glob("*.json"))
    }


def _refresh_context(ctx, *, run_dir=None):
    return DetectorContext.from_paths(
        stem=ctx.stem,
        md_path=ctx.md_path,
        work_dir=ctx.work_dir,
        run_dir=run_dir or ctx.run_dir,
    )


def _unordered_registry():
    return Registry([Capability("unordered_blocks", detect_unordered_blocks)])


def _add_unordered_blocks(ctx, count=1):
    blocks = [
        {"block_id": 1, "block_label": "text", "block_order": 1,
         "block_content": "ordered prose"},
    ]
    for index in range(count):
        blocks.append({
            "block_id": 10 + index,
            "block_label": "reference_content",
            "block_order": None,
            "block_bbox": [10, 100 + index * 20, 200, 115 + index * 20],
            "block_content": f"valuable missing note {index}",
        })
    (ctx.work_dir / "page_0001_res.json").write_text(
        json.dumps({"width": 1000, "parsing_res_list": blocks}),
        encoding="utf-8",
    )
    key = dc.build_cache_key(
        stem=ctx.stem,
        source_pdf_sha256="a" * 64,
        dpi=150,
        ocr_page_sha256=dc.sha256_text("unordered-page"),
        page_corrections=[],
        page_overlay=[],
        adoption_thresholds={},
        reconstruct_profile="reconstruct-v1",
        adoption_profile="route-b-v1",
    )
    record = dc.materialize_page_cache(
        page=1,
        cache_key=key,
        adopted_decisions=[],
        fragments=[{
            "block_ids": [block["block_id"] for block in blocks],
            "md": "ordered prose",
        }],
        page_markdown="ordered prose\n",
    )
    final_text = read_text_exact(ctx.md_path)
    newline_style = dc.detect_newline_style(final_text)
    assert dc.assemble_document(
        [record], newline_style=newline_style) == final_text
    dc.write_page_cache(ctx.work_dir, record)
    dc.write_document_index(
        ctx.work_dir,
        dc.build_document_index(
            [record], final_markdown=final_text),
    )


def _accepting_agent(calls):
    def invoke(spec, packet, timeout):
        calls.append((spec.provider, packet.finding_id))
        return json.dumps({
            "verdict": "accept", "issue_family": packet.issue_kind,
            "severity": packet.severity, "source_evidence": ["checked"],
            "target": dict(packet.target), "replacement": "",
            "confidence": 0.95, "generalizable": False,
        })
    return invoke


def test_audit_keeps_legacy_reports_and_adds_complete_bounded_events(tmp_path):
    ctx = _context(tmp_path)

    summary = audit_document(ctx, registry=_registry())

    event_lines = (ctx.run_dir / "events.jsonl").read_text(
        encoding="utf-8").splitlines()
    payload = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert len(event_lines) == 1
    assert (ctx.run_dir / "findings.jsonl").is_file()
    assert summary.finding_count == payload["finding_count"] == 1
    assert payload["events"]["event_count"] == 1
    assert payload["events"]["counts_by_route"] == {
        "deterministic:final_delimiters": 1,
    }


def test_audit_event_ledger_is_complete_while_summary_samples_are_bounded(tmp_path):
    ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(ctx, count=15)

    audit_document(ctx, registry=_unordered_registry())

    event_lines = (ctx.run_dir / "events.jsonl").read_text(
        encoding="utf-8").splitlines()
    payload = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert len(event_lines) == 15
    assert payload["events"]["event_count"] == 15
    assert len(payload["events"]["samples"]) == 10
    assert payload["events"]["samples_truncated"] is True


def test_audit_historical_formula_finding_does_not_keep_terminal_run_suspect(tmp_path):
    ctx = _context(tmp_path)
    ctx.selfcheck_path.write_text(json.dumps({
        "formula_suspicions": [{"page": 1, "block_id": 1, "reason": "bare_op"}],
    }), encoding="utf-8")
    repair = ctx.doc_work_dir / "Demo_repair"
    repair.mkdir()
    candidate = {
        "candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "engine_latex": "x + 1", "reasons": ["worklist:bare_op"],
    }
    (repair / "formula_candidates.jsonl").write_text(
        json.dumps(candidate) + "\n", encoding="utf-8")
    (repair / "formula_agent_verdicts.jsonl").write_text(json.dumps({
        "candidate_id": "p0001-b0001", "verdict": "accept",
        "latex": "x + 1", "confidence": 0.99,
        "content_fingerprint": content_fingerprint("x + 1"),
    }) + "\n", encoding="utf-8")

    summary = audit_document(
        ctx, registry=Registry([Capability("formulas", detect_formulas)]))
    payload = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))

    assert summary.finding_count == 1  # compatibility/history remains visible
    assert summary.status == payload["status"] == "OK"
    assert payload["events"]["event_count"] == 0
    assert payload["events"]["metrics"]["formula:terminal_candidates"] == 1


def test_audit_keeps_multiple_formula_suspicion_fallback_occurrences_unique(
        tmp_path):
    ctx = _context(tmp_path)
    ctx.selfcheck_path.write_text(json.dumps({
        "formula_suspicions": [
            {"page": 1, "block_id": 1, "reason": "bare_op"},
            {"page": 1, "block_id": 2, "reason": "unbalanced"},
        ],
    }), encoding="utf-8")

    audit_document(
        ctx, registry=Registry([Capability("formulas", detect_formulas)]))
    events = [
        json.loads(line)
        for line in (ctx.run_dir / "events.jsonl").read_text(
            encoding="utf-8").splitlines()
    ]

    assert len(events) == 2
    assert len({event["event_id"] for event in events}) == 2
    assert len({
        event["target"]["legacy_finding_id"] for event in events
    }) == 2


def test_propose_builds_safe_whitespace_patch_but_keeps_markdown_identical(tmp_path):
    ctx = _context(tmp_path)
    before = ctx.md_path.read_bytes()
    result = propose_document(ctx, registry=_registry(), agent_specs=[])
    assert len(result.patch_plan.proposals) == 1
    proposal = result.patch_plan.proposals[0]
    assert proposal.replacement == "$x + 1$"
    assert ctx.md_path.read_bytes() == before
    assert (ctx.run_dir / "proposals.jsonl").is_file()
    assert (ctx.run_dir / "patch_plan.json").is_file()


def test_propose_routes_known_unordered_event_by_explicit_route(tmp_path):
    ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(ctx)
    calls = []

    result = propose_document(
        ctx,
        registry=_unordered_registry(),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=_accepting_agent(calls),
    )

    assert len(calls) == 1
    assert result.event_batch.events[0].status == "ignored"
    tasks = [
        json.loads(line)
        for line in (ctx.run_dir / "agent_tasks.jsonl").read_text(
            encoding="utf-8").splitlines()
    ]
    assert tasks[0]["route"] == "quality_agent:block_review"
    assert tasks[0]["status"] == "ignored"


def test_propose_routes_missing_unordered_block_via_adjacent_fragment_anchor(
        tmp_path):
    """A genuinely omitted block has no fragment of its own.

    The production owner resolver must derive a zero-width insertion anchor
    from the nearest represented ordered block on the same page, rather than
    turning the actionable event into an unlocatable-owner blocker.
    """

    ctx = _context(tmp_path, "ordered prose\n\n")
    ctx.md_path.write_text("ordered prose\n\n", encoding="utf-8", newline="")
    (ctx.work_dir / "page_0001_res.json").write_text(json.dumps({
        "width": 1000,
        "parsing_res_list": [
            {
                "block_id": 1,
                "block_label": "text",
                "block_order": 1,
                "block_bbox": [10, 10, 200, 40],
                "block_content": "ordered prose",
            },
            {
                "block_id": 10,
                "block_label": "reference_content",
                "block_order": None,
                "block_bbox": [10, 100, 200, 120],
                "block_content": "valuable missing note",
            },
        ],
    }), encoding="utf-8")
    key = dc.build_cache_key(
        stem=ctx.stem,
        source_pdf_sha256="a" * 64,
        dpi=150,
        ocr_page_sha256=dc.sha256_text("production-unordered-page"),
        page_corrections=[],
        page_overlay=[],
        adoption_thresholds={},
        reconstruct_profile="reconstruct-v1",
        adoption_profile="route-b-v1",
    )
    record = dc.materialize_page_cache(
        page=1,
        cache_key=key,
        adopted_decisions=[],
        fragments=[{"block_ids": [1], "md": "ordered prose"}],
        page_markdown="ordered prose\n",
    )
    dc.write_page_cache(ctx.work_dir, record)
    dc.write_document_index(
        ctx.work_dir,
        dc.build_document_index([record], final_markdown="ordered prose\n\n"),
    )
    ctx = _refresh_context(ctx)
    calls = []

    def invoke(_spec, packet, _timeout):
        calls.append(packet)
        return json.dumps({
            "verdict": "repair",
            "issue_family": "missing-unordered-content",
            "severity": "P1",
            "source_evidence": ["page image and OCR block"],
            "target": dict(packet.target),
            "replacement": "\nvaluable missing note",
            "confidence": 0.99,
            "generalizable": False,
        })

    result = propose_document(
        ctx,
        registry=_unordered_registry(),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=invoke,
    )

    assert len(calls) == 1
    event = result.event_batch.events[0]
    assert event.route == "quality_agent:block_review"
    assert event.evidence["md_owner"] == "derived_insertion_anchor"
    assert event.evidence["anchor_block_id"] == 1
    assert event.target["md_start"] == event.target["md_end"] == len(
        "ordered prose")
    assert len(result.patch_plan.proposals) == 1
    proposal = result.patch_plan.proposals[0]
    assert proposal.md_start == proposal.md_end == len("ordered prose")


def test_apply_blocks_two_missing_unordered_blocks_at_shared_insertion_anchor(
        tmp_path):
    """Distinct omitted blocks at one anchor must not be silently reversed."""

    ctx = _context(tmp_path, "ordered prose\n\n")
    ctx.md_path.write_text("ordered prose\n\n", encoding="utf-8", newline="")
    (ctx.work_dir / "page_0001_res.json").write_text(json.dumps({
        "width": 1000,
        "parsing_res_list": [
            {
                "block_id": 1,
                "block_label": "text",
                "block_order": 1,
                "block_bbox": [10, 10, 200, 40],
                "block_content": "ordered prose",
            },
            {
                "block_id": 10,
                "block_label": "reference_content",
                "block_order": None,
                "block_bbox": [10, 100, 200, 120],
                "block_content": "first missing note",
            },
            {
                "block_id": 11,
                "block_label": "reference_content",
                "block_order": None,
                "block_bbox": [10, 140, 200, 160],
                "block_content": "second missing note",
            },
        ],
    }), encoding="utf-8")
    key = dc.build_cache_key(
        stem=ctx.stem,
        source_pdf_sha256="a" * 64,
        dpi=150,
        ocr_page_sha256=dc.sha256_text("two-missing-blocks"),
        page_corrections=[],
        page_overlay=[],
        adoption_thresholds={},
        reconstruct_profile="reconstruct-v1",
        adoption_profile="route-b-v1",
    )
    record = dc.materialize_page_cache(
        page=1,
        cache_key=key,
        adopted_decisions=[],
        fragments=[{"block_ids": [1], "md": "ordered prose"}],
        page_markdown="ordered prose\n",
    )
    dc.write_page_cache(ctx.work_dir, record)
    dc.write_document_index(
        ctx.work_dir,
        dc.build_document_index([record], final_markdown="ordered prose\n\n"),
    )
    ctx = _refresh_context(ctx)
    before = ctx.md_path.read_bytes()
    replacements = iter(("\nfirst missing note", "\nsecond missing note"))

    def invoke(_spec, packet, _timeout):
        return json.dumps({
            "verdict": "repair",
            "issue_family": "missing-unordered-content",
            "severity": "P1",
            "source_evidence": ["page image and OCR block"],
            "target": dict(packet.target),
            "replacement": next(replacements),
            "confidence": 0.99,
            "generalizable": False,
        })

    result = apply_document(
        ctx,
        registry=_unordered_registry(),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=invoke,
        gates=[lambda *_: GateResult("ok", True, "")],
        agent_workers=1,
    )

    assert len(result.proposal_run.patch_plan.conflicts) == 1
    conflict = result.proposal_run.patch_plan.conflicts[0]
    assert conflict.reason == "shared_insertion_anchor"
    assert len(conflict.proposal_ids) == 2
    assert result.proposal_run.patch_plan.proposals == ()
    assert result.transaction.applied == 0
    assert result.transaction.reason == "proposal conflict blocks apply"
    assert ctx.md_path.read_bytes() == before
    summary = json.loads(
        (ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "SUSPECT"


def test_propose_marks_block_event_without_unique_md_owner_as_blocker(tmp_path):
    ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(ctx)
    dc.derived_dir(ctx.work_dir).rename(
        dc.derived_dir(ctx.work_dir).with_name("_derived_removed"))
    calls = []

    result = propose_document(
        ctx,
        registry=_unordered_registry(),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=_accepting_agent(calls),
    )

    assert calls == []
    event = result.event_batch.events[0]
    assert event.status == "unresolved"
    assert event.route == "blocker:unlocatable_md_owner"
    assert event.evidence["md_owner"] == "unresolved"


def test_propose_formula_event_keeps_formula_route_with_unique_md_owner(tmp_path):
    ctx = _context(tmp_path)
    _install_derived_cache(ctx, ["$$ x+1 $$\n"])
    ctx = _refresh_context(ctx)
    (ctx.work_dir / "page_0001_res.json").write_text(json.dumps({
        "parsing_res_list": [{
            "block_id": 1, "block_order": 1,
            "block_label": "isolate_formula", "block_content": "$$ x+1 $$",
        }],
    }), encoding="utf-8")
    repair = ctx.doc_work_dir / "Demo_repair"
    repair.mkdir()
    (repair / "formula_candidates.jsonl").write_text(json.dumps({
        "candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "engine_latex": "$$ x+1 $$", "reasons": ["worklist:bare_op"],
    }) + "\n", encoding="utf-8")
    seen_targets = []

    def invoke(_spec, packet, _timeout):
        seen_targets.append(dict(packet.target))
        return json.dumps({
            "verdict": "repair", "issue_family": "formula-review",
            "severity": "P2", "source_evidence": ["crop"],
            "target": dict(packet.target), "replacement": "$$ x+2 $$\n",
            "confidence": 0.95, "generalizable": False,
        })

    result = propose_document(
        ctx,
        registry=Registry([Capability("formulas", detect_formulas)]),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=invoke,
    )

    event = result.event_batch.events[0]
    assert event.route == "formula_agent"
    assert event.target["md_start"] == 0
    assert event.target["md_end"] > event.target["md_start"]
    assert seen_targets == [dict(event.target)]
    assert len(result.patch_plan.proposals) == 1


def test_propose_never_routes_deterministic_event_to_agent(tmp_path):
    ctx = _context(tmp_path)
    calls = []

    result = propose_document(
        ctx,
        registry=_registry(),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=_accepting_agent(calls),
    )

    assert calls == []
    assert result.event_batch.events[0].route == "deterministic:final_delimiters"
    assert (ctx.run_dir / "agent_tasks.jsonl").read_text(encoding="utf-8") == ""


def test_propose_marks_agent_events_unresolved_when_no_agent_configured(tmp_path):
    ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(ctx)

    result = propose_document(
        ctx, registry=_unordered_registry(), agent_specs=[]
    )

    assert result.event_batch.events[0].status == "unresolved_no_agent"
    payload = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert payload["events"]["counts_by_status"] == {"unresolved_no_agent": 1}
    task = json.loads((ctx.run_dir / "agent_tasks.jsonl").read_text(
        encoding="utf-8"))
    assert task["status"] == "unresolved_no_agent"


def test_propose_routes_fresh_source_audit_event_without_rerunning_audit(tmp_path):
    ctx = _context(tmp_path, "table value 7\n")
    _install_derived_cache(ctx, ["table value 7\n"])
    ctx = _refresh_context(ctx)
    (ctx.work_dir / "page_0001_res.json").write_text(json.dumps({
        "parsing_res_list": [{
            "block_id": 1,
            "block_order": 1,
            "block_label": "table",
            "block_bbox": [10, 20, 300, 80],
            "block_content": "table value 7",
        }],
    }), encoding="utf-8")
    pdf_path = ctx.doc_work_dir / "Demo.pdf"
    document = fitz.open()
    try:
        document.new_page()
        document.save(pdf_path)
    finally:
        document.close()
    pdf_bytes = pdf_path.read_bytes()
    manifest = json.loads(
        (ctx.work_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest.update({"dpi": 150, "pdf_path": str(pdf_path)})
    (ctx.work_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    report = {
        "schema_version": 6,
        "stem": "Demo",
        "pdf_fingerprint": {
            "size_bytes": len(pdf_bytes),
            "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "page_count": 1,
        },
        "ocr_fingerprint": checkpoint.ocr_results_fingerprint(
            str(ctx.work_dir), 1, 150),
        "summary": {
            "status": "SUSPECT",
            "pages": 1,
            "issue_counts": {"numeric_mismatch": 1},
        },
        "pages": [{
            "page": 1,
            "status": "SUSPECT",
            "issues": [{
                "code": "numeric_mismatch",
                "block_id": 1,
                "detail": "source=1 output=7",
            }],
        }],
    }
    (ctx.doc_work_dir / "Demo_source_audit.json").write_text(
        json.dumps(report), encoding="utf-8")

    result = propose_document(ctx, registry=Registry([]), agent_specs=[])

    assert result.summary.status == "SUSPECT"
    assert len(result.event_batch.events) == 1
    event = result.event_batch.events[0]
    assert event.capability == "source_audit"
    assert event.status == "unresolved_no_agent"
    assert event.target["md_start"] == 0
    assert event.target["md_end"] == len("table value 7")
    task = json.loads((ctx.run_dir / "agent_tasks.jsonl").read_text(
        encoding="utf-8"))
    assert task["route"] == "quality_agent:source_grounded_repair"
    assert task["status"] == "unresolved_no_agent"


def test_source_grounded_agent_repair_stages_raw_block_correction_not_md_patch(
        tmp_path, monkeypatch):
    ctx = _context(tmp_path, "table value 7\n")
    raw = {
        "block_id": 7,
        "block_order": 1,
        "block_label": "table",
        "block_content": "table value 7",
    }
    (ctx.work_dir / "page_0001_res.json").write_text(
        json.dumps({"parsing_res_list": [raw]}), encoding="utf-8")
    event = RepairEvent.create(
        capability="source_audit",
        kind="source_audit_numeric_mismatch",
        severity=Severity.P1,
        route="quality_agent:source_grounded_repair",
        input_fingerprint="source-input",
        page=1,
        block_id=7,
        target={
            "scope": "block",
            "page": 1,
            "block_id": 7,
            "md_start": 0,
            "md_end": len("table value 7"),
        },
        evidence={"source_grounded": True},
        message="source says value 1",
    )
    batch = DetectionBatch.create(
        stem=ctx.stem,
        baseline_sha256=ctx.baseline_sha256,
        events=[event],
    )
    monkeypatch.setattr(
        repair_engine, "_collect_event_batch",
        lambda *_args, **_kwargs: batch,
    )

    def invoke(_spec, packet, _timeout):
        return json.dumps({
            "verdict": "repair",
            "issue_family": "source_numeric_mismatch",
            "severity": "P1",
            "source_evidence": ["source page"],
            "target": {"scope": "block", "page": 1, "block_id": 7},
            "replacement": "table value 1",
            "confidence": 0.99,
            "generalizable": False,
        })

    result = propose_document(
        ctx,
        registry=Registry([]),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=invoke,
    )

    assert result.patch_plan.proposals == ()
    assert result.event_batch.events[0].status == "accepted"
    assert result.block_corrections == ({
        "page": 1,
        "block_id": 7,
        "status": "accepted",
        "content_fingerprint": content_fingerprint("table value 7"),
        "corrected_latex": "table value 1",
        "producer": "agent:fake:model:high",
    },)
    persisted = json.loads(
        (ctx.run_dir / "block_corrections.jsonl").read_text(encoding="utf-8"))
    assert persisted == result.block_corrections[0]


def test_propose_marks_budget_excess_explicitly_unresolved(tmp_path):
    ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(ctx, count=2)
    calls = []

    result = propose_document(
        ctx,
        registry=_unordered_registry(),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=_accepting_agent(calls),
        max_agent_items=1,
    )

    assert len(calls) == 1
    assert {event.status for event in result.event_batch.events} == {
        "ignored", "not_routed_due_budget",
    }


def test_event_agents_run_in_parallel_but_each_event_keeps_provider_fallback_order(tmp_path):
    ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(ctx, count=3)
    lock = threading.Lock()
    active = 0
    max_active = 0
    providers_by_event = {}

    def invoke(spec, packet, timeout):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            providers_by_event.setdefault(packet.finding_id, []).append(spec.provider)
        time.sleep(0.04)
        with lock:
            active -= 1
        verdict = "uncertain" if spec.provider == "first" else "accept"
        return json.dumps({
            "verdict": verdict, "issue_family": packet.issue_kind,
            "severity": packet.severity, "source_evidence": ["checked"],
            "target": dict(packet.target), "replacement": "",
            "confidence": 0.8, "generalizable": False,
        })

    propose_document(
        ctx,
        registry=_unordered_registry(),
        agent_specs=[
            AgentSpec.parse("first:model:high"),
            AgentSpec.parse("second:model:high"),
        ],
        invoke=invoke,
        agent_workers=3,
    )

    assert max_active >= 2
    assert all(chain == ["first", "second"]
               for chain in providers_by_event.values())
    tasks = [
        json.loads(line)
        for line in (ctx.run_dir / "agent_tasks.jsonl").read_text(
            encoding="utf-8").splitlines()
    ]
    ledger = [
        json.loads(line)
        for line in (ctx.run_dir / "agent_ledger.jsonl").read_text(
            encoding="utf-8").splitlines()
    ]
    assert [item["event_id"] for item in tasks] == [
        item["event_id"] for item in ledger
    ]


def test_same_page_agent_events_render_source_evidence_once(tmp_path, monkeypatch):
    ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(ctx, count=2)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"test-placeholder")
    manifest_path = ctx.work_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pdf_path"] = str(pdf)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    render_calls = []

    def fake_render(pdf_path, page, out_dir, dpi):
        render_calls.append((pdf_path, page, dpi))
        target = __import__("pathlib").Path(out_dir) / f"page_{page:04d}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        return str(target)

    monkeypatch.setattr(
        "scripts.pipelines.textbooks.preprocess.pdf_page_to_png", fake_render)
    image_packets = []

    def invoke(spec, packet, timeout):
        image_packets.append(packet.image_paths)
        return json.dumps({
            "verdict": "accept", "issue_family": packet.issue_kind,
            "severity": packet.severity, "source_evidence": ["checked"],
            "target": dict(packet.target), "replacement": "",
            "confidence": 0.9, "generalizable": False,
        })

    propose_document(
        ctx,
        registry=_unordered_registry(),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=invoke,
        agent_workers=2,
    )

    assert len(render_calls) == 1
    assert len(image_packets) == 2
    assert image_packets[0] == image_packets[1]


def test_terminal_event_ledger_is_reused_only_for_same_input_fingerprint(tmp_path):
    first_ctx = _context(tmp_path, "ordered prose\n\n")
    _add_unordered_blocks(first_ctx)
    calls = []
    specs = [AgentSpec.parse("fake:model:high")]

    first = propose_document(
        first_ctx, registry=_unordered_registry(), agent_specs=specs,
        invoke=_accepting_agent(calls),
    )
    assert len(calls) == 1
    assert first.event_batch.events[0].status == "ignored"

    second_ctx = DetectorContext.from_paths(
        stem="Demo", md_path=first_ctx.md_path, work_dir=first_ctx.work_dir,
        run_dir=first_ctx.run_dir.parent / "run-2",
    )
    second = propose_document(
        second_ctx, registry=_unordered_registry(), agent_specs=specs,
        invoke=lambda *_: (_ for _ in ()).throw(AssertionError("must reuse terminal")),
    )
    assert second.event_batch.events[0].status == "ignored"
    reused = json.loads((second_ctx.run_dir / "agent_tasks.jsonl").read_text(
        encoding="utf-8"))
    assert reused["reused_terminal"] is True

    page_path = first_ctx.work_dir / "page_0001_res.json"
    payload = json.loads(page_path.read_text(encoding="utf-8"))
    payload["parsing_res_list"][1]["block_content"] = "changed source note"
    page_path.write_text(json.dumps(payload), encoding="utf-8")
    third_ctx = DetectorContext.from_paths(
        stem="Demo", md_path=first_ctx.md_path, work_dir=first_ctx.work_dir,
        run_dir=first_ctx.run_dir.parent / "run-3",
    )
    third_calls = []
    third = propose_document(
        third_ctx, registry=_unordered_registry(), agent_specs=specs,
        invoke=_accepting_agent(third_calls),
    )
    assert len(third_calls) == 1
    assert third.event_batch.events[0].input_fingerprint != \
        first.event_batch.events[0].input_fingerprint


def test_propose_without_agent_specs_makes_zero_external_calls(tmp_path):
    ctx = _context(tmp_path, "plain text\n")
    called = False

    def invoke(*args):
        nonlocal called
        called = True
        raise AssertionError

    propose_document(ctx, registry=_registry(), agent_specs=[], invoke=invoke)
    assert called is False


def test_propose_routes_novel_finding_to_only_explicit_agent(tmp_path):
    ctx = _context(tmp_path, "broken\n")

    def novel(_):
        from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
        return [Finding.create(
            capability="novel_discovery", kind="novel_gap", severity=Severity.P1,
            message="gap", target={"md_start": 0, "md_end": 6},
            evidence={"source": "crop"})]

    calls = []

    def invoke(spec, packet, timeout):
        calls.append(spec.provider)
        return json.dumps({
            "verdict": "repair", "issue_family": "novel-gap", "severity": "P1",
            "source_evidence": ["crop"], "target": {"md_start": 0, "md_end": 6},
            "replacement": "fixed!", "confidence": 0.9, "generalizable": True,
        })

    result = propose_document(
        ctx, registry=Registry([Capability("novel_discovery", novel)]),
        agent_specs=[AgentSpec.parse("fake:model:high")], invoke=invoke)
    assert calls == ["fake"]
    assert result.patch_plan.proposals[0].producer == "agent:fake:model:high"
    assert ctx.md_path.read_text(encoding="utf-8") == "broken\n"


def test_apply_document_uses_transaction_after_propose(tmp_path):
    ctx = _context(tmp_path)
    result = apply_document(
        ctx, registry=_registry(), agent_specs=[],
        gates=[lambda *_: GateResult("ok", True, "")])
    assert result.transaction.applied == 1
    assert ctx.md_path.read_text(encoding="utf-8") == "$x + 1$\n"
    assert result.after_findings == ()
    assert (ctx.run_dir / "after_findings.jsonl").is_file()
    assert (ctx.run_dir / "after_events.jsonl").read_text(encoding="utf-8") == ""
    assert (ctx.run_dir / "validation.json").is_file()
    validation = json.loads((ctx.run_dir / "validation.json").read_text(encoding="utf-8"))
    assert validation["before_events"] == 1
    assert validation["after_events"] == 0
    summary = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "OK"
    assert summary["terminal"]["unresolved_events"] == 0
    assert summary["terminal"]["transaction_ok"] is True


def test_apply_document_failed_gate_keeps_final_summary_suspect(tmp_path):
    ctx = _context(tmp_path)

    result = apply_document(
        ctx, registry=_registry(), agent_specs=[],
        gates=[lambda *_: GateResult("blocked", False, "regression")])
    summary = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))

    assert result.transaction.rolled_back is True
    assert summary["status"] == "SUSPECT"
    assert summary["terminal"]["transaction_ok"] is False


def test_apply_joins_same_input_ignored_terminal_into_after_events(tmp_path):
    ctx = _context(tmp_path, "stable text\n")

    def detector(_):
        from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
        return [Finding.create(
            capability="novel_discovery", kind="visual_false_positive",
            severity=Severity.P1, message="review",
            target={"md_start": 0, "md_end": 6},
        )]

    result = apply_document(
        ctx,
        registry=Registry([Capability("novel_discovery", detector)]),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=_accepting_agent([]),
        gates=[],
    )
    after_event = json.loads((ctx.run_dir / "after_events.jsonl").read_text(
        encoding="utf-8"))
    summary = json.loads((ctx.run_dir / "summary.json").read_text(
        encoding="utf-8"))

    assert result.transaction.reason == "empty patch plan"
    assert after_event["status"] == "ignored"
    assert summary["after_events"]["unresolved"] == 0
    assert summary["status"] == "OK"


def test_apply_document_reconciles_single_page_edit_into_derived_cache(tmp_path):
    ctx = _context(tmp_path)
    _install_derived_cache(ctx, ["$ x + 1 $\n", "second page\n"])
    ctx = _refresh_context(ctx)

    result = apply_document(
        ctx, registry=_registry(), agent_specs=[],
        gates=[lambda *_: GateResult("ok", True, "")])

    assert result.transaction.applied == 1
    assert result.transaction.rolled_back is False
    final_text = ctx.md_path.read_text(encoding="utf-8")
    index = dc.read_document_index(
        ctx.work_dir, expected_final_sha256=dc.sha256_text(final_text))
    records = [
        dc.read_page_cache(ctx.work_dir, 1),
        dc.read_page_cache(ctx.work_dir, 2),
    ]
    assert index is not None
    assert all(record is not None for record in records)
    assert dc.assemble_document(records) == final_text
    assert records[0]["page_overlays"][-1]["kind"] == "exact_page_replacement"
    assert records[1]["page_overlays"] == []


def test_apply_document_reconciles_crlf_without_normalizing_published_bytes(tmp_path):
    ctx = _context(tmp_path)
    records, final_lf = _install_derived_cache(ctx, ["$ x + 1 $\n"])
    final_crlf = final_lf.replace("\n", "\r\n")
    ctx.md_path.write_text(final_crlf, encoding="utf-8", newline="")
    dc.write_document_index(
        ctx.work_dir,
        dc.build_document_index(records, final_markdown=final_crlf),
    )
    ctx = _refresh_context(ctx)

    result = apply_document(
        ctx, registry=_registry(), agent_specs=[],
        gates=[lambda *_: GateResult("ok", True, "")])

    assert result.transaction.applied == 1
    after = ctx.md_path.read_bytes()
    assert b"\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b"")
    index = dc.read_document_index(
        ctx.work_dir, expected_final_sha256=hashlib.sha256(after).hexdigest())
    assert index is not None
    assert index["newline_style"] == dc.NEWLINE_CRLF
    record = dc.read_page_cache(ctx.work_dir, 1)
    assert record is not None
    assert "\r" not in record["page_markdown"]
    assert dc.assemble_document(
        [record], newline_style=index["newline_style"]).encode("utf-8") == after


def test_apply_document_cross_page_reconcile_failure_rolls_back_md_and_cache(tmp_path):
    ctx = _context(tmp_path)
    _, before = _install_derived_cache(ctx, ["first page\n", "second page\n"])
    ctx = _refresh_context(ctx)
    before_cache = _cache_bytes(ctx)
    boundary_start = before.index("\n\n\n")
    boundary_end = boundary_start + len("\n\n\n")

    def boundary_finding(_):
        from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
        return [Finding.create(
            capability="novel_discovery", kind="cross_page_edit",
            severity=Severity.P1, message="boundary repair",
            target={"md_start": boundary_start, "md_end": boundary_end},
        )]

    def invoke(*_):
        return json.dumps({
            "verdict": "repair", "issue_family": "cross-page",
            "severity": "P1", "source_evidence": ["test"],
            "target": {"md_start": boundary_start, "md_end": boundary_end},
            "replacement": "\n", "confidence": 0.99, "generalizable": False,
        })

    result = apply_document(
        ctx,
        registry=Registry([Capability("novel_discovery", boundary_finding)]),
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=invoke,
        gates=[lambda *_: GateResult("ok", True, "")],
    )

    assert result.transaction.applied == 0
    assert result.transaction.rolled_back is True
    assert "derived cache" in result.transaction.reason
    assert ctx.md_path.read_text(encoding="utf-8") == before
    assert _cache_bytes(ctx) == before_cache
    summary = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "SUSPECT"


def test_apply_document_cache_write_failure_rolls_back_md_and_cache(
        tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    _, before = _install_derived_cache(ctx, ["$ x + 1 $\n"])
    ctx = _refresh_context(ctx)
    before_cache = _cache_bytes(ctx)

    def fail_index_write(*_args, **_kwargs):
        raise OSError("simulated index write failure")

    monkeypatch.setattr(dc, "write_document_index", fail_index_write)
    result = apply_document(
        ctx, registry=_registry(), agent_specs=[],
        gates=[lambda *_: GateResult("ok", True, "")])

    assert result.transaction.applied == 0
    assert result.transaction.rolled_back is True
    assert ctx.md_path.read_text(encoding="utf-8") == before
    assert _cache_bytes(ctx) == before_cache
    summary = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "SUSPECT"


def test_apply_document_derived_cache_reconcile_is_idempotent(tmp_path):
    first_ctx = _context(tmp_path)
    _install_derived_cache(first_ctx, ["$ x + 1 $\n"])
    first_ctx = _refresh_context(first_ctx)
    first = apply_document(
        first_ctx, registry=_registry(), agent_specs=[],
        gates=[lambda *_: GateResult("ok", True, "")])
    after_first_cache = _cache_bytes(first_ctx)
    after_first_md = first_ctx.md_path.read_bytes()
    second_ctx = DetectorContext.from_paths(
        stem=first_ctx.stem,
        md_path=first_ctx.md_path,
        work_dir=first_ctx.work_dir,
        run_dir=first_ctx.run_dir.parent / "run-2",
    )

    second = apply_document(
        second_ctx, registry=_registry(), agent_specs=[],
        gates=[lambda *_: GateResult("ok", True, "")])

    assert first.transaction.applied == 1
    assert second.transaction.reason == "empty patch plan"
    assert second.transaction.rolled_back is False
    assert second_ctx.md_path.read_bytes() == after_first_md
    assert _cache_bytes(second_ctx) == after_first_cache



def test_apply_document_p0_finding_blocks_all_writes(tmp_path):
    ctx = _context(tmp_path)
    before = hashlib.sha256(ctx.md_path.read_bytes()).hexdigest()

    def p0(_):
        from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
        return [Finding.create(capability="p0", kind="missing", severity=Severity.P0,
                               message="missing")]

    result = apply_document(
        ctx, registry=Registry([Capability("p0", p0)]), agent_specs=[], gates=[])
    assert result.transaction.applied == 0
    assert "P0" in result.transaction.reason
    assert hashlib.sha256(ctx.md_path.read_bytes()).hexdigest() == before


def test_confirmed_novel_decision_writes_learning_package_when_enabled(tmp_path):
    ctx = _context(tmp_path, "broken\n")

    def novel(_):
        from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
        return [Finding.create(
            capability="novel_discovery", kind="novel_gap", severity=Severity.P1,
            message="gap", evidence={"source": "crop"})]

    def invoke(*_):
        return json.dumps({
            "verdict": "novel", "issue_family": "lost-side-caption",
            "severity": "P1", "source_evidence": ["visible in crop"],
            "target": {}, "replacement": "", "confidence": 0.95,
            "generalizable": True,
        })

    result = propose_document(
        ctx, registry=Registry([Capability("novel_discovery", novel)]),
        agent_specs=[AgentSpec.parse("fake:model:high")], invoke=invoke,
        learn="package")
    package = ctx.run_dir / "learning_packages" / result.findings[0].finding_id
    expected = {"finding.json", "evidence_manifest.json", "current_md.txt",
                "expected_behavior.md", "fixture_plan.md", "test_plan.md",
                "lesson_draft.md", "development_brief.md"}
    assert {path.name for path in package.iterdir()} == expected


def test_agent_repair_outside_detector_target_is_not_proposed(tmp_path):
    ctx = _context(tmp_path, "broken safe\n")

    def novel(_):
        from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
        return [Finding.create(
            capability="novel_discovery", kind="novel_gap", severity=Severity.P1,
            message="gap", target={"md_start": 0, "md_end": 6})]

    def invoke(*_):
        return json.dumps({
            "verdict": "repair", "issue_family": "gap", "severity": "P1",
            "source_evidence": ["claim"], "target": {"md_start": 7, "md_end": 11},
            "replacement": "HACK", "confidence": 0.99, "generalizable": False,
        })

    result = propose_document(
        ctx, registry=Registry([Capability("novel_discovery", novel)]),
        agent_specs=[AgentSpec.parse("fake:model:high")], invoke=invoke)
    assert result.patch_plan.proposals == ()


def test_learning_package_copies_private_image_into_package(tmp_path):
    ctx = _context(tmp_path, "broken\n")
    image = tmp_path / "source.png"
    image.write_bytes(b"png-evidence")

    def novel(_):
        from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
        return [Finding.create(
            capability="novel_discovery", kind="novel_gap", severity=Severity.P1,
            message="gap", evidence={"image_paths": [str(image)]})]

    def invoke(*_):
        return json.dumps({
            "verdict": "novel", "issue_family": "visual-gap", "severity": "P1",
            "source_evidence": ["crop"], "target": {}, "replacement": "",
            "confidence": 0.9, "generalizable": True,
        })

    result = propose_document(
        ctx, registry=Registry([Capability("novel_discovery", novel)]),
        agent_specs=[AgentSpec.parse("fake:model:high")], invoke=invoke,
        learn="package")
    package = ctx.run_dir / "learning_packages" / result.findings[0].finding_id
    manifest = json.loads((package / "evidence_manifest.json").read_text(encoding="utf-8"))
    copied = [__import__("pathlib").Path(value) for value in manifest["image_paths"]]
    assert len(copied) == 1 and copied[0].is_file()
    assert copied[0].parent == package / "evidence"
    assert copied[0].read_bytes() == b"png-evidence"
