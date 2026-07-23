from __future__ import annotations

from ..models import DetectorContext, Finding, Severity
from ._shared import page_number, page_result_paths, read_json


_PAGE_SAMPLE_LIMIT = 50
_ERROR_SAMPLE_LIMIT = 10


def _ranges(pages: list[int]) -> list[str]:
    if not pages:
        return []
    ranges: list[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ranges


def detect_page_completeness(context: DetectorContext) -> list[Finding]:
    manifest = read_json(context.work_dir / "manifest.json")
    total = int((manifest.get("fingerprint") or {}).get("page_count") or 0)
    present = {page_number(path) for path in page_result_paths(context.work_dir)}
    findings: list[Finding] = []
    if total <= 0:
        findings.append(Finding.create(
            capability="page_completeness", kind="missing_page_inventory",
            severity=Severity.P1, message="manifest 未提供有效总页数",
            evidence={"manifest": str(context.work_dir / "manifest.json")},
        ))
    else:
        missing = sorted(set(range(1, total + 1)) - present)
        failed_pages = {
            item.get("page") for item in (manifest.get("failed_pages") or [])
            if isinstance(item, dict) and isinstance(item.get("page"), int)
        }
        unexplained = [page for page in missing if page not in failed_pages]
        if unexplained:
            findings.append(Finding.create(
                capability="page_completeness", kind="missing_page_results",
                severity=Severity.P0,
                message=f"{len(unexplained)} 页缺少结果文件且 manifest 未解释",
                evidence={"expected_total": total, "count": len(unexplained),
                          "pages": unexplained[:_PAGE_SAMPLE_LIMIT],
                          "pages_truncated": len(unexplained) > _PAGE_SAMPLE_LIMIT,
                          "ranges": _ranges(unexplained)},
            ))
    failures = [item for item in (manifest.get("failed_pages") or [])
                if isinstance(item, dict)]
    if failures:
        pages = sorted({item["page"] for item in failures
                        if isinstance(item.get("page"), int)})
        kinds: dict[str, int] = {}
        for item in failures:
            kind = str(item.get("kind") or "unknown")
            kinds[kind] = kinds.get(kind, 0) + 1
        findings.append(Finding.create(
            capability="page_completeness", kind="failed_pages",
            severity=Severity.P0,
            message=f"manifest 标记 {len(failures)} 个失败页记录",
            evidence={"count": len(failures), "unique_page_count": len(pages),
                      "pages": pages[:_PAGE_SAMPLE_LIMIT],
                      "pages_truncated": len(pages) > _PAGE_SAMPLE_LIMIT,
                      "ranges": _ranges(pages), "kinds": dict(sorted(kinds.items())),
                      "missing_result_count": len([page for page in pages
                                                   if page not in present]),
                      "error_samples": [str(item.get("error") or "")[:300]
                                        for item in failures[:_ERROR_SAMPLE_LIMIT]]},
        ))
    selfcheck = read_json(context.selfcheck_path)
    missing = selfcheck.get("missing") or []
    if missing:
        findings.append(Finding.create(
            capability="page_completeness", kind="block_coverage_missing",
            severity=Severity.P0,
            message="Tier0 block coverage 报告最终 Markdown 有内容缺失",
            evidence={"count": len(missing), "samples": list(missing)[:20]},
        ))
    return findings
