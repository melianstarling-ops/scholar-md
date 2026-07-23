"""Deterministic repairers. They only emit proposals and never write files."""
from __future__ import annotations

import hashlib

from scripts.pipelines.textbooks.reconstruct import (
    _find_display_math_end,
    _find_inline_math_end,
    _is_escaped,
)

from .models import Finding, Proposal


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def delimiter_whitespace_proposals(text: str, findings: list[Finding]) -> list[Proposal]:
    owner = next((item for item in findings
                  if item.capability == "final_delimiters"
                  and item.kind == "inline_math_delimiter_whitespace"), None)
    if owner is None:
        return []
    proposals: list[Proposal] = []
    i = 0
    while i < len(text):
        if text[i] != "$" or _is_escaped(text, i):
            i += 1
            continue
        display = i + 1 < len(text) and text[i + 1] == "$"
        if not display and ((i > 0 and text[i - 1] == "$")
                            or (i + 1 < len(text) and text[i + 1] == "$")):
            i += 1
            continue
        width = 2 if display else 1
        start = i + width
        end = (_find_display_math_end(text, start) if display
               else _find_inline_math_end(text, start))
        if end == -1:
            i += width
            continue
        body = text[start:end]
        finish = end + width
        if not display and body != body.strip():
            before = text[i:finish]
            proposals.append(Proposal.create(
                finding_id=owner.finding_id, kind="normalize_inline_math_whitespace",
                md_start=i, md_end=finish, before_fingerprint=_fingerprint(before),
                replacement="$" + body.strip() + "$",
                producer="deterministic:final_delimiters", confidence=1.0,
            ))
        i = finish
    return proposals


def deterministic_proposals(text: str, findings: list[Finding]) -> list[Proposal]:
    return delimiter_whitespace_proposals(text, findings)
