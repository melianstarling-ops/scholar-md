"""Dry-run aggregation of formula candidates for later visual review."""
from __future__ import annotations

import argparse
import json
import os

from PIL import Image

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout


REASON_PREFIXES = {"worklist", "katex_error", "katex_warning"}


def candidate_id(page: int, block_id: int) -> str:
    """Human-readable display id. Machine joins use (page, block_id)."""
    return f"p{page:04d}-b{block_id:04d}"


def estimate_candidate_tokens(candidates: list[dict]) -> dict:
    """Small deterministic estimate for dry-run budgeting."""
    pixels = sum(max(0, int(c.get("estimated_pixels", 0))) for c in candidates)
    image_low = max(1, pixels // 2000) if candidates else 0
    image_high = max(image_low, pixels // 1000) if candidates else 0
    output_low = len(candidates) * 90
    output_high = len(candidates) * 210
    return {
        "image_tokens_low": image_low,
        "image_tokens_high": image_high,
        "output_tokens_low": output_low,
        "output_tokens_high": output_high,
        "total_tokens_low": image_low + output_low,
        "total_tokens_high": image_high + output_high,
    }


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _page_blocks_by_id(layout: DocLayout, page: int) -> dict[int, dict]:
    return {
        b.get("block_id"): b
        for b in cp.load_page_blocks(layout.work_dir, page)
        if b.get("block_id") is not None
    }


def _error_detail(record: dict) -> str:
    raw = record.get("error") or record.get("message") or "unknown"
    return str(raw).split(":", 1)[0].strip() or "unknown"


def _warning_detail(record: dict) -> str:
    return str(record.get("code") or "unknown")


def _new_candidate(page: int, block_id: int, block: dict | None, item: dict | None = None) -> dict:
    source = item or block or {}
    bbox = source.get("bbox") or source.get("block_bbox")
    engine_latex = source.get("engine_latex") or source.get("block_content") or ""
    return {
        "candidate_id": candidate_id(page, block_id),
        "page": page,
        "block_id": block_id,
        "block_label": (block or {}).get("block_label"),
        "bbox": bbox,
        "reasons": [],
        "engine_latex": engine_latex,
        "crop_path": source.get("crop_path"),
    }


def _add_reason(candidates: dict[tuple[int, int], dict], page: int, block_id: int,
                reason: str, block: dict | None = None, item: dict | None = None) -> None:
    key = (page, block_id)
    if key not in candidates:
        candidates[key] = _new_candidate(page, block_id, block, item)
    cand = candidates[key]
    if block:
        cand["block_label"] = cand.get("block_label") or block.get("block_label")
        cand["bbox"] = cand.get("bbox") or block.get("block_bbox")
        cand["engine_latex"] = cand.get("engine_latex") or block.get("block_content") or ""
    if item:
        cand["crop_path"] = cand.get("crop_path") or item.get("crop_path")
        cand["bbox"] = cand.get("bbox") or item.get("bbox")
        cand["engine_latex"] = cand.get("engine_latex") or item.get("engine_latex") or ""
    if reason not in cand["reasons"]:
        cand["reasons"].append(reason)


def _add_worklist(candidates: dict[tuple[int, int], dict], layout: DocLayout) -> int:
    hits = 0
    data = _read_json(layout.worklist_path)
    for item in data.get("items", []):
        page = item.get("page")
        block_id = item.get("block_id")
        if page is None or block_id is None:
            continue
        block = _page_blocks_by_id(layout, int(page)).get(block_id)
        for kind in item.get("kinds", []):
            if kind == "render_error":
                continue
            _add_reason(candidates, int(page), int(block_id), f"worklist:{kind}", block, item)
            hits += 1
    return hits


def _add_katex_records(candidates: dict[tuple[int, int], dict], layout: DocLayout,
                       records: list[dict], prefix: str) -> int:
    hits = 0
    for record in records:
        page = record.get("page")
        if page is None:
            continue
        blocks = _page_blocks_by_id(layout, int(page))
        detail = _error_detail(record) if prefix == "katex_error" else _warning_detail(record)
        reason = f"{prefix}:{detail}"
        for block_id in record.get("block_ids", []):
            block = blocks.get(block_id)
            if block is None:
                continue
            _add_reason(candidates, int(page), int(block_id), reason, block=block)
            hits += 1
    return hits


def _add_katex(candidates: dict[tuple[int, int], dict], layout: DocLayout) -> int:
    data = _read_json(layout.render_errors_path)
    return (
        _add_katex_records(candidates, layout, data.get("errors", []), "katex_error")
        + _add_katex_records(candidates, layout, data.get("warnings", []), "katex_warning")
    )


def _selfcheck_unresolved(layout: DocLayout) -> dict:
    data = _read_json(layout.selfcheck_path)
    missing = data.get("missing", [])
    if not missing:
        return {}
    return {
        "selfcheck_missing": {
            "count": len(missing),
            "reason": "selfcheck v1 has no page/block_id locator",
        }
    }


def _estimate_one(candidate: dict) -> None:
    crop_path = candidate.get("crop_path")
    if crop_path and os.path.exists(crop_path):
        with Image.open(crop_path) as img:
            width, height = img.size
        candidate["estimate_basis"] = "crop"
    else:
        bbox = candidate.get("bbox") or [0, 0, 0, 0]
        width = max(0, int(float(bbox[2]) - float(bbox[0]))) if len(bbox) == 4 else 0
        height = max(0, int(float(bbox[3]) - float(bbox[1]))) if len(bbox) == 4 else 0
        candidate["crop_path"] = crop_path if crop_path and os.path.exists(crop_path) else None
        candidate["estimate_basis"] = "bbox_proxy"
    candidate["estimated_width"] = width
    candidate["estimated_height"] = height
    candidate["estimated_pixels"] = width * height


def _summary(stem: str, candidates: list[dict], raw_reason_hits: int,
             unresolved_inputs: dict, layout: DocLayout) -> dict:
    by_reason: dict[str, int] = {}
    basis_counts: dict[str, int] = {}
    max_single = {"width": 0, "height": 0, "pixels": 0}
    total_pixels = 0
    for candidate in candidates:
        prefixes = {reason.split(":", 1)[0] for reason in candidate["reasons"]}
        for prefix in sorted(prefixes):
            by_reason[prefix] = by_reason.get(prefix, 0) + 1
        basis = candidate["estimate_basis"]
        basis_counts[basis] = basis_counts.get(basis, 0) + 1
        pixels = candidate["estimated_pixels"]
        total_pixels += pixels
        if pixels > max_single["pixels"]:
            max_single = {
                "width": candidate["estimated_width"],
                "height": candidate["estimated_height"],
                "pixels": pixels,
            }
    return {
        "stem": stem,
        "raw_reason_hits": raw_reason_hits,
        "deduped_count": len(candidates),
        "by_reason": by_reason,
        "reason_count_note": "Counts unique candidates per reason prefix; sum may exceed deduped_count.",
        "estimate_basis_counts": basis_counts,
        "crop_pixels": {"total": total_pixels, "max_single": max_single},
        "token_estimate": estimate_candidate_tokens(candidates),
        "unresolved_inputs": unresolved_inputs,
        "outputs": {
            "jsonl": layout.formula_candidates_path,
            "summary": layout.formula_candidates_summary_path,
        },
    }


def collect_formula_candidates(layout: DocLayout, write: bool = False) -> dict:
    candidates_by_key: dict[tuple[int, int], dict] = {}
    raw_hits = _add_worklist(candidates_by_key, layout)
    raw_hits += _add_katex(candidates_by_key, layout)
    candidates = [candidates_by_key[key] for key in sorted(candidates_by_key)]
    for candidate in candidates:
        candidate["reasons"].sort()
        _estimate_one(candidate)
    summary = _summary(layout.stem, candidates, raw_hits, _selfcheck_unresolved(layout), layout)
    if write:
        os.makedirs(layout.repair_dir, exist_ok=True)
        with open(layout.formula_candidates_path, "w", encoding="utf-8") as f:
            for candidate in candidates:
                f.write(json.dumps(candidate, ensure_ascii=False) + "\n")
        with open(layout.formula_candidates_summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    return {"candidates": candidates, "summary": summary}


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks formula candidate dry-run aggregator")
    ap.add_argument("--out", required=True, help="交付根(md+assets)")
    ap.add_argument("--work-dir", default=None, help="过程根(默认 <out>/_work_root)")
    ap.add_argument("--stem", required=True, help="文档 stem")
    args = ap.parse_args()
    layout = resolve_layout(args.stem, args.out, args.work_dir)
    result = collect_formula_candidates(layout, write=True)
    summary = result["summary"]
    print(
        f"[formula_candidates] {summary['deduped_count']} candidates "
        f"({summary['raw_reason_hits']} reason hits) -> {layout.formula_candidates_path}"
    )


if __name__ == "__main__":
    main()
