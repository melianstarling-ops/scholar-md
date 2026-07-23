"""Validate, load, and deterministically cluster learning packages."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from .models import DevelopmentPlan, LearnError, LearningCluster, LearningPackage


REQUIRED_FILES = {
    "finding.json", "evidence_manifest.json", "current_md.txt",
    "expected_behavior.md", "fixture_plan.md", "test_plan.md",
    "lesson_draft.md", "development_brief.md",
}
DEFAULT_ALLOWED_ROOTS = (
    "scripts/pipelines/textbooks/",
    "04_Docs/lessons/lessons_textbooks_quality_repair.md",
)
MAX_CLUSTER_PACKAGES = 8


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LearnError(f"invalid package JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LearnError(f"package JSON must be an object: {path}")
    return value


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown-issue"


def load_package(path: str | Path) -> LearningPackage:
    root = Path(path)
    missing = sorted(name for name in REQUIRED_FILES if not (root / name).is_file())
    if missing:
        raise LearnError(f"incomplete learning package {root}: missing {', '.join(missing)}")
    finding = _read_json(root / "finding.json")
    manifest = _read_json(root / "evidence_manifest.json")
    finding_id = str(finding.get("finding_id") or "").strip()
    decision = manifest.get("agent_decision")
    if not finding_id or not isinstance(decision, dict):
        raise LearnError(f"learning package lacks finding_id/agent_decision: {root}")
    if str(manifest.get("finding_id") or "") != finding_id:
        raise LearnError(f"finding_id mismatch in learning package: {root}")
    family = str(decision.get("issue_family") or finding.get("kind") or "").strip()
    severity = str(decision.get("severity") or finding.get("severity") or "").strip()
    if not family or severity not in {"P0", "P1", "P2"}:
        raise LearnError(f"invalid issue family/severity in learning package: {root}")
    return LearningPackage(root.resolve(), finding_id, family, severity, finding, manifest)


def discover_packages(run_dirs: Iterable[str | Path]) -> list[LearningPackage]:
    packages: list[LearningPackage] = []
    for run in sorted((Path(value).resolve() for value in run_dirs), key=str):
        package_root = run / "learning_packages"
        if not package_root.is_dir():
            continue
        packages.extend(load_package(path) for path in sorted(package_root.iterdir())
                        if path.is_dir())
    if not packages:
        raise LearnError("no complete learning packages found under the supplied run(s)")
    ids = [package.finding_id for package in packages]
    if len(ids) != len(set(ids)):
        raise LearnError("duplicate finding_id across learning packages")
    return packages


def build_plan(run_dirs: Iterable[str | Path], learn_run_id: str) -> DevelopmentPlan:
    resolved_runs = tuple(str(Path(value).resolve()) for value in run_dirs)
    grouped: dict[str, list[LearningPackage]] = {}
    for package in discover_packages(resolved_runs):
        grouped.setdefault(_slug(package.issue_family), []).append(package)
    clusters: list[LearningCluster] = []
    for family_slug, packages in sorted(grouped.items()):
        ordered = tuple(sorted(packages, key=lambda item: item.finding_id))
        for offset in range(0, len(ordered), MAX_CLUSTER_PACKAGES):
            chunk = ordered[offset:offset + MAX_CLUSTER_PACKAGES]
            digest = hashlib.sha256("\n".join(
                item.finding_id for item in chunk).encode("utf-8")).hexdigest()[:10]
            clusters.append(LearningCluster(
                cluster_id=f"{family_slug}-{digest}",
                issue_family=chunk[0].issue_family,
                severities=tuple(sorted({item.severity for item in chunk})),
                packages=chunk,
            ))
    return DevelopmentPlan(
        learn_run_id=learn_run_id,
        source_runs=resolved_runs,
        clusters=tuple(clusters),
        allowed_roots=DEFAULT_ALLOWED_ROOTS,
    )
