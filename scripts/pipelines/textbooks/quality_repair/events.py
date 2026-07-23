"""Fine-grained repair events and bounded human-facing summaries.

This module is deliberately independent from the existing detector/engine path.
Detectors may migrate to it incrementally without changing the legacy Finding
contract in one step.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import Severity
from .reporting import write_json, write_records


EVENT_SCHEMA_VERSION = 1
_TERMINAL_STATUSES = frozenset({"resolved", "accepted", "applied", "ignored"})


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def _bounded_counts(counter: Counter[str], limit: int) -> dict[str, int]:
    if limit <= 0:
        raise ValueError("summary group limit must be positive")
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    shown = ordered[:limit]
    result = dict(shown)
    omitted = sum(count for _, count in ordered[limit:])
    if omitted:
        result["__other__"] = omitted
    return result


@dataclass(frozen=True)
class RepairEvent:
    """One actionable occurrence.

    ``event_id`` intentionally excludes mutable presentation/provenance fields
    such as message, evidence/crop paths, severity, route, and status. It is
    derived only from the issue family, stable target, and detector input
    fingerprint, so a rerun can join terminal ledger state reliably.
    """

    event_id: str
    capability: str
    kind: str
    severity: Severity
    route: str
    status: str
    input_fingerprint: str
    target: Mapping[str, Any]
    page: int | None = None
    block_id: int | str | None = None
    bbox: tuple[float, float, float, float] | None = None
    message: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = EVENT_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        capability: str,
        kind: str,
        severity: Severity,
        route: str,
        input_fingerprint: str,
        target: Mapping[str, Any],
        status: str = "unresolved",
        page: int | None = None,
        block_id: int | str | None = None,
        bbox: Iterable[float] | None = None,
        message: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> "RepairEvent":
        if not capability.strip() or not kind.strip():
            raise ValueError("capability and kind must be non-empty")
        if not route.strip() or not status.strip():
            raise ValueError("route and status must be non-empty")
        if not input_fingerprint.strip():
            raise ValueError("input_fingerprint must be non-empty")
        if page is not None and page <= 0:
            raise ValueError("page must be one-based")
        normalized_bbox: tuple[float, float, float, float] | None = None
        if bbox is not None:
            raw_bbox = tuple(float(value) for value in bbox)
            if len(raw_bbox) != 4:
                raise ValueError("bbox must contain four coordinates")
            normalized_bbox = raw_bbox
        stable_target = dict(target)
        identity = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "capability": capability,
            "kind": kind,
            "page": page,
            "block_id": block_id,
            "bbox": normalized_bbox,
            "target": stable_target,
            "input_fingerprint": input_fingerprint,
        }
        event_id = hashlib.sha256(
            _canonical(identity).encode("utf-8")
        ).hexdigest()[:20]
        return cls(
            event_id=event_id,
            capability=capability,
            kind=kind,
            severity=severity,
            route=route,
            status=status,
            input_fingerprint=input_fingerprint,
            target=stable_target,
            page=page,
            block_id=block_id,
            bbox=normalized_bbox,
            message=message,
            evidence=dict(evidence or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        if self.bbox is not None:
            data["bbox"] = list(self.bbox)
        return data


@dataclass(frozen=True)
class DetectionBatch:
    """Immutable inventory of detailed events plus count-only detector metrics."""

    stem: str
    baseline_sha256: str
    events: tuple[RepairEvent, ...]
    metrics: Mapping[str, int] = field(default_factory=dict)
    schema_version: int = EVENT_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        stem: str,
        baseline_sha256: str,
        events: Iterable[RepairEvent],
        metrics: Mapping[str, int] | None = None,
    ) -> "DetectionBatch":
        if not stem.strip() or not baseline_sha256.strip():
            raise ValueError("stem and baseline_sha256 must be non-empty")
        ordered = tuple(sorted(events, key=lambda item: item.event_id))
        ids = [item.event_id for item in ordered]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate event_id in detection batch")
        normalized_metrics: dict[str, int] = {}
        for key, value in (metrics or {}).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("detection metrics must be non-negative integers")
            normalized_metrics[str(key)] = value
        return cls(
            stem=stem,
            baseline_sha256=baseline_sha256,
            events=ordered,
            metrics=dict(sorted(normalized_metrics.items())),
        )

    def summary(
        self,
        *,
        sample_limit: int = 10,
        page_sample_limit: int = 50,
        group_limit: int = 50,
    ) -> dict[str, Any]:
        if sample_limit < 0 or page_sample_limit < 0:
            raise ValueError("summary sample limits must be non-negative")
        pages = sorted({event.page for event in self.events if event.page is not None})
        unresolved = sum(
            event.status not in _TERMINAL_STATUSES for event in self.events
        )
        samples = [
            {
                "event_id": event.event_id,
                "capability": event.capability,
                "kind": event.kind,
                "severity": event.severity.value,
                "route": event.route,
                "status": event.status,
                "page": event.page,
                "block_id": event.block_id,
                "message": event.message[:160],
            }
            for event in self.events[:sample_limit]
        ]
        return {
            "schema_version": self.schema_version,
            "stem": self.stem,
            "baseline_sha256": self.baseline_sha256,
            "event_count": len(self.events),
            "unresolved": unresolved,
            "counts_by_capability": _bounded_counts(
                Counter(event.capability for event in self.events), group_limit
            ),
            "counts_by_kind": _bounded_counts(
                Counter(event.kind for event in self.events), group_limit
            ),
            "counts_by_severity": _bounded_counts(
                Counter(event.severity.value for event in self.events), group_limit
            ),
            "counts_by_route": _bounded_counts(
                Counter(event.route for event in self.events), group_limit
            ),
            "counts_by_status": _bounded_counts(
                Counter(event.status for event in self.events), group_limit
            ),
            "affected_pages": {
                "count": len(pages),
                "sample": pages[:page_sample_limit],
                "truncated": len(pages) > page_sample_limit,
            },
            "metrics": _bounded_counts(Counter(self.metrics), group_limit),
            "samples": samples,
            "samples_truncated": len(self.events) > sample_limit,
        }


def write_detection_reports(
    run_dir: str | Path,
    batch: DetectionBatch,
    *,
    sample_limit: int = 10,
    page_sample_limit: int = 50,
    group_limit: int = 50,
) -> tuple[Path, Path]:
    """Atomically write the complete machine ledger and bounded human summary."""

    root = Path(run_dir)
    events_path = root / "events.jsonl"
    summary_path = root / "summary.json"
    write_records(events_path, [event.to_dict() for event in batch.events])
    write_json(
        summary_path,
        batch.summary(
            sample_limit=sample_limit,
            page_sample_limit=page_sample_limit,
            group_limit=group_limit,
        ),
    )
    return events_path, summary_path
