from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def page_result_paths(work_dir: Path) -> list[Path]:
    return sorted(work_dir.glob("page_[0-9][0-9][0-9][0-9]_res.json"))


def page_number(path: Path) -> int:
    return int(path.stem.split("_")[1])
