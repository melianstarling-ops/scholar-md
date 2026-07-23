"""Fine-grained event collectors backed by existing conversion artifacts.

The collectors are intentionally separate from the legacy detector/engine
integration. They materialize one RepairEvent per actionable occurrence while
keeping known furniture and stale/unlocatable inputs as count-only metrics.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from scripts.pipelines.textbooks import checkpoint
from scripts.pipelines.textbooks.repair_policy import SEVERE_SOURCE_AUDIT_CODES

from .detectors._shared import page_number, page_result_paths, read_json
from .detectors.unordered_blocks import _UNRESOLVED, _classification, _normalized
from .events import DetectionBatch, RepairEvent
from .formula_state import unresolved_formula_candidates
from .models import DetectorContext, Severity, read_text_exact


_CURRENCY_BODY = re.compile(r"^\d+(?:[.,]\d+)?(?:\s|$)")
_SOURCE_AUDIT_SCHEMA_VERSION = 6


def _canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _mtime_fresh(path: Path, dependencies: Iterable[Path]) -> bool:
    try:
        output_time = path.stat().st_mtime_ns
    except OSError:
        return False
    dependency_times: list[int] = []
    for dependency in dependencies:
        try:
            dependency_times.append(dependency.stat().st_mtime_ns)
        except OSError:
            continue
    return not dependency_times or output_time >= max(dependency_times)


def _block_lookup(context: DetectorContext, page: int) -> tuple[Path, dict[Any, dict]]:
    path = context.work_dir / f"page_{page:04d}_res.json"
    result = read_json(path)
    blocks = {
        block.get("block_id"): block
        for block in (result.get("parsing_res_list") or [])
        if isinstance(block, dict) and block.get("block_id") is not None
    }
    return path, blocks


def _normalized_block_id(value: Any) -> Any:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _file_fingerprint(path: Path) -> dict[str, Any] | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _source_audit_is_fresh(
    context: DetectorContext,
    report: dict[str, Any],
) -> bool:
    """Validate the strong mutable-input fingerprints without rerunning audit."""

    if (report.get("schema_version") != _SOURCE_AUDIT_SCHEMA_VERSION
            or report.get("stem") != context.stem):
        return False
    summary = report.get("summary")
    recorded_ocr = report.get("ocr_fingerprint")
    recorded_pdf = report.get("pdf_fingerprint")
    if (not isinstance(summary, dict)
            or not isinstance(recorded_ocr, dict)
            or not isinstance(recorded_pdf, dict)):
        return False
    page_count = summary.get("pages")
    dpi = recorded_ocr.get("dpi")
    if (not isinstance(page_count, int) or isinstance(page_count, bool)
            or page_count <= 0
            or not isinstance(dpi, int) or isinstance(dpi, bool)
            or dpi <= 0):
        return False
    current_ocr = checkpoint.ocr_results_fingerprint(
        str(context.work_dir), page_count, dpi)
    if current_ocr != recorded_ocr:
        return False

    manifest = checkpoint.load_manifest(str(context.work_dir)) or {}
    raw_pdf_path = manifest.get("pdf_path")
    if not isinstance(raw_pdf_path, str) or not raw_pdf_path.strip():
        return False
    pdf_path = Path(raw_pdf_path)
    if not pdf_path.is_absolute():
        pdf_path = context.work_dir / pdf_path
    try:
        pdf_bytes = pdf_path.read_bytes()
        current_pdf = checkpoint.pdf_fingerprint(str(pdf_path))
    except (OSError, RuntimeError, ValueError):
        return False
    current_pdf["sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
    if current_pdf != recorded_pdf:
        return False

    corrections_path = context.effective_corrections_path
    return report.get("corrections_fingerprint") == _file_fingerprint(
        corrections_path)


def _source_audit_block(
    context: DetectorContext,
    *,
    page: int,
    audit_block_id: Any,
    source_index: Any,
) -> tuple[Path, dict[str, Any] | None, bool]:
    """Join a real block ID; use a list offset only when explicitly reported."""

    page_path = context.work_dir / f"page_{page:04d}_res.json"
    result = read_json(page_path)
    blocks = [
        block for block in (result.get("parsing_res_list") or [])
        if isinstance(block, dict)
    ]
    normalized = _normalized_block_id(audit_block_id)
    for block in blocks:
        if _normalized_block_id(block.get("block_id")) == normalized:
            return page_path, block, False
    if (isinstance(source_index, int) and not isinstance(source_index, bool)
            and 0 <= source_index < len(blocks)):
        return page_path, blocks[source_index], True
    return page_path, None, False


def collect_source_audit_events(
    context: DetectorContext,
) -> tuple[list[RepairEvent], Counter[str]]:
    """Expand fresh severe source-audit findings into block-scoped Agent work."""

    report_path = context.effective_source_audit_path
    metrics: Counter[str] = Counter()
    if not report_path.is_file():
        return [], metrics
    report = read_json(report_path)
    if not report or not _source_audit_is_fresh(context, report):
        metrics["source_audit:stale_or_invalid_report"] += 1
        return [], metrics

    events: list[RepairEvent] = []
    for page_record in report.get("pages") or []:
        if not isinstance(page_record, dict):
            continue
        page = page_record.get("page")
        if (not isinstance(page, int) or isinstance(page, bool) or page <= 0):
            metrics["source_audit:invalid_page"] += 1
            continue
        for issue_index, issue in enumerate(page_record.get("issues") or []):
            if not isinstance(issue, dict):
                continue
            code = issue.get("code")
            if code not in SEVERE_SOURCE_AUDIT_CODES:
                metrics["source_audit:non_actionable_issue"] += 1
                continue
            audit_block_id = issue.get("block_id")
            if audit_block_id is None:
                metrics["source_audit:missing_block_id"] += 1
                continue
            source_index = issue.get("source_index")
            page_path, block, positional = _source_audit_block(
                context, page=page, audit_block_id=audit_block_id,
                source_index=source_index)
            if block is None:
                metrics["source_audit:invalid_target"] += 1
                continue
            if positional:
                metrics["source_audit:positional_block_join"] += 1
            block_id = block.get("block_id")
            if block_id is None:
                block_id = _normalized_block_id(audit_block_id)
            bbox = block.get("block_bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                bbox = None
            detail = issue.get("detail", "")
            detail_text = (
                detail if isinstance(detail, str)
                else json.dumps(detail, ensure_ascii=False, sort_keys=True)
            )
            source_evidence = {
                "code": code,
                "detail": detail,
                "page": page,
                "block_id": audit_block_id,
                "source_index": source_index,
                "bbox": list(bbox) if bbox is not None else None,
            }
            block_input = {
                "block_id": block_id,
                "block_label": block.get("block_label"),
                "block_content": block.get("block_content"),
                "block_bbox": bbox,
            }
            fingerprint = _canonical_fingerprint({
                "source_audit_inputs": {
                    "pdf_fingerprint": report.get("pdf_fingerprint"),
                    "ocr_fingerprint": report.get("ocr_fingerprint"),
                    "corrections_fingerprint": report.get(
                        "corrections_fingerprint"),
                },
                "issue": source_evidence,
                "issue_index": issue_index,
                "block": block_input,
            })
            events.append(RepairEvent.create(
                capability="source_audit",
                kind=f"source_audit_{code}",
                severity=Severity.P1,
                route="quality_agent:source_grounded_repair",
                input_fingerprint=fingerprint,
                page=page,
                block_id=block_id,
                bbox=bbox,
                target={
                    "scope": "block",
                    "page": page,
                    "block_id": block_id,
                    "source_audit_block_id": audit_block_id,
                    "source_issue_index": issue_index,
                },
                message=(
                    f"源文档对账发现 {code}"
                    + (f": {detail_text[:240]}" if detail_text else "")
                ),
                evidence={
                    "source_audit": source_evidence,
                    "source_audit_code": code,
                    "source_audit_detail": detail,
                    "source_audit_page": page,
                    "source_audit_block_id": audit_block_id,
                    "source_audit_source_index": source_index,
                    "source_audit_bbox": (
                        list(bbox) if bbox is not None else None),
                    "content_sample": str(
                        block.get("block_content") or "")[:2000],
                    "source": str(report_path),
                    "page_res_source": str(page_path),
                    "source_grounded": True,
                },
            ))
            metrics[f"source_audit:{code}"] += 1
    metrics["source_audit:actionable_events"] = len(events)
    return events, metrics


def collect_unordered_events(
    context: DetectorContext,
) -> tuple[list[RepairEvent], Counter[str]]:
    """Collect unresolved unordered blocks using their real page-JSON IDs."""

    final_md = _normalized(read_text_exact(context.md_path))
    events: list[RepairEvent] = []
    metrics: Counter[str] = Counter()
    for path in page_result_paths(context.work_dir):
        page = page_number(path)
        result = read_json(path)
        page_width = result.get("width")
        if not isinstance(page_width, (int, float)) or page_width <= 0:
            page_width = None
        for block in result.get("parsing_res_list") or []:
            if not isinstance(block, dict) or block.get("block_order") is not None:
                continue
            classification = _classification(block, page_width)
            metrics[f"unordered:{classification}"] += 1
            if classification not in _UNRESOLVED:
                continue
            content = str(block.get("block_content") or "")
            normalized = _normalized(content)
            if normalized and normalized in final_md:
                metrics["unordered:represented_in_final"] += 1
                continue
            block_id = block.get("block_id")
            if block_id is None:
                metrics["unordered:invalid_block_id"] += 1
                continue
            bbox = block.get("block_bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                bbox = None
            block_input = {
                "block_id": block_id,
                "block_label": block.get("block_label"),
                "block_content": content,
                "block_bbox": bbox,
                "block_order": block.get("block_order"),
            }
            events.append(RepairEvent.create(
                capability="unordered_blocks",
                kind="unordered_block",
                severity=(Severity.P1 if classification == "review_content"
                          else Severity.P2),
                route="quality_agent:block_review",
                input_fingerprint=_canonical_fingerprint(block_input),
                page=page,
                block_id=block_id,
                bbox=bbox,
                target={"scope": "block", "page": page, "block_id": block_id},
                message="未进入 reading order 的有内容块尚未出现在最终 Markdown",
                evidence={
                    "classification": classification,
                    "label": block.get("block_label"),
                    "content_sample": content[:1000],
                    "source": str(path),
                },
            ))
    return events, metrics


def _formula_candidate_paths(context: DetectorContext) -> tuple[Path, Path, Path]:
    repair_dir = context.doc_work_dir / f"{context.stem}_repair"
    return (
        repair_dir / "formula_candidates.jsonl",
        repair_dir / "worklist.json",
        context.doc_work_dir / f"{context.stem}_render_errors.json",
    )


def collect_formula_events(
    context: DetectorContext,
) -> tuple[list[RepairEvent], Counter[str]]:
    """Merge fresh candidate/render records into one event per page/block."""

    candidate_path, worklist_path, render_path = _formula_candidate_paths(context)
    metrics: Counter[str] = Counter()
    candidate_records = _read_jsonl(candidate_path)
    render_data = read_json(render_path)
    render_records = [
        (level, record)
        for level in ("errors", "warnings")
        for record in (render_data.get(level) or [])
        if isinstance(record, dict)
    ]

    candidate_pages = {
        int(record["page"]) for record in candidate_records
        if isinstance(record.get("page"), int)
    }
    candidate_dependencies = [
        dependency for dependency in (worklist_path, render_path)
        if dependency.is_file()
    ] + [
        context.work_dir / f"page_{page:04d}_res.json"
        for page in candidate_pages
    ]
    candidates_fresh = (
        candidate_path.is_file()
        and _mtime_fresh(candidate_path, candidate_dependencies)
    )
    if candidate_records and not candidates_fresh:
        metrics["formula:stale_candidates"] += len(candidate_records)
        candidate_records = []
    if candidate_records:
        pending_candidates, _candidate_states = unresolved_formula_candidates(
            context, candidate_records)
        metrics["formula:terminal_candidates"] += (
            len(candidate_records) - len(pending_candidates))
        candidate_records = pending_candidates

    render_pages = {
        int(record["page"]) for _, record in render_records
        if isinstance(record.get("page"), int)
    }
    render_fresh = (
        render_path.is_file()
        and _mtime_fresh(
            render_path,
            [context.work_dir / f"page_{page:04d}_res.json"
             for page in render_pages],
        )
    )
    if render_records and not render_fresh:
        metrics["formula:stale_render_records"] += len(render_records)
        render_records = []

    merged: dict[tuple[int, Any], dict[str, Any]] = {}
    page_lookups: dict[int, tuple[Path, dict[Any, dict]]] = {}

    def ensure(page: int, block_id: Any) -> dict[str, Any] | None:
        block_id = _normalized_block_id(block_id)
        if page not in page_lookups:
            page_lookups[page] = _block_lookup(context, page)
        page_path, blocks = page_lookups[page]
        block = blocks.get(block_id)
        if block is None:
            metrics["formula:invalid_target"] += 1
            return None
        key = (page, block_id)
        return merged.setdefault(key, {
            "page": page,
            "block_id": block_id,
            "block": block,
            "page_path": page_path,
            "candidates": [],
            "render_errors": [],
            "render_warnings": [],
        })

    for record in candidate_records:
        page = record.get("page")
        block_id = record.get("block_id")
        if not isinstance(page, int) or block_id is None:
            metrics["formula:invalid_candidate"] += 1
            continue
        entry = ensure(page, block_id)
        if entry is not None:
            entry["candidates"].append(record)
            metrics["formula:candidate_records"] += 1

    for level, record in render_records:
        page = record.get("page")
        block_ids = record.get("block_ids") or []
        if not isinstance(page, int) or not isinstance(block_ids, list) or not block_ids:
            metrics["formula:unlocated_render_records"] += 1
            continue
        for block_id in block_ids:
            entry = ensure(page, block_id)
            if entry is None:
                continue
            bucket = "render_errors" if level == "errors" else "render_warnings"
            entry[bucket].append(record)
            metrics[f"formula:{level[:-1]}_block_hits"] += 1

    events: list[RepairEvent] = []
    for (page, block_id), entry in sorted(
        merged.items(), key=lambda item: (item[0][0], str(item[0][1]))
    ):
        block = entry["block"]
        bbox = block.get("block_bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            bbox = None
        reasons = sorted({
            str(reason)
            for record in entry["candidates"]
            for reason in (record.get("reasons") or [])
        })
        crop_paths = sorted({
            str(record["crop_path"])
            for record in entry["candidates"]
            if record.get("crop_path")
        })
        block_input = {
            "block_id": block_id,
            "block_label": block.get("block_label"),
            "block_content": block.get("block_content"),
            "block_bbox": bbox,
            "block_order": block.get("block_order"),
        }
        events.append(RepairEvent.create(
            capability="formulas",
            kind="formula_review",
            severity=(Severity.P1 if entry["render_errors"] else Severity.P2),
            route="formula_agent",
            input_fingerprint=_canonical_fingerprint(block_input),
            page=page,
            block_id=block_id,
            bbox=bbox,
            target={"scope": "block", "page": page, "block_id": block_id},
            message="公式块有候选或 KaTeX 渲染信号，需逐块核验",
            evidence={
                "label": block.get("block_label"),
                "engine_latex": block.get("block_content") or "",
                "reasons": reasons,
                "crop_paths": crop_paths,
                "render_errors": entry["render_errors"],
                "render_warnings": entry["render_warnings"],
                "source": str(entry["page_path"]),
            },
        ))
    metrics["formula:deduped_events"] = len(events)
    return events, metrics


def _is_escaped(text: str, position: int) -> bool:
    slashes = 0
    index = position - 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return bool(slashes % 2)


def _inline_positions(line: str) -> list[int]:
    positions: list[int] = []
    index = 0
    while index < len(line):
        if line[index] == "\\":
            index += 2
            continue
        if line.startswith("$$", index):
            index += 2
            continue
        if line[index] == "$" and not _is_escaped(line, index):
            positions.append(index)
        index += 1
    return positions


def collect_final_delimiter_events(
    context: DetectorContext,
) -> tuple[list[RepairEvent], Counter[str]]:
    """Emit one exact Markdown-span event for every delimiter occurrence."""

    text = read_text_exact(context.md_path)
    events: list[RepairEvent] = []
    metrics: Counter[str] = Counter()
    base = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        positions = _inline_positions(line)
        paired_count = len(positions) - (len(positions) % 2)
        for pair_index in range(0, paired_count, 2):
            start_local = positions[pair_index]
            end_local = positions[pair_index + 1] + 1
            body = line[start_local + 1:end_local - 1]
            if body == body.strip():
                continue
            start = base + start_local
            end = base + end_local
            before = text[start:end]
            events.append(RepairEvent.create(
                capability="final_delimiters",
                kind="inline_math_delimiter_whitespace",
                severity=Severity.P1,
                route="deterministic:final_delimiters",
                input_fingerprint=hashlib.sha256(before.encode("utf-8")).hexdigest(),
                target={
                    "scope": "md_span", "md_start": start, "md_end": end,
                    "line": line_no, "column": start_local + 1,
                },
                message="行内公式定界符内侧有空白",
                evidence={"sample": before[:160]},
            ))
            metrics["delimiter:inline_math_whitespace"] += 1
        if len(positions) % 2:
            start_local = positions[-1]
            start = base + start_local
            end = start + 1
            suffix = line[start_local + 1:]
            currency = bool(_CURRENCY_BODY.match(suffix))
            kind = "ambiguous_currency_delimiter" if currency else "unpaired_math_delimiter"
            events.append(RepairEvent.create(
                capability="final_delimiters",
                kind=kind,
                severity=Severity.P2 if currency else Severity.P1,
                route="quality_agent:md_span",
                input_fingerprint=hashlib.sha256(
                    text[start:end].encode("utf-8")
                ).hexdigest(),
                target={
                    "scope": "md_span", "md_start": start, "md_end": end,
                    "line": line_no, "column": start_local + 1,
                },
                message=("孤立 $ 后接金额，需区分货币与数学定界符"
                         if currency else "行内数学定界符未配对"),
                evidence={"sample": line.rstrip("\r\n")[:160]},
            ))
            metrics[f"delimiter:{kind}"] += 1
        base += len(line)
    return events, metrics


def collect_detection_batch(context: DetectorContext) -> DetectionBatch:
    """Collect all phase-two event families into one immutable batch."""

    events: list[RepairEvent] = []
    metrics: Counter[str] = Counter()
    for collector in (
        collect_source_audit_events,
        collect_unordered_events,
        collect_formula_events,
        collect_final_delimiter_events,
    ):
        emitted, collected_metrics = collector(context)
        events.extend(emitted)
        metrics.update(collected_metrics)
    return DetectionBatch.create(
        stem=context.stem,
        baseline_sha256=context.baseline_sha256,
        events=events,
        metrics=metrics,
    )
