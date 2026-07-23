from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.pipelines.textbooks.quality_repair.arbiter import arbitrate
from scripts.pipelines.textbooks.quality_repair.gates import GateResult
from scripts.pipelines.textbooks.quality_repair.models import Proposal
from scripts.pipelines.textbooks.quality_repair.transaction import apply_patch_plan


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _proposal(*, finding: str, start: int, end: int,
              before: str, replacement: str) -> Proposal:
    return Proposal.create(
        finding_id=finding, kind="replace_text", md_start=start, md_end=end,
        before_fingerprint=_fingerprint(before), replacement=replacement,
        producer="test", confidence=1.0,
    )


def test_arbiter_deduplicates_identical_proposals():
    baseline = "alpha beta"
    a = _proposal(finding="f1", start=0, end=5, before="alpha", replacement="ALPHA")
    b = _proposal(finding="f2", start=0, end=5, before="alpha", replacement="ALPHA")
    plan = arbitrate(baseline, [b, a], baseline_sha256=_fingerprint(baseline))
    assert len(plan.proposals) == 1
    assert plan.conflicts == ()


def test_arbiter_rejects_overlapping_disagreeing_proposals():
    baseline = "alpha beta"
    a = _proposal(finding="f1", start=0, end=5, before="alpha", replacement="ALPHA")
    b = _proposal(finding="f2", start=3, end=8, before="ha be", replacement="X")
    plan = arbitrate(baseline, [a, b], baseline_sha256=_fingerprint(baseline))
    assert plan.proposals == ()
    assert len(plan.conflicts) == 1
    assert set(plan.conflicts[0].proposal_ids) == {a.proposal_id, b.proposal_id}


def test_arbiter_rejects_distinct_insertions_at_shared_zero_width_anchor():
    baseline = "alpha beta"
    a = _proposal(finding="f1", start=5, end=5, before="", replacement="\nfirst")
    b = _proposal(finding="f2", start=5, end=5, before="", replacement="\nsecond")

    plan = arbitrate(
        baseline, [b, a], baseline_sha256=_fingerprint(baseline))

    assert plan.proposals == ()
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].reason == "shared_insertion_anchor"
    assert set(plan.conflicts[0].proposal_ids) == {a.proposal_id, b.proposal_id}


def test_arbiter_keeps_one_insertion_at_zero_width_anchor():
    baseline = "alpha beta"
    insertion = _proposal(
        finding="f1", start=5, end=5, before="", replacement="\nfirst")

    plan = arbitrate(
        baseline, [insertion], baseline_sha256=_fingerprint(baseline))

    assert plan.proposals == (insertion,)
    assert plan.conflicts == ()


def test_arbiter_rejects_target_fingerprint_drift():
    proposal = _proposal(finding="f1", start=0, end=5,
                         before="wrong", replacement="ALPHA")
    plan = arbitrate("alpha beta", [proposal], baseline_sha256=_fingerprint("alpha beta"))
    assert plan.proposals == ()
    assert plan.conflicts[0].reason == "target_fingerprint_mismatch"


def test_transaction_applies_once_via_temp_file(tmp_path):
    md = tmp_path / "Demo.md"
    md.write_text("alpha beta", encoding="utf-8")
    proposal = _proposal(finding="f1", start=0, end=5,
                         before="alpha", replacement="ALPHA")
    plan = arbitrate("alpha beta", [proposal], baseline_sha256=_fingerprint("alpha beta"))
    snapshots = tmp_path / "run"
    result = apply_patch_plan(
        md, plan, gates=[lambda *_: GateResult("ok", True, "")],
        snapshot_dir=snapshots,
    )
    assert result.applied == 1 and result.rolled_back is False
    assert md.read_text(encoding="utf-8") == "ALPHA beta"
    assert (snapshots / "Demo.md.pre_quality_repair.bak").read_text(
        encoding="utf-8") == "alpha beta"
    assert not (md.parent / "Demo.md.pre_quality_repair.bak").exists()


def test_transaction_gate_failure_leaves_markdown_byte_identical(tmp_path):
    md = tmp_path / "Demo.md"
    md.write_text("alpha beta", encoding="utf-8")
    before = md.read_bytes()
    proposal = _proposal(finding="f1", start=0, end=5,
                         before="alpha", replacement="ALPHA")
    plan = arbitrate("alpha beta", [proposal], baseline_sha256=_fingerprint("alpha beta"))
    result = apply_patch_plan(
        md, plan, gates=[lambda *_: GateResult("forced", False, "regression")],
        snapshot_dir=tmp_path / "run",
    )
    assert result.applied == 0 and result.rolled_back is True
    assert md.read_bytes() == before


def test_transaction_baseline_drift_fails_before_writing(tmp_path):
    md = tmp_path / "Demo.md"
    md.write_text("changed", encoding="utf-8")
    proposal = _proposal(finding="f1", start=0, end=5,
                         before="alpha", replacement="ALPHA")
    plan = arbitrate("alpha beta", [proposal], baseline_sha256=_fingerprint("alpha beta"))
    result = apply_patch_plan(md, plan, gates=[], snapshot_dir=tmp_path / "run")
    assert result.applied == 0 and "baseline" in result.reason
    assert md.read_text(encoding="utf-8") == "changed"
