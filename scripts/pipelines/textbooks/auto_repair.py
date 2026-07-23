"""V2 automatic repair: formula + quality, one final publication.

This module is intentionally the only bridge between the legacy formula
pipeline and the generic quality engine.  Both stages work against candidates;
the deliverable Markdown, accepted corrections and derived page cache are
committed together only after the terminal audit passes.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from scripts.pipelines.textbooks import derived_cache as dc
from scripts.pipelines.textbooks.corrections import load_corrections
from scripts.pipelines.textbooks.katex_scan import scan_katex
from scripts.pipelines.textbooks.paths import DocLayout
from scripts.pipelines.textbooks.selfcheck import (
    inline_math_delimiter_ws_scan, katex_incompat_scan,
)
from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec
from scripts.pipelines.textbooks.quality_repair.cli import default_registry
from scripts.pipelines.textbooks.quality_repair.engine import propose_document
from scripts.pipelines.textbooks.quality_repair.gates import (
    Gate, GateResult, build_default_gates,
)
from scripts.pipelines.textbooks.quality_repair.detectors.assets import (
    asset_issue_counts,
)
from scripts.pipelines.textbooks.quality_repair.models import (
    DetectorContext, PatchPlan, Severity,
)


@dataclass(frozen=True)
class QualityCandidateResult:
    markdown: str
    rounds: tuple[dict, ...]
    reason: str | None
    corrections_payload: dict
    page_records: tuple[dict, ...]

    def __iter__(self):
        """Keep the historical three-value unpacking contract."""
        yield self.markdown
        yield list(self.rounds)
        yield self.reason


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_exact(path: str | Path) -> str:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _apply_plan(text: str, plan: PatchPlan) -> str:
    if _sha256_text(text) != plan.baseline_sha256:
        raise RuntimeError("quality proposal baseline drift")
    result = text
    for proposal in sorted(
            plan.proposals, key=lambda item: item.md_start, reverse=True):
        before = result[proposal.md_start:proposal.md_end]
        if _sha256_text(before) != proposal.before_fingerprint:
            raise RuntimeError(
                f"quality proposal target drift: {proposal.proposal_id}")
        result = (
            result[:proposal.md_start]
            + proposal.replacement
            + result[proposal.md_end:]
        )
    return result


def _run_gates(
        before: str, after: str, plan: PatchPlan, gates: Iterable[Gate],
) -> tuple[GateResult, ...]:
    results = tuple(gate(before, after, plan) for gate in gates)
    failed = next((item for item in results if not item.passed), None)
    if failed is not None:
        raise RuntimeError(f"gate {failed.name}: {failed.detail}")
    return results


def _absolute_final_gates(
        markdown: str, *, layout: DocLayout, run_dir: Path,
) -> tuple[GateResult, ...]:
    """Require a clean terminal candidate, not merely no regression."""

    run_dir.mkdir(parents=True, exist_ok=True)
    candidate = run_dir / "final_candidate.md"
    _write_candidate(candidate, markdown)
    katex = scan_katex(
        str(candidate), str(run_dir / "final_candidate_katex.json"))
    katex_errors = None if katex is None else len(katex.get("errors") or [])
    incompatible = katex_incompat_scan(markdown)
    delimiter_count = int(
        inline_math_delimiter_ws_scan(markdown).get("count") or 0)
    assets = asset_issue_counts(markdown, Path(layout.md_path).parent)
    results = (
        GateResult(
            "final_katex", katex_errors == 0,
            ("KaTeX scanner unavailable" if katex_errors is None
             else f"KaTeX errors={katex_errors}")),
        GateResult(
            "final_katex_compat", not incompatible,
            f"incompatible commands={len(incompatible)}"),
        GateResult(
            "final_inline_delimiters", delimiter_count == 0,
            f"inline delimiter findings={delimiter_count}"),
        GateResult(
            "final_assets",
            all(int(assets.get(key) or 0) == 0
                for key in ("missing", "base64", "escape")),
            f"asset issues={assets}"),
    )
    failed = next((item for item in results if not item.passed), None)
    if failed is not None:
        raise RuntimeError(f"gate {failed.name}: {failed.detail}")
    return results


def _correction_identity(item: dict) -> tuple:
    return (
        item.get("page"), item.get("block_id"), item.get("status"),
        item.get("content_fingerprint"), item.get("corrected_latex"),
    )


def _formula_pages(before: list[dict], after: list[dict]) -> set[int]:
    old = {_correction_identity(item): item for item in before}
    new = {_correction_identity(item): item for item in after}
    changed = set(old) ^ set(new)
    pages: set[int] = set()
    for key in changed:
        item = old.get(key) or new.get(key) or {}
        page = item.get("page")
        if isinstance(page, int) and not isinstance(page, bool) and page > 0:
            pages.add(page)
    return pages


def _merge_corrections(
        existing: list[dict], payload: dict | None,
) -> tuple[dict, list[dict]]:
    """Merge this run by block target without dropping unrelated manual fixes."""

    if not isinstance(payload, dict):
        merged = list(existing)
        return {"corrections": merged}, merged
    incoming = list(payload.get("corrections") or [])
    by_target: dict[tuple[object, object], dict] = {}
    target_order: list[tuple[object, object]] = []
    for item in [*existing, *incoming]:
        target = (item.get("page"), item.get("block_id"))
        if target not in by_target:
            target_order.append(target)
        by_target[target] = dict(item)
    merged = [by_target[target] for target in target_order]
    result = dict(payload)
    result["corrections"] = merged
    return result, merged


def _write_candidate(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _stage_source_audit(
    *,
    layout: DocLayout,
    pdf_path: str,
    run_dir: Path,
    corrections_payload: dict,
    page_records: list[dict] | None = None,
) -> tuple[Path, Path]:
    """Audit raw page results against an unpublished corrections sidecar."""

    from scripts.pipelines.textbooks.source_audit import (
        ROUTE_B_V1_THRESHOLDS,
        THRESHOLD_PROFILE_V1,
        audit_document,
        write_audit_report,
    )

    stage = run_dir / "_source_audit_stage"
    corrections_path = stage / f"{layout.stem}_corrections.json"
    report_path = stage / f"{layout.stem}_source_audit.json"
    prior_report = None
    # Within one run prefer the previous staged report.  On the first round,
    # seed from the formal report so a new run does not discard all reusable
    # page audits merely because its staging directory is new.
    for candidate in (report_path, Path(layout.source_audit_path)):
        try:
            with candidate.open(encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                prior_report = loaded
                break
        except (FileNotFoundError, OSError, ValueError):
            continue

    decisions_by_page = None
    mode = None
    if (
        prior_report is not None
        and prior_report.get("adoption_source") == "recorded"
        and page_records
    ):
        from scripts.pipelines.textbooks.prose_adoption import AdoptionDecision

        parsed: dict[int, list] = {}
        try:
            for record in page_records:
                page = int(record["page"])
                parsed[page] = [
                    item if isinstance(item, AdoptionDecision)
                    else AdoptionDecision(**item)
                    for item in (record.get("adoption_decisions") or [])
                ]
        except (KeyError, TypeError, ValueError):
            # Malformed/missing derived provenance fails closed to dry-run.
            parsed = {}
        if parsed:
            decisions_by_page = parsed
            prior_mode = prior_report.get("born_digital_mode")
            mode = prior_mode if isinstance(prior_mode, str) else None

    _atomic_write_json(corrections_path, corrections_payload)
    report = audit_document(
        pdf_path,
        layout,
        ROUTE_B_V1_THRESHOLDS,
        decisions_by_page=decisions_by_page,
        born_digital_mode=mode,
        threshold_profile=THRESHOLD_PROFILE_V1,
        corrections_path=str(corrections_path),
        prior_report=prior_report,
    )
    write_audit_report(report, str(report_path))
    return corrections_path, report_path


def _quality_candidate(
    *,
    layout: DocLayout,
    initial: str,
    run_dir: Path,
    agent_specs: list[AgentSpec],
    discovery: str,
    learn: str,
    timeout: int,
    workers: int,
    max_rounds: int,
    page_records: list[dict] | None = None,
    pdf_path: str | None = None,
    dpi: int | None = None,
    corrections_payload: dict | None = None,
    gate_factory: Callable[[Path, Path], Iterable[Gate]] = build_default_gates,
) -> QualityCandidateResult:
    """Repair an isolated Markdown candidate; never writes formal resources."""

    candidate_path = run_dir / "_candidate" / Path(layout.md_path).name
    _write_candidate(candidate_path, initial)
    staged_derived_work: Path | None = None
    staged_records = list(page_records or [])
    if staged_records:
        staged_derived_work = run_dir / "_candidate_derived_work"
        for record in staged_records:
            dc.write_page_cache(staged_derived_work, record)
        dc.write_document_index(
            staged_derived_work,
            dc.build_document_index(
                staged_records, final_markdown=initial),
        )
    # Formula has already been resolved into the candidate.  Keeping it in this
    # registry would re-read the still-uncommitted corrections sidecar and route
    # the same candidates twice.
    only = [
        "assets", "final_delimiters", "page_completeness",
        "unordered_blocks",
    ]
    if discovery != "off":
        only.append("novel_discovery")
    registry = default_registry(discovery=discovery, only=only)
    rounds: list[dict] = []
    reason: str | None = None
    staged_payload = json.loads(json.dumps(
        corrections_payload
        if isinstance(corrections_payload, dict)
        else {"stem": layout.stem, "corrections": []},
        ensure_ascii=False,
    ))
    staged_payload.setdefault("stem", layout.stem)
    staged_corrections = list(staged_payload.get("corrections") or [])
    staged_corrections_path: Path | None = None
    staged_source_audit_path: Path | None = None

    def refresh_source_audit() -> None:
        nonlocal staged_corrections_path, staged_source_audit_path
        if pdf_path is None or corrections_payload is None:
            return
        staged_corrections_path, staged_source_audit_path = (
            _stage_source_audit(
                layout=layout,
                pdf_path=pdf_path,
                run_dir=run_dir,
                corrections_payload=staged_payload,
                page_records=staged_records,
            )
        )

    try:
        refresh_source_audit()
    except Exception as exc:  # noqa: BLE001 staged audit must fail closed
        reason = f"staged source audit failed: {type(exc).__name__}: {exc}"
        return QualityCandidateResult(
            initial, tuple(rounds), reason, staged_payload,
            tuple(staged_records),
        )

    for number in range(1, max_rounds + 1):
        round_dir = run_dir / f"round-{number:02d}"
        context = DetectorContext.from_paths(
            stem=layout.stem,
            md_path=candidate_path,
            work_dir=layout.work_dir,
            run_dir=round_dir,
            asset_base_dir=Path(layout.md_path).parent,
            derived_work_dir=staged_derived_work,
            corrections_path=staged_corrections_path,
            source_audit_report_path=staged_source_audit_path,
        )
        proposed = propose_document(
            context,
            registry=registry,
            agent_specs=agent_specs,
            agent_timeout=timeout,
            learn=learn,
            agent_workers=workers,
            _mode="auto-candidate",
        )
        conflicts = len(proposed.patch_plan.conflicts)
        unresolved = proposed.event_batch.summary(sample_limit=0)["unresolved"]
        block_corrections = list(
            getattr(proposed, "block_corrections", ()) or ())
        round_record = {
            "round": number,
            "before_sha256": context.baseline_sha256,
            "findings": len(proposed.findings),
            "events": len(proposed.event_batch.events),
            "unresolved_events": unresolved,
            "proposals": len(proposed.patch_plan.proposals),
            "block_corrections": len(block_corrections),
            "conflicts": conflicts,
        }
        rounds.append(round_record)
        if any(item.severity == Severity.P0 for item in proposed.findings):
            reason = "P0 finding blocks unified publication"
            break
        if conflicts:
            reason = "proposal conflict blocks unified publication"
            break
        if block_corrections:
            if (pdf_path is None or dpi is None or not staged_records):
                reason = (
                    "source-grounded correction requires PDF, DPI, "
                    "and staged page cache")
                break
            next_payload, next_corrections = _merge_corrections(
                staged_corrections,
                {"stem": layout.stem, "corrections": block_corrections},
            )
            next_payload.setdefault("stem", layout.stem)
            affected_pages = _formula_pages(
                staged_corrections, next_corrections)
            round_record["affected_pages"] = sorted(affected_pages)
            before = _read_exact(candidate_path)
            if affected_pages:
                from scripts.pipelines.textbooks.convert import (
                    _build_reassembled_result,
                )

                try:
                    built = _build_reassembled_result(
                        layout,
                        pdf_path,
                        dpi,
                        affected_pages=affected_pages,
                        corrections_override=next_corrections,
                        page_cache_overrides={
                            int(record["page"]): record
                            for record in staged_records
                        },
                    )
                except Exception as exc:  # noqa: BLE001 staged rebuild boundary
                    reason = (
                        "source-grounded page rebuild failed: "
                        f"{type(exc).__name__}: {exc}")
                    break
                if built is None:
                    reason = "source-grounded page rebuild unavailable"
                    break
                assembled, _legacy_migration = built
                after = str(assembled["md"])
                gate_plan = PatchPlan(
                    baseline_sha256=_sha256_text(before),
                    proposals=(),
                    conflicts=(),
                )
                try:
                    gate_results = _run_gates(
                        before,
                        after,
                        gate_plan,
                        gate_factory(Path(layout.md_path), round_dir),
                    )
                except RuntimeError as exc:
                    reason = str(exc)
                    round_record["gate_error"] = reason
                    break
                round_record["gates"] = [
                    asdict(item) for item in gate_results]
                _write_candidate(candidate_path, after)
                staged_records = list(
                    assembled["_page_cache_records"])
                if staged_derived_work is not None:
                    for record in staged_records:
                        dc.write_page_cache(staged_derived_work, record)
                    dc.write_document_index(
                        staged_derived_work,
                        dc.build_document_index(
                            staged_records, final_markdown=after),
                    )
            staged_payload = next_payload
            staged_corrections = next_corrections
            try:
                refresh_source_audit()
            except Exception as exc:  # noqa: BLE001 final audit must see stage
                reason = (
                    "staged source audit failed: "
                    f"{type(exc).__name__}: {exc}")
                break
            # Any Markdown proposals were located against the pre-rebuild
            # candidate. Re-detect them next round instead of applying stale
            # offsets in the same transaction.
            continue
        if not proposed.patch_plan.proposals:
            if unresolved:
                reason = "quality findings remain without a safe proposal"
            break

        before = _read_exact(candidate_path)
        after = _apply_plan(before, proposed.patch_plan)
        try:
            gate_results = _run_gates(
                before, after, proposed.patch_plan,
                gate_factory(Path(layout.md_path), round_dir),
            )
        except RuntimeError as exc:
            reason = str(exc)
            round_record["gate_error"] = reason
            break
        round_record["gates"] = [asdict(item) for item in gate_results]
        _write_candidate(candidate_path, after)
        if staged_derived_work is not None:
            staged_records = dc.reconcile_page_overlays(
                staged_records,
                current_final_markdown=after,
            )
            for record in staged_records:
                dc.write_page_cache(staged_derived_work, record)
            dc.write_document_index(
                staged_derived_work,
                dc.build_document_index(
                    staged_records, final_markdown=after),
            )

    final = _read_exact(candidate_path)
    if reason is None:
        final_dir = run_dir / "final-audit"
        final_context = DetectorContext.from_paths(
            stem=layout.stem, md_path=candidate_path,
            work_dir=layout.work_dir, run_dir=final_dir,
            asset_base_dir=Path(layout.md_path).parent,
            derived_work_dir=staged_derived_work,
            corrections_path=staged_corrections_path,
            source_audit_report_path=staged_source_audit_path)
        terminal = propose_document(
            final_context,
            registry=registry,
            agent_specs=[],
            agent_timeout=timeout,
            learn="off",
            agent_workers=workers,
            _mode="final-audit",
        )
        terminal_unresolved = terminal.event_batch.summary(
            sample_limit=0)["unresolved"]
        if (any(item.severity == Severity.P0 for item in terminal.findings)
                or terminal_unresolved or terminal.patch_plan.proposals
                or terminal.patch_plan.conflicts):
            reason = "final quality audit is not clean"
    return QualityCandidateResult(
        final,
        tuple(rounds),
        reason,
        staged_payload,
        tuple(staged_records),
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            dir=path.parent, prefix=f".{path.name}.",
            suffix=".auto-repair.tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


def _restore_file(path: Path, content: bytes | None) -> None:
    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _commit(
    *,
    layout: DocLayout,
    final_markdown: str,
    corrections_payload: dict,
    page_records: list[dict],
) -> bool:
    """Commit side resources first and formal Markdown last.

    No formal resource changes when staging or validation fails.  On success
    the deliverable path receives exactly one ``os.replace``.
    """

    md_path = Path(layout.md_path)
    corrections_path = Path(layout.corrections_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    before_md = md_path.read_bytes() if md_path.is_file() else None
    before_corrections = (
        corrections_path.read_bytes() if corrections_path.is_file() else None)
    cache_snapshot = dc.snapshot_cache_directory(layout.work_dir)
    encoded = final_markdown.encode("utf-8")
    try:
        current_payload = (
            json.loads(before_corrections.decode("utf-8"))
            if before_corrections is not None else None)
    except (UnicodeDecodeError, ValueError):
        current_payload = None
    md_changed = before_md != encoded
    corrections_changed = current_payload != corrections_payload
    if not page_records and not (md_changed or corrections_changed):
        return False
    if not page_records:
        raise ValueError("unified publication requires page cache records")
    cache_changed = False
    for record in page_records:
        current = dc.read_page_cache(
            layout.work_dir, int(record["page"]))
        if (current is None
                or current.get("record_sha256") != record.get("record_sha256")):
            cache_changed = True
            break
    desired_index = dc.build_document_index(
        page_records, final_markdown=final_markdown)
    current_index = dc.read_document_index(layout.work_dir)
    if (current_index is None
            or current_index.get("record_sha256")
            != desired_index.get("record_sha256")):
        cache_changed = True
    if not (md_changed or corrections_changed or cache_changed):
        return False

    temporary: str | None = None
    try:
        if corrections_changed:
            _atomic_write_json(corrections_path, corrections_payload)
        if cache_changed:
            for record in page_records:
                dc.write_page_cache(layout.work_dir, record)
            dc.write_document_index(layout.work_dir, desired_index)
        if md_changed:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=md_path.parent, prefix=f".{md_path.name}.",
                suffix=".auto-repair.tmp", delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, md_path)
            temporary = None
        return True
    except Exception:
        _restore_file(corrections_path, before_corrections)
        dc.restore_cache_directory(layout.work_dir, cache_snapshot)
        # Markdown is published last.  This branch normally sees it untouched;
        # retain the explicit guard for injected failures in tests.
        if (before_md is not None and md_path.is_file()
                and md_path.read_bytes() != before_md):
            _restore_file(md_path, before_md)
        elif before_md is None:
            try:
                md_path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


def run_unified_auto_repair(
    layout: DocLayout,
    pdf_path: str,
    *,
    dpi: int,
    formula_mode: str,
    formula_agent_specs,
    quality_agents: list[str],
    discovery: str,
    learn: str,
    timeout: int,
    workers: int,
    max_rounds: int,
    baseline_result: dict | None = None,
) -> dict:
    """Run auto repair without an intermediate formal Markdown publication."""

    from scripts.pipelines.textbooks.convert import (
        _build_reassembled_result, _run_formula_repair,
    )

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S.%f")
    run_dir = Path(layout.quality_repair_dir) / f"auto-{run_id}"
    if baseline_result is None:
        before_md = _read_exact(layout.md_path)
    else:
        before_md = str(baseline_result["md"])
    existing_corrections = load_corrections(layout.doc_work_dir)
    formula_layout = layout
    if baseline_result is not None:
        formula_layout = DocLayout(
            stem=layout.stem,
            deliverables_root=str(run_dir / "_formula_candidate_root"),
            work_root=layout.work_root,
        )
        _write_candidate(Path(formula_layout.md_path), before_md)

    formula = _run_formula_repair(
        formula_layout, pdf_path, formula_mode, dpi,
        agent_specs=formula_agent_specs, workers=workers,
        defer_publish=True,
    )
    agents = formula.get("agents") or {}
    payload = agents.get("corrections_payload")
    corrections_payload, proposed_corrections = _merge_corrections(
        existing_corrections, payload)
    corrections_payload.setdefault("stem", layout.stem)
    candidate_count = int(
        (formula.get("formula_candidates") or {}).get("count") or 0)
    formula_unresolved = (
        formula.get("status") == "error"
        or any(
            (formula.get(stage) or {}).get("status") == "error"
            for stage in ("katex_scan", "katex_triage", "formula_candidates")
        )
        or bool(agents.get("pending_ids"))
        or bool(agents.get("circuit_broken"))
        or bool(agents.get("rolled_back"))
        or int(agents.get("rejected") or 0) > 0
        or (formula_mode == "deterministic" and candidate_count > 0)
        or (candidate_count > 0
            and agents.get("status") == "degraded_deterministic")
    )
    if formula_unresolved:
        return {
            "mode": "auto", "status": "SUSPECT", "published": False,
            "formula_repair": formula, "quality_repair": {"mode": "auto"},
            "reason": "formula stage unresolved",
        }

    affected_pages = _formula_pages(
        existing_corrections, proposed_corrections)
    staged_records = (
        {
            int(record["page"]): record
            for record in baseline_result.get("_page_cache_records", [])
        }
        if baseline_result is not None else None
    )
    if baseline_result is not None and not affected_pages:
        built = (baseline_result, False)
    else:
        built = _build_reassembled_result(
            layout, pdf_path, dpi,
            affected_pages=affected_pages,
            corrections_override=proposed_corrections,
            page_cache_overrides=staged_records,
        )
    if built is None:
        return {
            "mode": "auto", "status": "SUSPECT", "published": False,
            "formula_repair": formula, "quality_repair": {"mode": "auto"},
            "reason": "derived candidate unavailable",
        }
    assembled, _legacy_migration = built
    quality_specs = [AgentSpec.parse(value) for value in quality_agents]
    quality_result = _quality_candidate(
        layout=layout,
        initial=assembled["md"],
        page_records=assembled["_page_cache_records"],
        pdf_path=pdf_path,
        dpi=dpi,
        corrections_payload=corrections_payload,
        run_dir=run_dir,
        agent_specs=quality_specs,
        discovery=discovery,
        learn=learn,
        timeout=timeout,
        workers=workers,
        max_rounds=max_rounds,
    )
    if isinstance(quality_result, QualityCandidateResult):
        quality_md = quality_result.markdown
        rounds = list(quality_result.rounds)
        quality_reason = quality_result.reason
        corrections_payload = quality_result.corrections_payload
        quality_records = list(quality_result.page_records)
    else:
        # Compatibility for injected test doubles and downstream wrappers that
        # still implement the historical three-value candidate contract.
        quality_md, rounds, quality_reason = quality_result
        quality_records = list(assembled["_page_cache_records"])
    if quality_reason is not None:
        return {
            "mode": "auto", "status": "SUSPECT", "published": False,
            "formula_repair": formula,
            "quality_repair": {
                "mode": "auto", "status": "SUSPECT",
                "rounds": rounds, "reason": quality_reason,
                "report_dir": str(run_dir),
            },
            "reason": quality_reason,
        }

    try:
        final_records = dc.reconcile_page_overlays(
            quality_records,
            current_final_markdown=quality_md,
        )
        # One final global gate set compares the untouched formal baseline with
        # the fully combined formula+quality candidate.
        empty_plan = PatchPlan(
            baseline_sha256=_sha256_text(before_md),
            proposals=(), conflicts=(),
        )
        final_gates = _run_gates(
            before_md, quality_md, empty_plan,
            build_default_gates(layout.md_path, run_dir / "global-gates"),
        )
        absolute_gates = _absolute_final_gates(
            quality_md, layout=layout, run_dir=run_dir / "final-gates")
        published = _commit(
            layout=layout,
            final_markdown=quality_md,
            corrections_payload=corrections_payload,
            page_records=final_records,
        )
    except Exception as exc:  # noqa: BLE001 no formal partial result escapes
        return {
            "mode": "auto", "status": "SUSPECT", "published": False,
            "formula_repair": formula,
            "quality_repair": {
                "mode": "auto", "status": "SUSPECT",
                "rounds": rounds, "reason": f"{type(exc).__name__}: {exc}",
                "report_dir": str(run_dir),
            },
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {
        "mode": "auto", "status": "OK", "published": published,
        "formula_repair": formula,
        "quality_repair": {
            "mode": "auto", "status": "OK", "rounds": rounds,
            "reason": "", "report_dir": str(run_dir),
            "findings": 0, "unresolved": [], "unresolved_events": [],
            "unresolved_count": 0, "unresolved_event_count": 0,
            "conflicts": 0, "rolled_back": False,
            "stop_reason": "resolved",
            "gates": [
                asdict(item) for item in (*final_gates, *absolute_gates)
            ],
        },
        "reason": "",
    }
