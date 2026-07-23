from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from ..models import DetectorContext, Finding, Severity


_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def detect_assets(context: DetectorContext) -> list[Finding]:
    text = context.md_path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    for index, match in enumerate(_IMAGE.finditer(text)):
        raw = match.group(1).strip().strip("<>")
        if raw.startswith("data:image/"):
            findings.append(Finding.create(
                capability="assets", kind="embedded_base64_asset", severity=Severity.P1,
                message="最终 Markdown 不应内嵌 base64 图片",
                target={"image_index": index}, evidence={"prefix": raw[:40]},
            ))
            continue
        if raw.startswith(("http://", "https://")):
            continue
        relative = raw.split(maxsplit=1)[0]
        base = (context.asset_base_dir or context.md_path.parent).resolve()
        target = (base / Path(unquote(relative))).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            findings.append(Finding.create(
                capability="assets", kind="asset_path_escape", severity=Severity.P0,
                message="Markdown 图片链接越出交付文档目录",
                target={"image_index": index}, evidence={"path": relative},
            ))
            continue
        if not target.is_file():
            findings.append(Finding.create(
                capability="assets", kind="missing_asset", severity=Severity.P0,
                message="Markdown 图片链接目标不存在",
                target={"image_index": index}, evidence={"path": relative},
            ))
    return findings


def asset_issue_counts(text: str, base_dir: Path) -> dict[str, int]:
    counts = {"missing": 0, "base64": 0, "escape": 0}
    base = base_dir.resolve()
    for match in _IMAGE.finditer(text):
        raw = match.group(1).strip().strip("<>")
        if raw.startswith("data:image/"):
            counts["base64"] += 1
            continue
        if raw.startswith(("http://", "https://")):
            continue
        relative = raw.split(maxsplit=1)[0]
        target = (base / Path(unquote(relative))).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            counts["escape"] += 1
            continue
        if not target.is_file():
            counts["missing"] += 1
    return counts
