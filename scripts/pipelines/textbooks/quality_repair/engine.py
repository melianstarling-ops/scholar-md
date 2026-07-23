"""Read-only audit orchestrator."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from scripts.pipelines.textbooks import derived_cache as dc
from scripts.pipelines.textbooks.vision_repair import content_fingerprint

from .agents import AgentDecision, AgentSpec, Invoke, invoke_cli, route_evidence
from .arbiter import arbitrate
from .event_collectors import (
    collect_final_delimiter_events,
    collect_formula_events,
    collect_source_audit_events,
    collect_unordered_events,
)
from .events import DetectionBatch, RepairEvent
from .gates import Gate
from .models import (
    DetectorContext, EvidencePacket, Finding, PatchPlan, Proposal,
    RunSummary, Severity, read_text_exact, sha256_file,
)
from .repairers import deterministic_proposals
from .registry import Registry
from .reporting import write_findings, write_json, write_records, write_text
from .transaction import TransactionResult, apply_patch_plan


DEFAULT_MAX_AGENT_ITEMS = 20


@dataclass(frozen=True)
class ProposeRun:
    summary: RunSummary
    findings: tuple[Finding, ...]
    event_batch: DetectionBatch
    patch_plan: PatchPlan
    agent_decisions: tuple[AgentDecision, ...]
    block_corrections: tuple[dict, ...]


@dataclass(frozen=True)
class ApplyRun:
    proposal_run: ProposeRun
    transaction: TransactionResult
    after_findings: tuple[Finding, ...]


def _summary(
    context: DetectorContext,
    findings: list[Finding],
    mode: str,
    event_batch: DetectionBatch | None = None,
) -> RunSummary:
    by_capability = Counter(item.capability for item in findings)
    by_severity = Counter(item.severity.value for item in findings)
    after = sha256_file(context.md_path)
    if event_batch is None:
        suspect = bool(findings)
    else:
        suspect = (
            any(item.severity == Severity.P0 for item in findings)
            or event_batch.summary(sample_limit=0)["unresolved"] > 0
        )
    return RunSummary(
        stem=context.stem, mode=mode, status="SUSPECT" if suspect else "OK",
        baseline_sha256=context.baseline_sha256, after_sha256=after,
        finding_count=len(findings),
        counts_by_capability=dict(sorted(by_capability.items())),
        counts_by_severity=dict(sorted(by_severity.items())),
        report_dir=str(context.run_dir),
    )


def _collect(context: DetectorContext, registry: Registry) -> list[Finding]:
    if sha256_file(context.md_path) != context.baseline_sha256:
        raise RuntimeError("markdown changed before quality run started")
    findings = registry.detect(context)
    if sha256_file(context.md_path) != context.baseline_sha256:
        raise RuntimeError("detector modified final markdown")
    return findings


def _write_common(context: DetectorContext, registry: Registry,
                  findings: list[Finding], summary: RunSummary,
                  *, agents: Iterable[AgentSpec] = (),
                  options: dict | None = None,
                  event_batch: DetectionBatch | None = None,
                  agent_tasks: Iterable[dict] = ()) -> None:
    write_json(context.run_dir / "config.json", {
        "schema_version": context.schema_version,
        "stem": context.stem,
        "mode": summary.mode,
        "md_path": str(context.md_path),
        "work_dir": str(context.work_dir),
        "baseline_sha256": context.baseline_sha256,
        "capabilities": [cap.name for cap in registry.capabilities],
        "agents": [asdict(spec) for spec in agents],
        "options": dict(options or {}),
    })
    write_findings(context.run_dir / "findings.jsonl", findings)
    if event_batch is None:
        event_batch = DetectionBatch.create(
            stem=context.stem, baseline_sha256=context.baseline_sha256,
            events=(),
        )
    write_records(context.run_dir / "events.jsonl",
                  [event.to_dict() for event in event_batch.events])
    write_records(context.run_dir / "agent_tasks.jsonl", agent_tasks)
    summary_payload = summary.to_dict()
    summary_payload["events"] = event_batch.summary()
    write_json(context.run_dir / "summary.json", summary_payload)


_LEGACY_FINDING_ROUTES = {
    "assets": "blocker",
    "novel_discovery": "quality_agent",
    "page_completeness": "blocker",
}


def _finding_input_fingerprint(
    finding: Finding, text: str, context: DetectorContext,
) -> str:
    start = finding.target.get("md_start")
    end = finding.target.get("md_end")
    if (isinstance(start, int) and isinstance(end, int)
            and 0 <= start <= end <= len(text)):
        return hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()
    if finding.page is not None:
        page_path = context.work_dir / f"page_{finding.page:04d}_res.json"
        if page_path.is_file():
            return sha256_file(page_path)
    return context.baseline_sha256


def _event_from_legacy_finding(
    finding: Finding, text: str, context: DetectorContext,
    *, route_override: str | None = None,
) -> RepairEvent | None:
    route = route_override or _LEGACY_FINDING_ROUTES.get(finding.capability)
    if route is None and finding.capability not in {
        "final_delimiters", "formulas", "unordered_blocks",
    }:
        route = "blocker"
    if (route is None and finding.capability == "formulas"
            and finding.kind == "katex_incompatible_command"):
        route = "blocker"
    if route is None:
        return None
    bbox = finding.target.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        bbox = None
    evidence = dict(finding.evidence)
    evidence["_legacy_finding_id"] = finding.finding_id
    target = dict(finding.target)
    # Legacy aggregate findings can contain several occurrences with the same
    # empty/page-level target. Keep the stable finding identity in the event
    # target so occurrence IDs never collapse.
    target.setdefault("legacy_finding_id", finding.finding_id)
    return RepairEvent.create(
        capability=finding.capability,
        kind=finding.kind,
        severity=finding.severity,
        route=route,
        input_fingerprint=_finding_input_fingerprint(finding, text, context),
        target=target,
        page=finding.page,
        block_id=finding.target.get("block_id"),
        bbox=bbox,
        message=finding.message,
        evidence=evidence,
    )


def _fragment_owner_span(
    context: DetectorContext,
    *,
    page: int,
    block_id: int | str,
) -> tuple[int, int] | None:
    index = dc.read_document_index(
        context.derived_cache_work_dir,
        expected_final_sha256=context.baseline_sha256)
    if index is None:
        return None
    entry = next(
        (item for item in index["pages"] if item.get("page") == page), None)
    record = dc.read_page_cache(context.derived_cache_work_dir, page)
    if entry is None or record is None or entry.get("document_start") is None:
        return None

    def same_block(value: object) -> bool:
        return str(value) == str(block_id)

    fragments = [
        fragment for fragment in record["fragments"]
        if any(same_block(value) for value in fragment["block_ids"])
    ]
    spans = {
        (fragment["local_start"], fragment["local_end"])
        for fragment in fragments
    }
    if len(spans) != 1:
        return None
    local_start, local_end = spans.pop()
    if index["newline_style"] == dc.NEWLINE_CRLF:
        page_text = record["page_markdown"]
        local_start += page_text[:local_start].count("\n")
        local_end += page_text[:local_end].count("\n")
    start = entry["document_start"] + local_start
    end = entry["document_start"] + local_end
    return start, end


def _unordered_insertion_anchor(
    context: DetectorContext,
    event: RepairEvent,
) -> tuple[int, dict] | None:
    """Locate an omitted OCR block at a safe adjacent-fragment boundary.

    A truly omitted unordered block cannot own a fragment: requiring its own
    block ID would make every real omission unroutable.  We instead interpolate
    its position from same-page OCR geometry and reading order, then use the
    exact start/end boundary of one represented adjacent fragment.  Ambiguous
    geometry, non-monotonic neighbours, and document boundaries still fail
    closed.
    """

    if event.page is None or event.block_id is None:
        return None
    index = dc.read_document_index(
        context.derived_cache_work_dir,
        expected_final_sha256=context.baseline_sha256,
    )
    record = dc.read_page_cache(context.derived_cache_work_dir, event.page)
    if index is None or record is None:
        return None
    entry = next(
        (item for item in index["pages"] if item.get("page") == event.page),
        None,
    )
    if entry is None or entry.get("document_start") is None:
        return None
    source_path = context.work_dir / f"page_{event.page:04d}_res.json"
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    blocks = [
        block for block in (source.get("parsing_res_list") or [])
        if isinstance(block, dict) and block.get("block_id") is not None
    ]

    def same_block(left: object, right: object) -> bool:
        return str(left) == str(right)

    target = next(
        (block for block in blocks
         if same_block(block.get("block_id"), event.block_id)),
        None,
    )

    def valid_bbox(value: object) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) == 4
            and all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in value
            )
            and value[0] <= value[2]
            and value[1] <= value[3]
        )

    target_bbox = (
        target.get("block_bbox") if target is not None else event.bbox)
    if not valid_bbox(target_bbox):
        return None
    target_center = (float(target_bbox[1]) + float(target_bbox[3])) / 2.0

    represented: list[dict] = []
    for block in blocks:
        block_id = block.get("block_id")
        order = block.get("block_order")
        bbox = block.get("block_bbox")
        if (order is None or isinstance(order, bool)
                or not isinstance(order, (int, float))
                or not valid_bbox(bbox)):
            continue
        fragments = [
            fragment for fragment in record["fragments"]
            if any(same_block(value, block_id)
                   for value in fragment["block_ids"])
        ]
        spans = {
            (fragment["local_start"], fragment["local_end"])
            for fragment in fragments
        }
        if len(spans) != 1:
            continue
        local_start, local_end = spans.pop()
        represented.append({
            "block_id": block_id,
            "order": float(order),
            "center": (float(bbox[1]) + float(bbox[3])) / 2.0,
            "local_start": local_start,
            "local_end": local_end,
        })
    if not represented:
        return None
    predecessors = [
        item for item in represented if item["center"] <= target_center]
    successors = [
        item for item in represented if item["center"] > target_center]
    predecessor = max(
        predecessors,
        key=lambda item: (item["center"], item["order"]),
        default=None,
    )
    successor = min(
        successors,
        key=lambda item: (item["center"], item["order"]),
        default=None,
    )
    if (predecessor is not None and successor is not None
            and predecessor["order"] >= successor["order"]):
        return None

    page_text = record["page_markdown"]
    if predecessor is not None:
        local_anchor = predecessor["local_end"]
        relation = "after"
        adjacent = predecessor
    elif successor is not None:
        local_anchor = successor["local_start"]
        relation = "before"
        adjacent = successor
    else:
        return None
    # Page-boundary insertions cannot be assigned unambiguously by the cache
    # overlay reconciler.  Preserve the fail-closed contract.
    if not 0 < local_anchor < len(page_text):
        return None
    if index["newline_style"] == dc.NEWLINE_CRLF:
        local_anchor += page_text[:local_anchor].count("\n")
    anchor = int(entry["document_start"]) + local_anchor
    document_end = entry.get("document_end")
    if (not isinstance(document_end, int)
            or not int(entry["document_start"]) < anchor < document_end):
        return None
    return anchor, {
        "md_owner": "derived_insertion_anchor",
        "anchor_block_id": adjacent["block_id"],
        "anchor_relation": relation,
        "anchor_block_order": adjacent["order"],
    }


def _attach_agent_md_owners(
    context: DetectorContext,
    events: list[RepairEvent],
    text: str,
    metrics: Counter[str],
) -> list[RepairEvent]:
    resolved: list[RepairEvent] = []
    for event in events:
        if (event.capability not in {
                "unordered_blocks", "formulas", "source_audit"}
                or not _is_agent_route(event.route)
                or ("md_start" in event.target and "md_end" in event.target)):
            resolved.append(event)
            continue
        span = (
            _fragment_owner_span(
                context, page=event.page, block_id=event.block_id)
            if event.page is not None and event.block_id is not None
            else None
        )
        evidence = dict(event.evidence)
        anchor_evidence: dict | None = None
        if span is None and event.capability == "unordered_blocks":
            anchored = _unordered_insertion_anchor(context, event)
            if anchored is not None:
                anchor, anchor_evidence = anchored
                span = (anchor, anchor)
        if span is None:
            evidence["md_owner"] = "unresolved"
            evidence["md_owner_reason"] = (
                "derived index/fragments do not identify exactly one Markdown span")
            metrics[f"{event.capability}:unlocatable_md_owner"] += 1
            resolved.append(replace(
                event,
                route="blocker:unlocatable_md_owner",
                evidence=evidence,
            ))
            continue
        start, end = span
        if not 0 <= start <= end <= len(text):
            evidence["md_owner"] = "unresolved"
            evidence["md_owner_reason"] = "derived fragment span is outside final Markdown"
            metrics[f"{event.capability}:invalid_md_owner"] += 1
            resolved.append(replace(
                event,
                route="blocker:unlocatable_md_owner",
                evidence=evidence,
            ))
            continue
        target = dict(event.target)
        target.update({"md_start": start, "md_end": end})
        if anchor_evidence is None:
            evidence["md_owner"] = "derived_fragment"
        else:
            evidence.update(anchor_evidence)
        metrics[f"{event.capability}:located_md_owner"] += 1
        # Keep the collector's block-scoped event_id. The executable MD span
        # can change length after repair and must not invalidate terminal join.
        resolved.append(replace(event, target=target, evidence=evidence))
    return resolved


def _collect_event_batch(
    context: DetectorContext, registry: Registry,
    findings: list[Finding], text: str,
) -> DetectionBatch:
    active = {capability.name for capability in registry.capabilities}
    events: list[RepairEvent] = []
    metrics: Counter[str] = Counter()
    source_events, source_metrics = collect_source_audit_events(context)
    events.extend(source_events)
    metrics.update(source_metrics)
    collectors = {
        "unordered_blocks": collect_unordered_events,
        "formulas": collect_formula_events,
        "final_delimiters": collect_final_delimiter_events,
    }
    for capability, collector in collectors.items():
        if capability not in active:
            continue
        emitted, collected_metrics = collector(context)
        events.extend(emitted)
        metrics.update(collected_metrics)
    event_capabilities = Counter(event.capability for event in events)
    for finding in findings:
        event = _event_from_legacy_finding(finding, text, context)
        if event is None and finding.capability in collectors:
            has_detail = event_capabilities[finding.capability] > 0
            terminal_formula = metrics.get("formula:terminal_candidates", 0) > 0
            needs_fallback = (
                not has_detail
                and not (
                    finding.capability == "formulas"
                    and finding.kind == "formula_suspicion"
                    and terminal_formula
                )
            )
            if needs_fallback:
                event = _event_from_legacy_finding(
                    finding, text, context, route_override="blocker")
        if event is not None:
            events.append(event)
    events = _attach_agent_md_owners(context, events, text, metrics)
    return DetectionBatch.create(
        stem=context.stem, baseline_sha256=context.baseline_sha256,
        events=events, metrics=metrics,
    )


def audit_document(context: DetectorContext, *, registry: Registry) -> RunSummary:
    """Run every detector against one immutable baseline and write audit reports."""
    findings = _collect(context, registry)
    text = read_text_exact(context.md_path)
    event_batch = _collect_event_batch(context, registry, findings, text)
    summary = _summary(context, findings, "audit", event_batch)
    _write_common(context, registry, findings, summary, event_batch=event_batch)
    return summary


def _packet(finding: Finding, text: str,
            context: DetectorContext,
            page_image_cache: "_PageImageCache | None" = None) -> EvidencePacket:
    start = finding.target.get("md_start")
    end = finding.target.get("md_end")
    if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text):
        excerpt = text[start:end]
    else:
        excerpt = str(finding.evidence.get("sample") or
                      finding.evidence.get("content_sample") or "")[:1000]
    paths = finding.evidence.get("image_paths") or ()
    if isinstance(paths, str):
        paths = (paths,)
    image_paths = [str(path) for path in paths]
    if finding.page is not None and not image_paths:
        if page_image_cache is not None:
            rendered = page_image_cache.get_or_render(context, finding.page)
        else:
            try:
                rendered = _render_page_evidence(context, finding.page)
            except Exception:                       # noqa: BLE001 evidence failure -> uncertain
                rendered = None
        if rendered:
            image_paths.append(rendered)
    return EvidencePacket(
        finding_id=finding.finding_id, issue_kind=finding.kind,
        severity=finding.severity.value, md_excerpt=excerpt,
        source_evidence=(finding.message,
                         json.dumps(finding.evidence, ensure_ascii=False, sort_keys=True)[:2000]),
        target=dict(finding.target),
        image_paths=tuple(image_paths),
    )


def _render_page_evidence(context: DetectorContext, page: int) -> str | None:
    """Render one source page; caller owns cache and failure policy."""

    manifest_path = context.work_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pdf_path = manifest.get("pdf_path")
    if not pdf_path or not os.path.isfile(pdf_path):
        return None
    from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
    evidence_dir = context.run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return str(pdf_page_to_png(
        pdf_path, page, str(evidence_dir), dpi=150))


class _PageImageCache:
    """Thread-safe one-render-per-page evidence cache."""

    def __init__(self) -> None:
        self._values: dict[int, str | None] = {}
        self._lock = threading.Lock()

    def get_or_render(self, context: DetectorContext, page: int) -> str | None:
        with self._lock:
            if page in self._values:
                return self._values[page]
            try:
                rendered = _render_page_evidence(context, page)
            except Exception:                       # noqa: BLE001 cache failures as unavailable
                rendered = None
            self._values[page] = rendered
            return rendered


def _agent_proposal(decision: AgentDecision, finding: Finding,
                    text: str) -> Proposal | None:
    if decision.verdict != "repair":
        return None
    start = decision.target.get("md_start")
    end = decision.target.get("md_end")
    owner_start = finding.target.get("md_start")
    owner_end = finding.target.get("md_end")
    if (not isinstance(start, int) or not isinstance(end, int)
            or not isinstance(owner_start, int) or not isinstance(owner_end, int)
            or not 0 <= owner_start <= start <= end <= owner_end <= len(text)):
        return None
    before = text[start:end]
    fingerprint = hashlib.sha256(before.encode("utf-8")).hexdigest()
    return Proposal.create(
        finding_id=finding.finding_id, kind=f"agent:{decision.issue_family}",
        md_start=start, md_end=end, before_fingerprint=fingerprint,
        replacement=decision.replacement,
        producer=f"agent:{decision.provider}:{decision.model}:{decision.effort}",
        confidence=decision.confidence,
    )


def _source_block_correction(
    decision: AgentDecision,
    finding: Finding,
    context: DetectorContext,
) -> dict | None:
    """Bind a source-grounded repair to the immutable raw OCR block."""

    if (decision.verdict != "repair"
            or finding.capability != "source_audit"
            or not finding.evidence.get("source_grounded")):
        return None
    page = finding.page
    block_id = finding.target.get("block_id")
    decision_page = decision.target.get("page")
    decision_block_id = decision.target.get("block_id")
    if (not isinstance(page, int) or isinstance(page, bool) or page <= 0
            or decision_page != page
            or block_id is None
            or str(decision_block_id) != str(block_id)):
        return None
    try:
        payload = json.loads(
            (context.work_dir / f"page_{page:04d}_res.json").read_text(
                encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw_block = next(
        (
            item for item in (payload.get("parsing_res_list") or [])
            if isinstance(item, dict)
            and str(item.get("block_id")) == str(block_id)
        ),
        None,
    )
    if raw_block is None:
        return None
    raw_content = raw_block.get("block_content")
    if not isinstance(raw_content, str):
        return None
    return {
        "page": page,
        "block_id": raw_block.get("block_id"),
        "status": "accepted",
        "content_fingerprint": content_fingerprint(raw_content),
        "corrected_latex": decision.replacement,
        "producer": (
            f"agent:{decision.provider}:{decision.model}:{decision.effort}"),
    }


def _finding_from_event(event: RepairEvent) -> Finding:
    evidence = dict(event.evidence)
    legacy_finding_id = evidence.pop("_legacy_finding_id", None)
    crop_paths = evidence.get("crop_paths")
    if crop_paths and not evidence.get("image_paths"):
        evidence["image_paths"] = crop_paths
    return Finding(
        finding_id=(legacy_finding_id
                    if isinstance(legacy_finding_id, str) else event.event_id),
        capability=event.capability,
        kind=event.kind,
        severity=event.severity,
        message=event.message,
        page=event.page,
        target=dict(event.target),
        evidence=evidence,
    )


def _is_agent_route(route: str) -> bool:
    handler = route.split(":", 1)[0]
    return handler in {"quality_agent", "formula_agent"}


_TERMINAL_EVENT_STATUSES = frozenset({"accepted", "applied", "ignored"})


def _prior_terminal_tasks(context: DetectorContext) -> dict[tuple[str, str], dict]:
    """Load terminal event records from earlier runs; later paths/lines win."""

    terminal: dict[tuple[str, str], dict] = {}
    root = context.run_dir.parent
    for path in sorted(root.glob("*/agent_tasks.jsonl"), key=lambda item: str(item)):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            event_id = record.get("event_id")
            fingerprint = record.get("input_fingerprint")
            if (record.get("status") in _TERMINAL_EVENT_STATUSES
                    and isinstance(event_id, str)
                    and isinstance(fingerprint, str)):
                record = dict(record)
                record["_ledger_path"] = str(path)
                terminal[(event_id, fingerprint)] = record
    return terminal


def _proposal_from_task(record: dict) -> Proposal | None:
    payload = record.get("proposal")
    if not isinstance(payload, dict):
        return None
    try:
        return Proposal(**payload)
    except (TypeError, ValueError):
        return None


def _block_correction_from_task(record: dict) -> dict | None:
    payload = record.get("block_correction")
    if not isinstance(payload, dict):
        return None
    required = {
        "page", "block_id", "status", "content_fingerprint",
        "corrected_latex", "producer",
    }
    if not required.issubset(payload):
        return None
    return dict(payload)


def _route_event_batch(
    context: DetectorContext,
    batch: DetectionBatch,
    text: str,
    *,
    agent_specs: list[AgentSpec],
    invoke: Invoke,
    agent_timeout: int,
    max_agent_items: int,
    agent_workers: int,
    learn: str,
) -> tuple[
    DetectionBatch, list[AgentDecision], list[Proposal], list[dict],
    list[dict], list[dict],
]:
    """Route only explicitly Agent-owned events and persist every unresolved state."""

    if agent_workers <= 0:
        raise ValueError("agent_workers must be positive")
    by_id = {event.event_id: event for event in batch.events}
    routable = sorted(
        (event for event in batch.events
         if event.status == "unresolved" and _is_agent_route(event.route)),
        key=lambda event: (
            {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}[event.severity],
            event.page if event.page is not None else 0,
            str(event.block_id),
            event.event_id,
        ),
    )
    terminal = _prior_terminal_tasks(context)
    outcomes: dict[
        str,
        tuple[
            str, AgentDecision | None, Proposal | None, dict | None, dict,
        ],
    ] = {}
    fresh: list[RepairEvent] = []

    for event in routable:
        prior = terminal.get((event.event_id, event.input_fingerprint))
        if prior is None:
            fresh.append(event)
            continue
        status = str(prior["status"])
        proposal = _proposal_from_task(prior)
        block_correction = _block_correction_from_task(prior)
        task = {
            key: value for key, value in prior.items()
            if key != "_ledger_path"
        }
        task.update({
            "reused_terminal": True,
            "source_ledger": prior["_ledger_path"],
        })
        outcomes[event.event_id] = (
            status, None, proposal, block_correction, task)

    eligible: list[RepairEvent] = []
    for index, event in enumerate(fresh):
        if not agent_specs:
            status = "unresolved_no_agent"
        elif index >= max_agent_items:
            status = "not_routed_due_budget"
        else:
            eligible.append(event)
            continue
        outcomes[event.event_id] = (
            status,
            None,
            None,
            None,
            {
                "schema_version": 1,
                "event_id": event.event_id,
                "input_fingerprint": event.input_fingerprint,
                "route": event.route,
                "status": status,
                "page": event.page,
                "block_id": event.block_id,
                "reused_terminal": False,
            },
        )

    page_image_cache = _PageImageCache()
    packets: dict[str, tuple[Finding, EvidencePacket]] = {}
    for event in eligible:
        finding = _finding_from_event(event)
        packets[event.event_id] = (
            finding,
            _packet(
                finding, text, context,
                page_image_cache=page_image_cache,
            ),
        )

    def invoke_one(
        event: RepairEvent,
    ) -> tuple[
        str, AgentDecision | None, Proposal | None, dict | None, dict,
    ]:
        finding, packet = packets[event.event_id]
        try:
            decision = route_evidence(
                packet, agent_specs, invoke=invoke, timeout=agent_timeout)
        except Exception:  # noqa: BLE001 provider/runtime failure remains unresolved
            decision = None
        proposal: Proposal | None = None
        block_correction: dict | None = None
        if decision is None:
            status = "provider_unavailable"
        else:
            if learn == "package" and (
                decision.verdict == "novel"
                or (decision.verdict == "repair" and decision.generalizable)
            ):
                _write_learning_package(context, finding, packet, decision)
            if finding.capability == "source_audit":
                block_correction = _source_block_correction(
                    decision, finding, context)
            else:
                proposal = _agent_proposal(decision, finding, text)
            if decision.verdict == "accept":
                status = "ignored"
            elif (decision.verdict == "repair"
                  and (proposal is not None or block_correction is not None)):
                status = "accepted"
            elif decision.verdict == "repair":
                status = "repair_unapplied"
            elif decision.verdict == "novel":
                status = "novel_confirmed"
            else:
                status = "uncertain"
        task = {
            "schema_version": 1,
            "event_id": event.event_id,
            "input_fingerprint": event.input_fingerprint,
            "route": event.route,
            "status": status,
            "page": event.page,
            "block_id": event.block_id,
            "reused_terminal": False,
        }
        if decision is not None:
            task.update({
                "verdict": decision.verdict,
                "provider": decision.provider,
                "model": decision.model,
                "effort": decision.effort,
            })
        if proposal is not None:
            task["proposal"] = proposal.to_dict()
        if block_correction is not None:
            task["block_correction"] = block_correction
        return status, decision, proposal, block_correction, task

    if eligible:
        with ThreadPoolExecutor(
            max_workers=min(agent_workers, len(eligible))
        ) as pool:
            futures = [pool.submit(invoke_one, event) for event in eligible]
            # Consume in canonical event order, never completion order.
            for event, future in zip(eligible, futures):
                outcomes[event.event_id] = future.result()

    decisions: list[AgentDecision] = []
    proposals: list[Proposal] = []
    block_corrections: list[dict] = []
    tasks: list[dict] = []
    ledger_records: list[dict] = []
    for event in routable:
        status, decision, proposal, block_correction, task = outcomes[event.event_id]
        by_id[event.event_id] = replace(event, status=status)
        if decision is not None:
            decisions.append(decision)
            ledger_records.append({
                "schema_version": 1,
                "event_id": event.event_id,
                "input_fingerprint": event.input_fingerprint,
                **asdict(decision),
            })
        if proposal is not None:
            proposals.append(proposal)
        if block_correction is not None:
            block_corrections.append(block_correction)
        tasks.append(task)
    updated = DetectionBatch.create(
        stem=batch.stem,
        baseline_sha256=batch.baseline_sha256,
        events=by_id.values(),
        metrics=batch.metrics,
    )
    return (
        updated, decisions, proposals, block_corrections, tasks,
        ledger_records,
    )


def _write_learning_package(context: DetectorContext, finding: Finding,
                            packet: EvidencePacket,
                            decision: AgentDecision) -> None:
    package = context.run_dir / "learning_packages" / finding.finding_id
    copied_images: list[str] = []
    for index, raw in enumerate(packet.image_paths, 1):
        source = Path(raw)
        if not source.is_file():
            continue
        suffix = source.suffix.lower() if source.suffix else ".png"
        target = package / "evidence" / f"source_crop_{index:02d}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_images.append(str(target.resolve()))
    write_json(package / "finding.json", finding.to_dict())
    write_json(package / "evidence_manifest.json", {
        "schema_version": 1,
        "finding_id": finding.finding_id,
        "agent_decision": asdict(decision),
        "image_paths": copied_images,
        "privacy": "private work-root package; do not copy source images to public tests",
    })
    write_text(package / "current_md.txt", packet.md_excerpt)
    write_text(package / "expected_behavior.md",
               f"# Expected behavior\n\nIssue family: `{decision.issue_family}`\n\n"
               f"Agent verdict: `{decision.verdict}`. Before implementation, verify this "
               "expectation against the source evidence.\n")
    write_text(package / "fixture_plan.md",
               "# Fixture plan\n\nCreate the smallest private real fixture that reproduces the issue. "
               "Create a synthetic or redacted public fixture for regression tests.\n")
    write_text(package / "test_plan.md",
               "# Test plan\n\n1. Write a failing detector test.\n2. If repairable, add drift, "
               "conflict, rollback, and idempotency tests.\n3. Run the full textbooks suite.\n")
    write_text(package / "lesson_draft.md",
               f"# Lesson draft\n\n- Issue family: `{decision.issue_family}`\n"
               "- Trigger: [待确认]\n- Safe repair boundary: [待确认]\n"
               "- Counterexamples: [待确认]\n")
    write_text(package / "development_brief.md",
               f"# Development brief\n\nPromote `{decision.issue_family}` only after source "
               "verification. Add detector, optional repairer, private fixture, public test, "
               "and lesson; do not auto-commit or merge.\n")


def propose_document(context: DetectorContext, *, registry: Registry,
                     agent_specs: list[AgentSpec], invoke: Invoke = invoke_cli,
                     agent_timeout: int = 300,
                     learn: str = "off", max_agent_items: int = DEFAULT_MAX_AGENT_ITEMS,
                     agent_workers: int = 4,
                     _mode: str = "propose") -> ProposeRun:
    findings = _collect(context, registry)
    text = read_text_exact(context.md_path)
    proposals = deterministic_proposals(text, findings)
    event_batch = _collect_event_batch(context, registry, findings, text)
    (event_batch, decisions, agent_proposals, block_corrections,
     agent_tasks, agent_ledger) = _route_event_batch(
        context,
        event_batch,
        text,
        agent_specs=agent_specs,
        invoke=invoke,
        agent_timeout=agent_timeout,
        max_agent_items=max_agent_items,
        agent_workers=agent_workers,
        learn=learn,
    )
    proposals.extend(agent_proposals)
    plan = arbitrate(text, proposals, baseline_sha256=context.baseline_sha256)
    summary = _summary(context, findings, _mode, event_batch)
    _write_common(context, registry, findings, summary, agents=agent_specs,
                  options={"learn": learn, "agent_timeout": agent_timeout,
                           "max_agent_items": max_agent_items,
                           "agent_workers": agent_workers},
                  event_batch=event_batch, agent_tasks=agent_tasks)
    write_records(context.run_dir / "proposals.jsonl",
                  [proposal.to_dict() for proposal in proposals])
    write_records(
        context.run_dir / "block_corrections.jsonl",
        block_corrections,
    )
    write_records(context.run_dir / "agent_ledger.jsonl",
                  agent_ledger)
    write_json(context.run_dir / "patch_plan.json", plan.to_dict())
    if sha256_file(context.md_path) != context.baseline_sha256:
        raise RuntimeError("propose mode modified final markdown")
    return ProposeRun(
        summary, tuple(findings), event_batch, plan, tuple(decisions),
        tuple(block_corrections))


def _load_indexed_page_records(context: DetectorContext) -> list[dict]:
    index = dc.read_document_index(
        context.work_dir, expected_final_sha256=context.baseline_sha256)
    if index is None:
        raise RuntimeError(
            "derived index invalid or does not match pre-repair Markdown")
    records: list[dict] = []
    for entry in index["pages"]:
        page = entry["page"]
        record = dc.read_page_cache(context.work_dir, page)
        if record is None:
            raise RuntimeError(f"derived page cache invalid or missing: page {page}")
        if (record["cache_key"]["digest"] != entry["cache_key_digest"]
                or record["page_md_sha256"] != entry["page_md_sha256"]):
            raise RuntimeError(f"derived page/index mismatch: page {page}")
        records.append(record)
    if dc.sha256_text(dc.assemble_document(
            records, newline_style=index["newline_style"])) != context.baseline_sha256:
        raise RuntimeError("derived page cache does not reproduce pre-repair Markdown")
    return records


def _restore_markdown_from_backup(md_path: Path, backup_path: Path) -> None:
    if not backup_path.is_file():
        raise RuntimeError(f"quality repair backup missing: {backup_path}")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=md_path.parent,
            prefix=f".{md_path.name}.",
            suffix=".quality-repair-rollback.tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            with backup_path.open("rb") as source:
                shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, md_path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


def _reconcile_derived_cache_after_apply(
    context: DetectorContext,
    transaction: TransactionResult,
) -> TransactionResult:
    """Commit page overlays with the MD, or roll both resources back."""

    if (transaction.applied <= 0 or transaction.rolled_back
            or transaction.reason
            or not dc.document_index_path(context.work_dir).is_file()):
        return transaction
    backup_path = context.run_dir / (
        f"{context.md_path.name}.pre_quality_repair.bak")
    try:
        cache_snapshot = dc.snapshot_cache_directory(context.work_dir)
    except Exception as exc:  # noqa: BLE001 restore MD before returning failure
        try:
            _restore_markdown_from_backup(context.md_path, backup_path)
            rollback_detail = ""
        except Exception as rollback_exc:  # noqa: BLE001 report incomplete rollback
            rollback_detail = f"; markdown rollback failed: {rollback_exc}"
        return TransactionResult(
            0,
            True,
            (f"derived cache snapshot failed: {type(exc).__name__}: {exc}"
             f"{rollback_detail}"),
            transaction.gate_results,
        )
    try:
        page_records = _load_indexed_page_records(context)
        final_markdown = read_text_exact(context.md_path)
        reconciled = dc.reconcile_page_overlays(
            page_records, current_final_markdown=final_markdown)
        for record in reconciled:
            dc.write_page_cache(context.work_dir, record)
        dc.write_document_index(
            context.work_dir,
            dc.build_document_index(
                reconciled, final_markdown=final_markdown),
        )
    except Exception as exc:  # noqa: BLE001 transaction boundary
        rollback_errors: list[str] = []
        try:
            _restore_markdown_from_backup(context.md_path, backup_path)
        except Exception as rollback_exc:  # noqa: BLE001 preserve cache rollback
            rollback_errors.append(f"markdown rollback failed: {rollback_exc}")
        try:
            dc.restore_cache_directory(context.work_dir, cache_snapshot)
        except Exception as rollback_exc:  # noqa: BLE001 report incomplete rollback
            rollback_errors.append(f"cache rollback failed: {rollback_exc}")
        detail = f"derived cache reconcile failed: {type(exc).__name__}: {exc}"
        if rollback_errors:
            detail += "; " + "; ".join(rollback_errors)
        return TransactionResult(
            0, True, detail, transaction.gate_results)
    return transaction


def _join_terminal_event_state(
    current: DetectionBatch,
    prior: DetectionBatch,
) -> DetectionBatch:
    terminal = {
        (event.event_id, event.input_fingerprint): event.status
        for event in prior.events
        if event.status in _TERMINAL_EVENT_STATUSES
    }
    events = [
        replace(
            event,
            status=terminal.get(
                (event.event_id, event.input_fingerprint), event.status),
        )
        for event in current.events
    ]
    return DetectionBatch.create(
        stem=current.stem,
        baseline_sha256=current.baseline_sha256,
        events=events,
        metrics=current.metrics,
    )


def apply_document(context: DetectorContext, *, registry: Registry,
                   agent_specs: list[AgentSpec], gates: Iterable[Gate],
                   invoke: Invoke = invoke_cli,
                   agent_timeout: int = 300,
                   learn: str = "off",
                   max_agent_items: int = DEFAULT_MAX_AGENT_ITEMS,
                   agent_workers: int = 4) -> ApplyRun:
    proposed = propose_document(
        context, registry=registry, agent_specs=agent_specs, invoke=invoke,
        agent_timeout=agent_timeout, learn=learn,
        max_agent_items=max_agent_items, agent_workers=agent_workers,
        _mode="apply")
    if any(item.severity == Severity.P0 for item in proposed.findings):
        transaction = TransactionResult(0, False, "P0 finding blocks apply", ())
    elif proposed.patch_plan.conflicts:
        transaction = TransactionResult(0, False, "proposal conflict blocks apply", ())
    else:
        transaction = apply_patch_plan(
            context.md_path, proposed.patch_plan, gates=gates,
            snapshot_dir=context.run_dir,
        )
    transaction = _reconcile_derived_cache_after_apply(context, transaction)
    after_context = DetectorContext.from_paths(
        stem=context.stem, md_path=context.md_path, work_dir=context.work_dir,
        run_dir=context.run_dir)
    after_findings_list = registry.detect(after_context)
    after_findings = tuple(after_findings_list)
    after_text = read_text_exact(after_context.md_path)
    after_event_batch = _collect_event_batch(
        after_context, registry, after_findings_list, after_text)
    after_event_batch = _join_terminal_event_state(
        after_event_batch, proposed.event_batch)
    write_findings(context.run_dir / "after_findings.jsonl", after_findings)
    write_records(context.run_dir / "after_events.jsonl",
                  [event.to_dict() for event in after_event_batch.events])
    write_json(context.run_dir / "validation.json", {
        "schema_version": 1,
        "before_sha256": context.baseline_sha256,
        "after_sha256": after_context.baseline_sha256,
        "before_findings": len(proposed.findings),
        "after_findings": len(after_findings),
        "before_events": len(proposed.event_batch.events),
        "after_events": len(after_event_batch.events),
        "after_counts_by_severity": dict(sorted(Counter(
            item.severity.value for item in after_findings).items())),
        "applied": transaction.applied,
        "rolled_back": transaction.rolled_back,
        "reason": transaction.reason,
    })
    write_json(context.run_dir / "transaction.json", asdict(transaction))
    terminal_summary = _summary(
        after_context, after_findings_list, "apply", after_event_batch)
    transaction_ok = (
        not transaction.rolled_back
        and all(result.passed for result in transaction.gate_results)
        and transaction.reason in {"", "empty patch plan"}
    )
    summary = proposed.summary.to_dict()
    summary["mode"] = "apply"
    summary["after_sha256"] = sha256_file(context.md_path)
    summary["status"] = (
        "OK"
        if terminal_summary.status == "OK" and transaction_ok
        else "SUSPECT"
    )
    summary["applied"] = transaction.applied
    summary["rolled_back"] = transaction.rolled_back
    summary["events"] = proposed.event_batch.summary()
    summary["after_events"] = after_event_batch.summary()
    summary["terminal"] = {
        "status": terminal_summary.status,
        "finding_count": len(after_findings),
        "p0_findings": sum(
            item.severity == Severity.P0 for item in after_findings),
        "unresolved_events": after_event_batch.summary(
            sample_limit=0)["unresolved"],
        "transaction_ok": transaction_ok,
        "transaction_reason": transaction.reason,
    }
    write_json(context.run_dir / "summary.json", summary)
    return ApplyRun(proposed, transaction, after_findings)


def _unresolved_event_records(run_dir: Path) -> list[dict]:
    path = run_dir / "after_events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (isinstance(record, dict)
                and record.get("status") not in _TERMINAL_EVENT_STATUSES):
            records.append(record)
    return records


def auto_apply(
    context: DetectorContext,
    *,
    registry: Registry,
    agent_specs: list[AgentSpec],
    gate_factory: Callable[[DetectorContext], Iterable[Gate]],
    invoke: Invoke = invoke_cli,
    agent_timeout: int = 300,
    learn: str = "off",
    max_agent_items: int = DEFAULT_MAX_AGENT_ITEMS,
    agent_workers: int = 4,
    max_rounds: int = 1,
) -> dict:
    """Apply repeatedly until resolved, rolled back, stalled, or capped."""

    if agent_workers <= 0 or max_rounds <= 0:
        raise ValueError("agent_workers and max_rounds must be positive")
    rounds: list[dict] = []
    seen_states: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    total_applied = 0
    total_conflicts = 0
    any_rollback = False
    final_applied: ApplyRun | None = None
    final_unresolved_events: list[dict] = []
    final_events_reported = False
    stop_reason = "max_rounds"

    for round_number in range(1, max_rounds + 1):
        round_dir = (
            context.run_dir
            if max_rounds == 1
            else context.run_dir / f"round-{round_number:02d}"
        )
        round_context = DetectorContext.from_paths(
            stem=context.stem,
            md_path=context.md_path,
            work_dir=context.work_dir,
            run_dir=round_dir,
        )
        applied = apply_document(
            round_context,
            registry=registry,
            agent_specs=agent_specs,
            gates=gate_factory(round_context),
            invoke=invoke,
            agent_timeout=agent_timeout,
            learn=learn,
            max_agent_items=max_agent_items,
            agent_workers=agent_workers,
        )
        final_applied = applied
        transaction = applied.transaction
        conflicts = len(applied.proposal_run.patch_plan.conflicts)
        total_applied += transaction.applied
        total_conflicts += conflicts
        any_rollback = any_rollback or transaction.rolled_back
        final_unresolved_events = _unresolved_event_records(round_dir)
        final_events_reported = (round_dir / "after_events.jsonl").is_file()
        after_ids = tuple(sorted(
            finding.finding_id for finding in applied.after_findings))
        after_event_ids = tuple(sorted(
            str(event.get("event_id"))
            for event in final_unresolved_events
        ))
        after_sha256 = sha256_file(context.md_path)
        state = (after_sha256, after_ids, after_event_ids)
        before_ids = tuple(sorted(
            finding.finding_id
            for finding in applied.proposal_run.findings
        ))
        before_event_ids = tuple(sorted(
            event.event_id
            for event in applied.proposal_run.event_batch.events
            if event.status not in _TERMINAL_EVENT_STATUSES
        ))
        before_state = (
            round_context.baseline_sha256, before_ids, before_event_ids)
        rounds.append({
            "round": round_number,
            "before_findings": applied.proposal_run.summary.finding_count,
            "after_findings": len(applied.after_findings),
            "after_unresolved_events": len(final_unresolved_events),
            "applied": transaction.applied,
            "conflicts": conflicts,
            "rolled_back": transaction.rolled_back,
            "reason": transaction.reason,
            "after_sha256": after_sha256,
        })
        after_p0 = any(
            finding.severity == Severity.P0
            for finding in applied.after_findings)
        findings_unresolved_without_event_report = (
            bool(applied.after_findings) and not final_events_reported)
        if (not final_unresolved_events
                and not after_p0
                and not findings_unresolved_without_event_report):
            stop_reason = "resolved"
            break
        if transaction.rolled_back:
            stop_reason = "rolled_back"
            break
        if (transaction.applied == 0
                or state == before_state
                or state in seen_states):
            stop_reason = "no_progress"
            break
        seen_states.add(state)

    assert final_applied is not None
    after_counts = Counter(
        finding.severity.value for finding in final_applied.after_findings)
    unresolved = [
        {
            "finding_id": finding.finding_id,
            "kind": finding.kind,
            "severity": finding.severity.value,
            "page": finding.page,
            "message": finding.message,
        }
        for finding in final_applied.after_findings
    ]
    final_transaction = final_applied.transaction
    after_p0 = any(
        finding.severity == Severity.P0
        for finding in final_applied.after_findings)
    blocking_reason = final_transaction.reason not in {
        "", "empty patch plan"}
    status = (
        "OK"
        if (not final_unresolved_events
            and not after_p0
            and not (unresolved and not final_events_reported)
            and not any_rollback
            and not blocking_reason
            and total_conflicts == 0)
        else "SUSPECT"
    )
    return {
        "mode": "apply",
        "status": status,
        "findings": len(unresolved),
        "before_findings": rounds[0]["before_findings"],
        "severity_counts": dict(sorted(after_counts.items())),
        "applied": total_applied,
        "conflicts": total_conflicts,
        "rolled_back": any_rollback,
        "reason": final_transaction.reason,
        "report_dir": final_applied.proposal_run.summary.report_dir,
        "rounds": rounds,
        "round_count": len(rounds),
        "stop_reason": stop_reason,
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
        "unresolved_events": final_unresolved_events,
        "unresolved_event_count": len(final_unresolved_events),
    }
