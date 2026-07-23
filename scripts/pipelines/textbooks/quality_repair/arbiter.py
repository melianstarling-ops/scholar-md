"""Central proposal arbiter: drift checks, de-duplication, overlap conflicts."""
from __future__ import annotations

import hashlib

from .models import PatchPlan, Proposal, ProposalConflict


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _overlap(a: Proposal, b: Proposal) -> bool:
    return max(a.md_start, b.md_start) < min(a.md_end, b.md_end)


def arbitrate(baseline: str, proposals: list[Proposal], *,
              baseline_sha256: str) -> PatchPlan:
    if _fingerprint(baseline) != baseline_sha256:
        raise ValueError("baseline_sha256 does not match baseline text")
    conflicts: list[ProposalConflict] = []
    valid: list[Proposal] = []
    for proposal in sorted(proposals, key=lambda item: item.proposal_id):
        if not (0 <= proposal.md_start <= proposal.md_end <= len(baseline)):
            conflicts.append(ProposalConflict((proposal.proposal_id,), "target_out_of_range"))
            continue
        before = baseline[proposal.md_start:proposal.md_end]
        if _fingerprint(before) != proposal.before_fingerprint:
            conflicts.append(ProposalConflict(
                (proposal.proposal_id,), "target_fingerprint_mismatch"))
            continue
        valid.append(proposal)

    deduped: dict[tuple, Proposal] = {}
    for proposal in valid:
        key = (proposal.md_start, proposal.md_end, proposal.before_fingerprint,
               proposal.replacement)
        deduped.setdefault(key, proposal)
    nodes = sorted(deduped.values(), key=lambda item: (item.md_start, item.md_end,
                                                        item.proposal_id))

    edges: dict[str, set[str]] = {item.proposal_id: set() for item in nodes}
    by_id = {item.proposal_id: item for item in nodes}
    insertion_anchors: dict[int, list[Proposal]] = {}
    for proposal in nodes:
        if proposal.md_start == proposal.md_end:
            insertion_anchors.setdefault(proposal.md_start, []).append(proposal)

    conflicted: set[str] = set()
    for anchor, insertions in sorted(insertion_anchors.items()):
        if len(insertions) <= 1:
            continue
        proposal_ids = tuple(sorted(item.proposal_id for item in insertions))
        conflicted.update(proposal_ids)
        conflicts.append(ProposalConflict(
            proposal_ids, "shared_insertion_anchor"))

    for index, left in enumerate(nodes):
        for right in nodes[index + 1:]:
            if right.md_start >= left.md_end:
                break
            if _overlap(left, right):
                edges[left.proposal_id].add(right.proposal_id)
                edges[right.proposal_id].add(left.proposal_id)

    visited: set[str] = set()
    for proposal_id, neighbours in edges.items():
        if not neighbours or proposal_id in visited:
            continue
        stack = [proposal_id]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(edges[current] - component)
        visited.update(component)
        conflicted.update(component)
        conflicts.append(ProposalConflict(tuple(sorted(component)), "overlapping_proposals"))

    survivors = tuple(item for item in nodes if item.proposal_id not in conflicted)
    return PatchPlan(baseline_sha256=baseline_sha256, proposals=survivors,
                     conflicts=tuple(conflicts))
