from __future__ import annotations

import json
import os
from types import SimpleNamespace
from pathlib import Path

import fitz
import pytest

from scripts.pipelines.textbooks import auto_repair as ar
from scripts.pipelines.textbooks import convert as cv
from scripts.pipelines.textbooks import source_audit as sa
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.quality_repair.models import (
    Finding, PatchPlan, Proposal, Severity,
)


def _layout(tmp_path: Path):
    layout = resolve_layout("Demo", str(tmp_path / "out"), str(tmp_path / "work"))
    Path(layout.doc_deliverable_dir).mkdir(parents=True)
    Path(layout.work_dir).mkdir(parents=True)
    with Path(layout.md_path).open("w", encoding="utf-8", newline="") as handle:
        handle.write("before\n")
    Path(layout.corrections_path).parent.mkdir(parents=True, exist_ok=True)
    Path(layout.corrections_path).write_text(
        json.dumps({"stem": "Demo", "corrections": []}),
        encoding="utf-8",
    )
    return layout


def _formula_payload():
    correction = {
        "page": 1, "block_id": 7, "status": "accepted",
        "content_fingerprint": "old", "corrected_latex": "$$ x $$",
    }
    return {
        "mode": "agents-apply",
        "formula_candidates": {"status": "ok", "count": 1},
        "agents": {
            "status": "ok", "run_mode": "apply", "pending_ids": [],
            "rejected": 0, "circuit_broken": False, "rolled_back": False,
            "corrections_payload": {
                "stem": "Demo", "corrections": [correction],
            },
        },
    }


def test_stage_source_audit_seeds_new_run_from_formal_recorded_pages(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    formal = {
        "schema_version": sa.SOURCE_AUDIT_SCHEMA_VERSION,
        "stem": layout.stem,
        "route": "B",
        "born_digital_mode": "hybrid",
        "adoption_source": "recorded",
        "pages": [],
    }
    Path(layout.source_audit_path).write_text(
        json.dumps(formal), encoding="utf-8")
    captured = {}

    def fake_audit(
            _pdf_path, _layout, _thresholds, decisions_by_page,
            born_digital_mode=None, **kwargs):
        captured["decisions"] = decisions_by_page
        captured["mode"] = born_digital_mode
        captured["prior"] = kwargs["prior_report"]
        return formal

    monkeypatch.setattr(sa, "audit_document", fake_audit)
    correction_path, report_path = ar._stage_source_audit(
        layout=layout,
        pdf_path="source.pdf",
        run_dir=tmp_path / "new-run",
        corrections_payload={"stem": layout.stem, "corrections": []},
        page_records=[{
            "page": 1,
            "adoption_decisions": [{
                "block_id": 7,
                "content_source": "source_text",
                "reasons": [],
                "block_ned": 0.0,
                "adopted_text": "accepted prose",
            }],
        }],
    )

    assert correction_path.is_file()
    assert report_path.is_file()
    assert captured["prior"] == formal
    assert captured["mode"] == "hybrid"
    assert captured["decisions"][1][0].block_id == 7
    assert captured["decisions"][1][0].adopted_text == "accepted prose"


def test_quality_failure_keeps_formula_md_corrections_and_cache_unpublished(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    before_md = Path(layout.md_path).read_bytes()
    before_corrections = Path(layout.corrections_path).read_bytes()
    cache_marker = Path(layout.work_dir) / "_derived_v1" / "page_0001.json"
    cache_marker.parent.mkdir()
    cache_marker.write_bytes(b"old-cache")

    import scripts.pipelines.textbooks.convert as convert
    monkeypatch.setattr(convert, "_run_formula_repair",
                        lambda *args, **kwargs: _formula_payload())
    monkeypatch.setattr(
        convert, "_build_reassembled_result",
        lambda *args, **kwargs: ({
            "md": "formula candidate\n",
            "_page_cache_records": [{"page": 1}],
        }, False),
    )
    monkeypatch.setattr(
        ar, "_quality_candidate",
        lambda **kwargs: (
            "quality candidate\n", [{"round": 1}],
            "gate injected failure",
        ),
    )
    committed = []
    monkeypatch.setattr(ar, "_commit",
                        lambda **kwargs: committed.append(kwargs))

    result = ar.run_unified_auto_repair(
        layout, "source.pdf", dpi=150,
        formula_mode="agents-apply", formula_agent_specs=[],
        quality_agents=[], discovery="signals", learn="off",
        timeout=30, workers=2, max_rounds=2,
    )

    assert result["status"] == "SUSPECT"
    assert result["published"] is False
    assert committed == []
    assert Path(layout.md_path).read_bytes() == before_md
    assert Path(layout.corrections_path).read_bytes() == before_corrections
    assert cache_marker.read_bytes() == b"old-cache"


def test_baseline_formula_candidate_uses_short_temporary_delivery_root(
        tmp_path, monkeypatch):
    stem = "EMC_Principle_Analysis_and_Design_Lin_Hannian"
    layout = resolve_layout(
        stem, str(tmp_path / "out"), str(tmp_path / "work"))
    Path(layout.work_dir).mkdir(parents=True)
    Path(layout.corrections_path).write_text(
        json.dumps({"stem": stem, "corrections": []}),
        encoding="utf-8",
    )
    captured = {}

    import scripts.pipelines.textbooks.convert as convert

    def formula(candidate_layout, *_args, **_kwargs):
        candidate = Path(candidate_layout.md_path)
        captured["root"] = Path(candidate_layout.deliverables_root)
        captured["candidate"] = candidate
        assert candidate.read_text(encoding="utf-8") == "all pages\n"
        return {
            "mode": "agents-apply",
            "formula_candidates": {"status": "ok", "count": 0},
            "agents": {
                "status": "ok",
                "pending_ids": [],
                "rejected": 0,
                "circuit_broken": False,
                "rolled_back": False,
                "corrections_payload": {
                    "stem": stem,
                    "corrections": [],
                },
            },
        }

    monkeypatch.setattr(convert, "_run_formula_repair", formula)
    monkeypatch.setattr(
        ar,
        "_quality_candidate",
        lambda **_kwargs: ("all pages\n", [], "injected stop"),
    )

    result = ar.run_unified_auto_repair(
        layout,
        "source.pdf",
        dpi=150,
        formula_mode="agents-apply",
        formula_agent_specs=[],
        quality_agents=[],
        discovery="signals",
        learn="off",
        timeout=30,
        workers=1,
        max_rounds=1,
        baseline_result={
            "md": "all pages\n",
            "_page_cache_records": [{"page": 1}],
        },
    )

    assert result["status"] == "SUSPECT"
    assert "_formula_candidate_root" not in str(captured["root"])
    assert captured["candidate"].name == f"{stem}.md"
    assert not captured["root"].exists()


def test_atomic_json_writers_use_short_temp_names_near_windows_limit(
        tmp_path):
    parent = tmp_path
    filename = "staged_source_audit.json"
    while len(str(parent / filename)) < 235:
        parent /= "path-segment-123"
    parent.mkdir(parents=True)

    auto_target = parent / filename
    cache_target = parent / "page_0001.json"
    ar._atomic_write_json(auto_target, {"writer": "auto"})
    ar.dc._atomic_write_json(cache_target, {"writer": "cache"})

    assert json.loads(auto_target.read_text(encoding="utf-8")) == {
        "writer": "auto"}
    assert json.loads(cache_target.read_text(encoding="utf-8")) == {
        "writer": "cache"}


def test_formula_and_quality_same_page_conflict_never_publishes(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    import scripts.pipelines.textbooks.convert as convert
    monkeypatch.setattr(convert, "_run_formula_repair",
                        lambda *args, **kwargs: _formula_payload())
    seen = {}

    def build(*args, **kwargs):
        seen["affected_pages"] = kwargs["affected_pages"]
        return ({
            "md": "formula page\n",
            "_page_cache_records": [{"page": 1}],
        }, False)

    monkeypatch.setattr(convert, "_build_reassembled_result", build)
    monkeypatch.setattr(
        ar, "_quality_candidate",
        lambda **kwargs: (
            kwargs["initial"], [{"round": 1, "conflicts": 1}],
            "proposal conflict blocks unified publication",
        ),
    )
    monkeypatch.setattr(
        ar, "_commit",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("conflicted candidate must not commit")),
    )

    result = ar.run_unified_auto_repair(
        layout, "source.pdf", dpi=150,
        formula_mode="agents-apply", formula_agent_specs=[],
        quality_agents=[], discovery="signals", learn="off",
        timeout=30, workers=2, max_rounds=2,
    )

    assert seen["affected_pages"] == {1}
    assert result["published"] is False
    assert "conflict" in result["reason"]
    assert Path(layout.md_path).read_text(encoding="utf-8") == "before\n"


def test_unified_commit_receives_quality_staged_corrections_and_rebuilt_records(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    staged_correction = {
        "page": 2,
        "block_id": 7,
        "status": "accepted",
        "content_fingerprint": "raw",
        "corrected_latex": "value 1",
        "producer": "agent:fake:model:high",
    }
    rebuilt_record = {"page": 2, "record_sha256": "rebuilt"}
    import scripts.pipelines.textbooks.convert as convert
    monkeypatch.setattr(convert, "_run_formula_repair", lambda *_a, **_k: {
        "mode": "agents-apply",
        "formula_candidates": {"status": "ok", "count": 0},
        "agents": {
            "status": "ok",
            "pending_ids": [],
            "rejected": 0,
            "circuit_broken": False,
            "rolled_back": False,
            "corrections_payload": {
                "stem": "Demo",
                "corrections": [],
            },
        },
    })
    monkeypatch.setattr(
        convert,
        "_build_reassembled_result",
        lambda *_a, **_k: ({
            "md": "candidate\n",
            "_page_cache_records": [{"page": 1}],
        }, False),
    )
    monkeypatch.setattr(
        ar,
        "_quality_candidate",
        lambda **_kwargs: ar.QualityCandidateResult(
            markdown="source fixed\n",
            rounds=({"round": 1},),
            reason=None,
            corrections_payload={
                "stem": "Demo",
                "corrections": [staged_correction],
            },
            page_records=(rebuilt_record,),
        ),
    )
    monkeypatch.setattr(
        ar.dc,
        "reconcile_page_overlays",
        lambda records, **_kwargs: list(records),
    )
    monkeypatch.setattr(ar, "_run_gates", lambda *_a, **_k: ())
    monkeypatch.setattr(ar, "_absolute_final_gates", lambda *_a, **_k: ())
    committed = []
    monkeypatch.setattr(
        ar,
        "_commit",
        lambda **kwargs: committed.append(kwargs) or True,
    )

    result = ar.run_unified_auto_repair(
        layout,
        "source.pdf",
        dpi=150,
        formula_mode="agents-apply",
        formula_agent_specs=[],
        quality_agents=["fake:model:high"],
        discovery="signals",
        learn="off",
        timeout=30,
        workers=1,
        max_rounds=1,
    )

    assert result["status"] == "OK"
    assert result["published"] is True
    assert committed[0]["final_markdown"] == "source fixed\n"
    assert committed[0]["corrections_payload"]["corrections"] == [
        staged_correction]
    assert committed[0]["page_records"] == [rebuilt_record]


def test_success_replaces_formal_markdown_exactly_once(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(ar.dc, "snapshot_cache_directory", lambda _work: {})
    monkeypatch.setattr(ar.dc, "write_page_cache",
                        lambda _work, _record: None)
    monkeypatch.setattr(ar.dc, "build_document_index",
                        lambda _records, final_markdown: {"ok": final_markdown})
    monkeypatch.setattr(ar.dc, "write_document_index",
                        lambda _work, _index: None)
    monkeypatch.setattr(ar.dc, "restore_cache_directory",
                        lambda _work, _snapshot: None)
    real_replace = os.replace
    formal_targets = []

    def spy_replace(source, target):
        if Path(target) == Path(layout.md_path):
            formal_targets.append(Path(target))
        return real_replace(source, target)

    monkeypatch.setattr(ar.os, "replace", spy_replace)
    published = ar._commit(
        layout=layout,
        final_markdown="fixed\n",
        corrections_payload={
            "stem": "Demo",
            "corrections": [{"page": 1, "status": "accepted"}],
        },
        page_records=[{"page": 1}],
    )

    assert published is True
    assert formal_targets == [Path(layout.md_path)]
    assert Path(layout.md_path).read_text(encoding="utf-8") == "fixed\n"


def test_commit_cache_failure_restores_md_corrections_and_cache(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    cache_marker = Path(layout.work_dir) / "_derived_v1" / "page_0001.json"
    cache_marker.parent.mkdir(parents=True)
    cache_marker.write_bytes(b"old-cache")
    before_md = Path(layout.md_path).read_bytes()
    before_corrections = Path(layout.corrections_path).read_bytes()
    key = ar.dc.build_cache_key(
        stem=layout.stem,
        source_pdf_sha256="a" * 64,
        dpi=150,
        ocr_page_sha256=ar.dc.sha256_text("ocr-1"),
        page_corrections=[],
        page_overlay=[],
        adoption_thresholds={},
        reconstruct_profile="reconstruct-v1",
        adoption_profile="route-b-v1",
    )
    record = ar.dc.materialize_page_cache(
        page=1,
        cache_key=key,
        adopted_decisions=[],
        fragments=[{"block_ids": [1], "md": "fixed"}],
        page_markdown="fixed\n",
    )
    final_markdown = ar.dc.assemble_document([record])

    def fail_cache_write(_work_dir, _record):
        cache_marker.write_bytes(b"partial-new-cache")
        raise RuntimeError("injected cache write failure")

    monkeypatch.setattr(ar.dc, "write_page_cache", fail_cache_write)

    with pytest.raises(RuntimeError, match="injected cache write failure"):
        ar._commit(
            layout=layout,
            final_markdown=final_markdown,
            corrections_payload={
                "stem": "Demo",
                "corrections": [{
                    "page": 1,
                    "block_id": 1,
                    "status": "accepted",
                }],
            },
            page_records=[record],
        )

    assert Path(layout.md_path).read_bytes() == before_md
    assert Path(layout.corrections_path).read_bytes() == before_corrections
    assert cache_marker.read_bytes() == b"old-cache"


def test_identical_final_state_is_idempotent_and_writes_nothing(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    calls = []
    monkeypatch.setattr(
        ar.dc, "snapshot_cache_directory",
        lambda _work: calls.append("snapshot") or {},
    )
    monkeypatch.setattr(
        ar.dc, "write_page_cache",
        lambda *_args: calls.append("cache"),
    )
    monkeypatch.setattr(
        ar.os, "replace",
        lambda *_args: calls.append("replace"),
    )

    published = ar._commit(
        layout=layout,
        final_markdown="before\n",
        corrections_payload={"stem": "Demo", "corrections": []},
        page_records=[],
    )

    assert published is False
    assert calls == ["snapshot"]


def test_formula_payload_preserves_unrelated_existing_correction():
    old = [
        {"page": 1, "block_id": 1, "corrected_latex": "old-1"},
        {"page": 9, "block_id": 3, "corrected_latex": "manual"},
    ]
    payload, merged = ar._merge_corrections(old, {
        "stem": "Demo", "schema_version": 7, "corrections": [
            {"page": 1, "block_id": 1, "corrected_latex": "new-1"},
        ],
    })

    assert payload["schema_version"] == 7
    assert merged == [
        {"page": 1, "block_id": 1, "corrected_latex": "new-1"},
        {"page": 9, "block_id": 3, "corrected_latex": "manual"},
    ]


def test_quality_accept_only_is_clean_and_preserves_crlf(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    finding = Finding.create(
        capability="unordered_blocks", kind="review", severity=Severity.P2,
        message="agent accepted source state")
    plan = PatchPlan(
        baseline_sha256=ar._sha256_text("a\r\nb\r\n"),
        proposals=(), conflicts=())

    class AcceptedBatch:
        events = ()

        @staticmethod
        def summary(sample_limit=5):
            return {"unresolved": 0}

    proposed = SimpleNamespace(
        findings=(finding,), patch_plan=plan, event_batch=AcceptedBatch())
    monkeypatch.setattr(ar, "propose_document",
                        lambda *args, **kwargs: proposed)
    monkeypatch.setattr(ar, "default_registry", lambda **kwargs: object())

    final, rounds, reason = ar._quality_candidate(
        layout=layout,
        initial="a\r\nb\r\n",
        run_dir=tmp_path / "run",
        agent_specs=[],
        discovery="off",
        learn="off",
        timeout=30,
        workers=1,
        max_rounds=2,
        gate_factory=lambda *_args: (),
    )

    assert final == "a\r\nb\r\n"
    assert rounds[0]["proposals"] == 0
    assert reason is None


def test_quality_candidate_uses_and_advances_staged_derived_index(
        tmp_path, monkeypatch):
    """Auto repair must never resolve owners against the stale formal index."""

    layout = _layout(tmp_path)
    key = ar.dc.build_cache_key(
        stem=layout.stem,
        source_pdf_sha256="a" * 64,
        dpi=150,
        ocr_page_sha256=ar.dc.sha256_text("staged-page"),
        page_corrections=[],
        page_overlay=[],
        adoption_thresholds={},
        reconstruct_profile="reconstruct-v1",
        adoption_profile="route-b-v1",
    )
    record = ar.dc.materialize_page_cache(
        page=1,
        cache_key=key,
        adopted_decisions=[],
        fragments=[{"block_ids": [1], "md": "staged formula"}],
        page_markdown="staged formula\n",
    )
    proposal = Proposal.create(
        finding_id="fix",
        kind="replace",
        md_start=7,
        md_end=14,
        before_fingerprint=ar._sha256_text("formula"),
        replacement="quality",
        producer="test",
        confidence=1.0,
    )

    class CleanBatch:
        events = ()

        @staticmethod
        def summary(sample_limit=5):
            return {"unresolved": 0}

    calls = []

    def propose(context, **_kwargs):
        assert context.derived_work_dir is not None
        index = ar.dc.read_document_index(
            context.derived_work_dir,
            expected_final_sha256=context.baseline_sha256,
        )
        assert index is not None
        calls.append(ar._read_exact(context.md_path))
        plans = (
            PatchPlan(
                baseline_sha256=context.baseline_sha256,
                proposals=(proposal,),
                conflicts=(),
            ),
            PatchPlan(
                baseline_sha256=context.baseline_sha256,
                proposals=(),
                conflicts=(),
            ),
        )
        return SimpleNamespace(
            findings=(),
            patch_plan=plans[min(len(calls) - 1, 1)],
            event_batch=CleanBatch(),
        )

    monkeypatch.setattr(ar, "propose_document", propose)
    monkeypatch.setattr(ar, "default_registry", lambda **kwargs: object())

    final, rounds, reason = ar._quality_candidate(
        layout=layout,
        initial="staged formula\n\n",
        page_records=[record],
        run_dir=tmp_path / "run",
        agent_specs=[],
        discovery="off",
        learn="off",
        timeout=30,
        workers=1,
        max_rounds=2,
        gate_factory=lambda *_args: (),
    )

    assert final == "staged quality\n\n"
    assert calls == [
        "staged formula\n\n",
        "staged quality\n\n",
        "staged quality\n\n",
    ]
    assert len(rounds) == 2
    assert reason is None


def test_source_block_correction_rebuilds_only_target_page_and_terminal_uses_stage(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)

    def page_record(page, text):
        key = ar.dc.build_cache_key(
            stem=layout.stem,
            source_pdf_sha256="a" * 64,
            dpi=150,
            ocr_page_sha256=ar.dc.sha256_text(f"ocr-{page}"),
            page_corrections=[],
            page_overlay=[],
            adoption_thresholds={},
            reconstruct_profile="reconstruct-v1",
            adoption_profile="route-b-v1",
        )
        return ar.dc.materialize_page_cache(
            page=page,
            cache_key=key,
            adopted_decisions=[],
            fragments=[{"block_ids": [page], "md": text.strip()}],
            page_markdown=text,
        )

    initial_records = [
        page_record(1, "page one\n"),
        page_record(2, "value 7\n"),
    ]
    rebuilt_records = [
        initial_records[0],
        page_record(2, "value 1\n"),
    ]
    initial_md = ar.dc.assemble_document(initial_records)
    rebuilt_md = ar.dc.assemble_document(rebuilt_records)
    correction = {
        "page": 2,
        "block_id": 7,
        "status": "accepted",
        "content_fingerprint": "raw-fingerprint",
        "corrected_latex": "value 1",
        "producer": "agent:fake:model:high",
    }

    class CleanBatch:
        events = ()

        @staticmethod
        def summary(sample_limit=5):
            return {"unresolved": 0}

    first = SimpleNamespace(
        findings=(),
        patch_plan=PatchPlan(
            baseline_sha256=ar._sha256_text(initial_md),
            proposals=(),
            conflicts=(),
        ),
        event_batch=CleanBatch(),
        block_corrections=(correction,),
    )
    terminal = SimpleNamespace(
        findings=(),
        patch_plan=PatchPlan(
            baseline_sha256=ar._sha256_text(rebuilt_md),
            proposals=(),
            conflicts=(),
        ),
        event_batch=CleanBatch(),
        block_corrections=(),
    )
    proposed = iter((first, terminal))
    contexts = []

    def propose(context, **_kwargs):
        contexts.append(context)
        assert context.source_audit_report_path is not None
        assert context.corrections_path is not None
        return next(proposed)

    monkeypatch.setattr(ar, "propose_document", propose)
    monkeypatch.setattr(ar, "default_registry", lambda **kwargs: object())
    rebuilds = []

    def rebuild(_layout, _pdf_path, _dpi, **kwargs):
        rebuilds.append(kwargs)
        return ({
            "md": rebuilt_md,
            "_page_cache_records": rebuilt_records,
        }, False)

    monkeypatch.setattr(cv, "_build_reassembled_result", rebuild)
    staged_payloads = []

    def stage_audit(*, run_dir, corrections_payload, **_kwargs):
        staged_payloads.append(json.loads(json.dumps(corrections_payload)))
        corrections = run_dir / "staged_corrections.json"
        report = run_dir / "staged_source_audit.json"
        corrections.parent.mkdir(parents=True, exist_ok=True)
        corrections.write_text(
            json.dumps(corrections_payload), encoding="utf-8")
        report.write_text(json.dumps({
            "schema_version": 6,
            "stem": layout.stem,
            "summary": {"status": "OK", "pages": 2},
            "pages": [],
        }), encoding="utf-8")
        return corrections, report

    monkeypatch.setattr(ar, "_stage_source_audit", stage_audit)
    formal_md = Path(layout.md_path).read_bytes()
    formal_corrections = Path(layout.corrections_path).read_bytes()

    result = ar._quality_candidate(
        layout=layout,
        pdf_path="source.pdf",
        dpi=150,
        corrections_payload={"stem": "Demo", "corrections": []},
        initial=initial_md,
        page_records=initial_records,
        run_dir=tmp_path / "run",
        agent_specs=[],
        discovery="off",
        learn="off",
        timeout=30,
        workers=1,
        max_rounds=1,
        gate_factory=lambda *_args: (),
    )
    final, rounds, reason = result

    assert final == rebuilt_md
    assert reason is None
    assert rounds[0]["affected_pages"] == [2]
    assert rebuilds[0]["affected_pages"] == {2}
    assert rebuilds[0]["corrections_override"] == [correction]
    assert set(rebuilds[0]["page_cache_overrides"]) == {1, 2}
    assert staged_payloads == [
        {"stem": "Demo", "corrections": []},
        {"stem": "Demo", "corrections": [correction]},
    ]
    assert result.corrections_payload["corrections"] == [correction]
    assert result.page_records == tuple(rebuilt_records)
    assert len(contexts) == 2
    assert Path(layout.md_path).read_bytes() == formal_md
    assert Path(layout.corrections_path).read_bytes() == formal_corrections


def test_quality_candidate_terminal_audits_after_last_allowed_round(
        tmp_path, monkeypatch):
    layout = _layout(tmp_path)

    class CleanBatch:
        events = ()

        @staticmethod
        def summary(sample_limit=5):
            return {"unresolved": 0}

    texts = (
        ("bad-one bad-two\n", 0, 7, "fix-one"),
        ("fix-one bad-two\n", 8, 15, "fix-two"),
    )
    proposed = []
    for number, (baseline, start, end, replacement) in enumerate(texts, 1):
        proposal = Proposal.create(
            finding_id=f"finding-{number}",
            kind="replace",
            md_start=start,
            md_end=end,
            before_fingerprint=ar._sha256_text(baseline[start:end]),
            replacement=replacement,
            producer="test",
            confidence=1.0,
        )
        proposed.append(SimpleNamespace(
            findings=(),
            patch_plan=PatchPlan(
                baseline_sha256=ar._sha256_text(baseline),
                proposals=(proposal,),
                conflicts=(),
            ),
            event_batch=CleanBatch(),
        ))
    proposed.append(SimpleNamespace(
        findings=(),
        patch_plan=PatchPlan(
            baseline_sha256=ar._sha256_text("fix-one fix-two\n"),
            proposals=(),
            conflicts=(),
        ),
        event_batch=CleanBatch(),
    ))
    calls = iter(proposed)
    monkeypatch.setattr(ar, "propose_document",
                        lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(ar, "default_registry", lambda **kwargs: object())

    final, rounds, reason = ar._quality_candidate(
        layout=layout,
        initial="bad-one bad-two\n",
        run_dir=tmp_path / "run",
        agent_specs=[],
        discovery="off",
        learn="off",
        timeout=30,
        workers=1,
        max_rounds=2,
        gate_factory=lambda *_args: (),
    )

    assert final == "fix-one fix-two\n"
    assert len(rounds) == 2
    assert reason is None


def test_quality_candidate_resolves_assets_from_deliverable_directory(tmp_path):
    layout = _layout(tmp_path)
    assets = Path(layout.assets_dir)
    assets.mkdir(parents=True)
    (assets / "page.png").write_bytes(b"png")
    initial = "alpha\n\n![](Demo.assets/page.png)\n"
    Path(layout.work_dir, "manifest.json").write_text(
        json.dumps({"fingerprint": {"page_count": 1}, "failed_pages": []}),
        encoding="utf-8",
    )
    Path(layout.work_dir, "page_0001_res.json").write_text(
        json.dumps({"page_index": 0, "page_count": 1, "parsing_res_list": [{
            "block_id": 1, "block_order": 0, "block_label": "text",
            "block_content": "alpha",
        }]}),
        encoding="utf-8",
    )

    final, rounds, reason = ar._quality_candidate(
        layout=layout,
        initial=initial,
        run_dir=tmp_path / "run",
        agent_specs=[],
        discovery="off",
        learn="off",
        timeout=30,
        workers=1,
        max_rounds=2,
    )

    assert final == initial
    assert rounds[0]["findings"] == 0
    assert reason is None


def test_new_conversion_auto_publishes_formal_markdown_only_once_and_resume_zero(
        tmp_path, monkeypatch):
    pdf_doc = fitz.open()
    pdf_doc.new_page()
    pdf_path = tmp_path / "scan.pdf"
    pdf_doc.save(str(pdf_path))
    pdf_doc.close()

    def predict(png_path, work_dir):
        page = int(Path(png_path).stem.split("_")[1])
        blocks = [{
            "block_id": 1, "block_order": 0, "block_label": "text",
            "block_content": "candidate content",
        }]
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / f"page_{page:04d}_res.json").write_text(
            json.dumps({"parsing_res_list": blocks}), encoding="utf-8")
        return blocks

    monkeypatch.setattr(cv, "predict_page", predict)

    def unified(layout, _pdf_path, **kwargs):
        baseline = kwargs["baseline_result"]
        published = ar._commit(
            layout=layout,
            final_markdown=baseline["md"],
            corrections_payload={"stem": layout.stem, "corrections": []},
            page_records=baseline["_page_cache_records"],
        )
        return {
            "status": "OK",
            "formula_repair": {"mode": "deterministic"},
            "quality_repair": {
                "mode": "auto", "status": "OK", "published": published,
            },
        }

    monkeypatch.setattr(ar, "run_unified_auto_repair", unified)
    formal_targets = []
    real_replace = os.replace
    expected = tmp_path / "out" / "scan" / "scan.md"

    def spy_replace(source, target):
        if Path(target) == expected:
            formal_targets.append(Path(target))
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", spy_replace)
    kwargs = dict(
        deliverables_dir=str(tmp_path / "out"),
        dpi=100,
        repair_auto=True,
        formula_repair="deterministic",
        quality_repair="apply",
    )
    first = cv.convert_pdf(str(pdf_path), **kwargs)
    second = cv.convert_pdf(str(pdf_path), **kwargs)

    assert first["completion_status"] == cv.CompletionStatus.OK
    assert second["completion_status"] == cv.CompletionStatus.OK
    assert formal_targets == [expected]
