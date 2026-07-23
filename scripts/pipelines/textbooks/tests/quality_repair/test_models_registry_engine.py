from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.pipelines.textbooks.quality_repair.engine import audit_document
from scripts.pipelines.textbooks.quality_repair.models import (
    DetectorContext,
    Finding,
    Severity,
)
from scripts.pipelines.textbooks.quality_repair.registry import Capability, Registry


def _finding(kind: str, page: int = 1, capability: str = "demo") -> Finding:
    return Finding.create(
        capability=capability,
        kind=kind,
        severity=Severity.P2,
        message="demo",
        page=page,
        evidence={"value": 1},
    )


def _context(tmp_path: Path) -> DetectorContext:
    md_path = tmp_path / "Demo.md"
    md_path.write_text("alpha\n", encoding="utf-8")
    work_dir = tmp_path / "_work"
    work_dir.mkdir()
    return DetectorContext.from_paths(
        stem="Demo",
        md_path=md_path,
        work_dir=work_dir,
        run_dir=tmp_path / "quality_repair" / "run-1",
    )


def test_finding_id_is_stable_and_evidence_order_independent():
    a = Finding.create(
        capability="demo", kind="gap", severity=Severity.P1,
        message="first wording", page=3, target={"block_id": 7},
        evidence={"b": 2, "a": 1},
    )
    b = Finding.create(
        capability="demo", kind="gap", severity=Severity.P1,
        message="different wording", page=3, target={"block_id": 7},
        evidence={"a": 1, "b": 2},
    )
    assert a.finding_id == b.finding_id
    assert a.to_dict()["severity"] == "P1"


def test_registry_execution_is_name_sorted_not_registration_order(tmp_path):
    def detector_a(_):
        return [_finding("a", capability="a")]

    def detector_b(_):
        return [_finding("b", capability="b")]

    ctx = _context(tmp_path)
    one = Registry([Capability("b", detector_b), Capability("a", detector_a)])
    two = Registry([Capability("a", detector_a), Capability("b", detector_b)])
    assert [f.finding_id for f in one.detect(ctx)] == [f.finding_id for f in two.detect(ctx)]
    assert [f.kind for f in one.detect(ctx)] == ["a", "b"]


def test_audit_writes_bounded_reports_but_never_changes_markdown(tmp_path):
    ctx = _context(tmp_path)
    before = hashlib.sha256(ctx.md_path.read_bytes()).hexdigest()
    registry = Registry([Capability("demo", lambda _: [_finding("gap")])])

    summary = audit_document(ctx, registry=registry)

    after = hashlib.sha256(ctx.md_path.read_bytes()).hexdigest()
    assert before == after == summary.baseline_sha256 == summary.after_sha256
    assert summary.mode == "audit"
    assert summary.finding_count == 1
    assert (ctx.run_dir / "config.json").is_file()
    assert (ctx.run_dir / "findings.jsonl").is_file()
    assert (ctx.run_dir / "summary.json").is_file()
    payload = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert payload["status"] == "SUSPECT"


def test_registry_rejects_duplicate_capability_names():
    cap = Capability("same", lambda _: [])
    try:
        Registry([cap, cap])
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
    else:
        raise AssertionError("duplicate capability name must fail")
