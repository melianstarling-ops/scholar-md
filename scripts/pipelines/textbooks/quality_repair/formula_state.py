"""Resolve current formula-candidate state from existing Agent artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from scripts.pipelines.textbooks.vision_repair import content_fingerprint

from .models import DetectorContext


_NON_MUTATING_TERMINAL = frozenset({"accept", "not_formula_error"})
_ACTIVE_CORRECTION_STATUS = frozenset({"accepted", "applied"})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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


def formula_candidate_states(
    context: DetectorContext,
    candidates: Iterable[dict[str, Any]],
) -> dict[str, str]:
    """Return candidate_id -> unresolved|ignored|applied.

    A historical candidate file is not itself pending. Non-mutating terminal
    verdicts close a candidate; a mutating verdict closes only when its current
    correction is active and still fingerprints the candidate engine input.
    """

    repair_dir = context.doc_work_dir / f"{context.stem}_repair"
    verdicts: dict[str, dict[str, Any]] = {}
    ledger_rows = _read_jsonl(repair_dir / "formula_agent_ledger.jsonl")
    pending_state: dict[str, bool] = {}
    for row in ledger_rows:
        for resolved in row.get("resolved") or []:
            if isinstance(resolved, dict) and resolved.get("candidate_id"):
                candidate_id = str(resolved["candidate_id"])
                verdicts[candidate_id] = resolved
                pending_state[candidate_id] = False
        for candidate_id in row.get("pending_ids") or []:
            pending_state[str(candidate_id)] = True
    for row in _read_jsonl(repair_dir / "formula_agent_verdicts.jsonl"):
        if row.get("candidate_id"):
            verdicts[str(row["candidate_id"])] = row

    corrections_by_id: dict[str, dict[str, Any]] = {}
    corrections = _read_json(
        context.doc_work_dir / f"{context.stem}_corrections.json")
    for correction in corrections.get("corrections") or []:
        if isinstance(correction, dict) and correction.get("candidate_id"):
            corrections_by_id[str(correction["candidate_id"])] = correction

    states: dict[str, str] = {}
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            continue
        if pending_state.get(candidate_id) is True:
            states[candidate_id] = "unresolved"
            continue
        verdict_record = verdicts.get(candidate_id) or {}
        verdict = str(verdict_record.get("verdict") or "")
        current_fingerprint = content_fingerprint(
            str(candidate.get("engine_latex") or ""))
        verdict_current = (
            verdict_record.get("content_fingerprint") == current_fingerprint
        )
        if verdict in _NON_MUTATING_TERMINAL and verdict_current:
            states[candidate_id] = "ignored"
            continue
        correction = corrections_by_id.get(candidate_id) or {}
        correction_current = (
            correction.get("status") in _ACTIVE_CORRECTION_STATUS
            and correction.get("page") == candidate.get("page")
            and correction.get("block_id") == candidate.get("block_id")
            and correction.get("content_fingerprint")
            == current_fingerprint
        )
        if verdict == "correct" and correction_current:
            states[candidate_id] = "applied"
        else:
            states[candidate_id] = "unresolved"
    return states


def unresolved_formula_candidates(
    context: DetectorContext,
    candidates: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    items = list(candidates)
    states = formula_candidate_states(context, items)
    return [
        candidate for candidate in items
        if (not str(candidate.get("candidate_id") or "")
            or states.get(str(candidate.get("candidate_id"))) == "unresolved")
    ], states
