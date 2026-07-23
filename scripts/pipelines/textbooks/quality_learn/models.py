"""Data contracts for the quality-learning development workflow."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1


class LearnError(RuntimeError):
    """A fail-loud safety or protocol error."""


@dataclass(frozen=True)
class LearningPackage:
    path: Path
    finding_id: str
    issue_family: str
    severity: str
    finding: Mapping[str, Any]
    evidence_manifest: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "finding_id": self.finding_id,
            "issue_family": self.issue_family,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class LearningCluster:
    cluster_id: str
    issue_family: str
    severities: tuple[str, ...]
    packages: tuple[LearningPackage, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "issue_family": self.issue_family,
            "severities": list(self.severities),
            "packages": [package.to_dict() for package in self.packages],
        }


@dataclass(frozen=True)
class DevelopmentPlan:
    learn_run_id: str
    source_runs: tuple[str, ...]
    clusters: tuple[LearningCluster, ...]
    allowed_roots: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "learn_run_id": self.learn_run_id,
            "source_runs": list(self.source_runs),
            "allowed_roots": list(self.allowed_roots),
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }


@dataclass(frozen=True)
class DevelopResponse:
    issue_family: str
    test_patch: str
    implementation_patch: str
    target_tests: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ReviewResponse:
    verdict: str
    findings: tuple[str, ...]
    confidence: float
    summary: str


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
