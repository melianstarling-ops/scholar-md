"""Transaction gate contracts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scripts.pipelines.textbooks.katex_scan import scan_katex
from scripts.pipelines.textbooks.selfcheck import inline_math_delimiter_ws_scan

from .detectors.assets import asset_issue_counts
from .models import PatchPlan


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


class Gate(Protocol):
    def __call__(self, before: str, after: str, plan: PatchPlan) -> GateResult: ...


def nonempty_markdown_gate(_before: str, after: str, _plan: PatchPlan) -> GateResult:
    return GateResult("nonempty_markdown", bool(after.strip()),
                      "" if after.strip() else "candidate markdown is empty")


def delimiter_regression_gate(before: str, after: str, _plan: PatchPlan) -> GateResult:
    old = inline_math_delimiter_ws_scan(before)["count"]
    new = inline_math_delimiter_ws_scan(after)["count"]
    return GateResult("delimiter_regression", new <= old,
                      "" if new <= old else f"inline whitespace findings {old}->{new}")


def asset_regression_gate(base_dir: str | Path) -> Gate:
    base = Path(base_dir)

    def gate(before: str, after: str, _plan: PatchPlan) -> GateResult:
        old = asset_issue_counts(before, base)
        new = asset_issue_counts(after, base)
        passed = all(new[key] <= old[key] for key in ("missing", "base64", "escape"))
        return GateResult("asset_regression", passed, "" if passed else
                          f"missing/base64 {old}->{new}")

    return gate


def katex_regression_gate(run_dir: str | Path) -> Gate:
    root = Path(run_dir)

    def gate(before: str, after: str, _plan: PatchPlan) -> GateResult:
        root.mkdir(parents=True, exist_ok=True)
        before_md = root / "gate_before.md"
        after_md = root / "gate_after.md"
        before_md.write_text(before, encoding="utf-8", newline="")
        after_md.write_text(after, encoding="utf-8", newline="")
        old = scan_katex(str(before_md), str(root / "gate_before_katex.json"))
        new = scan_katex(str(after_md), str(root / "gate_after_katex.json"))
        if old is None or new is None:
            return GateResult("katex_regression", False, "KaTeX scanner unavailable")
        old_count = len(old.get("errors") or [])
        new_count = len(new.get("errors") or [])
        return GateResult("katex_regression", new_count <= old_count,
                          "" if new_count <= old_count else
                          f"KaTeX errors {old_count}->{new_count}")

    return gate


def build_default_gates(md_path: str | Path, run_dir: str | Path) -> list[Gate]:
    path = Path(md_path)
    return [nonempty_markdown_gate, delimiter_regression_gate,
            asset_regression_gate(path.parent), katex_regression_gate(run_dir)]
