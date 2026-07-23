"""Versioned immutable contracts shared by quality-repair stages."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text_exact(path: Path) -> str:
    """Read UTF-8 without universal-newline translation; offsets stay byte-layout stable."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


@dataclass(frozen=True)
class Finding:
    finding_id: str
    capability: str
    kind: str
    severity: Severity
    message: str
    page: int | None = None
    target: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def create(cls, *, capability: str, kind: str, severity: Severity,
               message: str, page: int | None = None,
               target: Mapping[str, Any] | None = None,
               evidence: Mapping[str, Any] | None = None) -> "Finding":
        target = dict(target or {})
        evidence = dict(evidence or {})
        identity = {
            "schema_version": SCHEMA_VERSION,
            "capability": capability,
            "kind": kind,
            "page": page,
            "target": target,
            "evidence": evidence,
        }
        finding_id = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:20]
        return cls(finding_id=finding_id, capability=capability, kind=kind,
                   severity=severity, message=message, page=page,
                   target=target, evidence=evidence)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data


@dataclass(frozen=True)
class DetectorContext:
    stem: str
    md_path: Path
    work_dir: Path
    run_dir: Path
    baseline_sha256: str
    asset_base_dir: Path | None = None
    # Auto repair evaluates an unpublished Markdown candidate.  Its derived
    # page records/index therefore live in an isolated staging work directory,
    # while ``work_dir`` continues to point at the OCR page JSON/manifest.
    derived_work_dir: Path | None = None
    # Unified auto-repair can audit unpublished accepted block corrections.
    # These paths always point into the run staging directory, never at formal
    # sidecars until the final transaction commits.
    corrections_path: Path | None = None
    source_audit_report_path: Path | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_paths(cls, *, stem: str, md_path: str | Path,
                   work_dir: str | Path, run_dir: str | Path,
                   asset_base_dir: str | Path | None = None,
                   derived_work_dir: str | Path | None = None,
                   corrections_path: str | Path | None = None,
                   source_audit_report_path: str | Path | None = None,
                   ) -> "DetectorContext":
        md = Path(md_path)
        if not md.is_file():
            raise FileNotFoundError(f"final markdown not found: {md}")
        work = Path(work_dir)
        if not work.is_dir():
            raise FileNotFoundError(f"page work directory not found: {work}")
        return cls(
            stem=stem, md_path=md, work_dir=work,
            run_dir=Path(run_dir), baseline_sha256=sha256_file(md),
            asset_base_dir=(
                Path(asset_base_dir) if asset_base_dir is not None else None),
            derived_work_dir=(
                Path(derived_work_dir)
                if derived_work_dir is not None else None),
            corrections_path=(
                Path(corrections_path)
                if corrections_path is not None else None),
            source_audit_report_path=(
                Path(source_audit_report_path)
                if source_audit_report_path is not None else None),
        )

    @property
    def doc_work_dir(self) -> Path:
        return self.work_dir.parent

    @property
    def selfcheck_path(self) -> Path:
        return self.doc_work_dir / f"{self.stem}_selfcheck.json"

    @property
    def derived_cache_work_dir(self) -> Path:
        return self.derived_work_dir or self.work_dir

    @property
    def effective_corrections_path(self) -> Path:
        return (
            self.corrections_path
            or self.doc_work_dir / f"{self.stem}_corrections.json"
        )

    @property
    def effective_source_audit_path(self) -> Path:
        return (
            self.source_audit_report_path
            or self.doc_work_dir / f"{self.stem}_source_audit.json"
        )


@dataclass(frozen=True)
class RunSummary:
    stem: str
    mode: str
    status: str
    baseline_sha256: str
    after_sha256: str
    finding_count: int
    counts_by_capability: Mapping[str, int]
    counts_by_severity: Mapping[str, int]
    report_dir: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidencePacket:
    finding_id: str
    issue_kind: str
    severity: str
    md_excerpt: str
    source_evidence: tuple[str, ...]
    target: Mapping[str, Any]
    image_paths: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    finding_id: str
    kind: str
    md_start: int
    md_end: int
    before_fingerprint: str
    replacement: str
    producer: str
    confidence: float
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def create(cls, *, finding_id: str, kind: str, md_start: int, md_end: int,
               before_fingerprint: str, replacement: str, producer: str,
               confidence: float) -> "Proposal":
        identity = {
            "schema_version": SCHEMA_VERSION, "finding_id": finding_id,
            "kind": kind, "md_start": md_start, "md_end": md_end,
            "before_fingerprint": before_fingerprint,
            "replacement": replacement, "producer": producer,
        }
        proposal_id = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:20]
        return cls(proposal_id=proposal_id, finding_id=finding_id, kind=kind,
                   md_start=md_start, md_end=md_end,
                   before_fingerprint=before_fingerprint, replacement=replacement,
                   producer=producer, confidence=confidence)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposalConflict:
    proposal_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class PatchPlan:
    baseline_sha256: str
    proposals: tuple[Proposal, ...]
    conflicts: tuple[ProposalConflict, ...]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "baseline_sha256": self.baseline_sha256,
            "proposals": [item.to_dict() for item in self.proposals],
            "conflicts": [asdict(item) for item in self.conflicts],
        }
