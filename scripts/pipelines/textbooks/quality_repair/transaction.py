"""Single-writer Markdown transaction."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .gates import Gate, GateResult
from .models import PatchPlan


def _fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class TransactionResult:
    applied: int
    rolled_back: bool
    reason: str
    gate_results: tuple[GateResult, ...]


def apply_patch_plan(md_path: str | Path, plan: PatchPlan, *,
                     gates: Iterable[Gate], snapshot_dir: str | Path) -> TransactionResult:
    """Apply once to a sibling temp file; original changes only after every gate passes."""
    path = Path(md_path)
    before_bytes = path.read_bytes()
    if _fingerprint_bytes(before_bytes) != plan.baseline_sha256:
        return TransactionResult(0, False, "baseline hash drift", ())
    if not plan.proposals:
        return TransactionResult(0, False, "empty patch plan", ())
    before = before_bytes.decode("utf-8")
    after = before
    for proposal in sorted(plan.proposals, key=lambda item: item.md_start, reverse=True):
        after = after[:proposal.md_start] + proposal.replacement + after[proposal.md_end:]

    gate_results = tuple(gate(before, after, plan) for gate in gates)
    failed = next((result for result in gate_results if not result.passed), None)
    if failed:
        return TransactionResult(0, True, f"gate {failed.name}: {failed.detail}", gate_results)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".quality-repair.tmp",
                                     dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        snapshot_root = Path(snapshot_dir)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        snapshot = snapshot_root / f"{path.name}.pre_quality_repair.bak"
        shutil.copy2(path, snapshot)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return TransactionResult(len(plan.proposals), False, "", gate_results)
