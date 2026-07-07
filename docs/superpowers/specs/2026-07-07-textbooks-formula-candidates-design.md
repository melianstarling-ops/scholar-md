# Textbooks Formula Candidates Dry-Run Design

- Date: 2026-07-07
- Status: reviewed design for implementation
- Scope: `scripts/pipelines/textbooks/` formula visual-review candidate aggregation

## Goal

Build a dry-run candidate ledger for formulas that deserve visual review. The ledger does not call any model, does not change Markdown, and does not touch source PDFs. It only gathers already-produced signals into auditable process-side files.

## Inputs

- Existing `worklist.json` from `debug_repair.py`.
- Existing `<stem>_render_errors.json`, including both `errors` and `warnings`.
- Existing page OCR JSON files under `_work/page_NNNN_res.json`.
- Existing `<stem>_selfcheck.json` only for unresolved summary counts, because current selfcheck output has no stable page/block locator for missing formula fragments.

## Outputs

Both outputs live under `DocLayout.repair_dir`.

- `formula_candidates.jsonl`: one candidate per line.
- `formula_candidates_summary.json`: counts, token estimate, and unresolved inputs.

JSONL is intentional: later visual verification can shard or diff large candidate sets line by line without loading the whole file. The rest of the pipeline can still consume it deterministically.

## Candidate Schema

Each candidate is uniquely joined by integer tuple `(page, block_id)`. `candidate_id` is a human-readable display identifier and must not be parsed as the machine key.

```json
{
  "candidate_id": "p0049-b0003",
  "page": 49,
  "block_id": 3,
  "block_label": "display_formula",
  "bbox": [10, 20, 100, 60],
  "reasons": ["worklist:bare_op", "katex_warning:unicodeTextInMathMode"],
  "engine_latex": "...",
  "crop_path": "D:/.../crops/page_0049_block_3.png",
  "estimate_basis": "crop"
}
```

Allowed reason prefixes for v1 are `worklist`, `katex_error`, and `katex_warning`.

- `worklist:*` is only for heuristic worklist reasons such as `bare_op` and `frac_primed_denom`.
- Worklist `render_error` does not create `worklist:render_error`; KaTeX hard errors are represented only as `katex_error:*`.
- `katex_warning:*` includes all KaTeX strict warning codes without filtering.

## Summary Semantics

- `raw_reason_hits`: total reason hits observed before candidate dedupe.
- `deduped_count`: final candidate count, equal to the JSONL line count.
- `by_reason`: number of unique candidates with at least one reason using each prefix. A candidate can count under multiple prefixes, so the values may sum to more than `deduped_count`.
- `estimate_basis_counts`: count of candidates estimated from an existing crop versus a bbox proxy.
- `crop_pixels.max_single`: dimensions of the single largest candidate by estimated pixel area.
- `unresolved_inputs.selfcheck_missing`: count of selfcheck missing items that cannot become v1 candidates because they lack page/block locators.

## Boundaries

- No model calls.
- No `vision_repair.py` execution.
- No Markdown changes.
- No source PDF reads for new rasterization.
- Missing crop paths are allowed. Candidate estimates fall back to bbox dimensions and record `estimate_basis: "bbox_proxy"`.

## Tests

- `DocLayout` exposes formula candidate output paths.
- Worklist items become candidates.
- KaTeX warnings become candidates from the `warnings` array.
- KaTeX hard errors become `katex_error:*` reasons.
- Worklist `render_error` does not duplicate the KaTeX hard-error reason.
- Multiple sources for the same `(page, block_id)` merge into one candidate.
- Missing crops use bbox proxy estimates.
- Selfcheck missing items are reported as unresolved, not emitted as candidates.
- CLI writes JSONL and summary.
