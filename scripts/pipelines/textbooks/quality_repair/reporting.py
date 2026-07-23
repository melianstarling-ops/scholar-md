"""Bounded, atomic audit reports."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .models import Finding


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def write_text(path: Path, text: str) -> None:
    _atomic_text(path, text)


def write_json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_findings(path: Path, findings: Iterable[Finding]) -> None:
    write_records(path, [item.to_dict() for item in findings])


def write_records(path: Path, records: Iterable[dict[str, Any]]) -> None:
    lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records]
    _atomic_text(path, "\n".join(lines) + ("\n" if lines else ""))
