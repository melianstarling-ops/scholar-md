# Architecture Review: `formula_candidates.py` Proposal

> Reviewer: Research subagent (read-only, read all textbooks pipeline modules, SOP-03, README, tests, lessons)
>
> Date: 2026-07-07

---

## CRITICAL

### C1. The proposal ignores KaTeX _warnings_ — the most actionable new candidate source

The render_errors file (`scan_katex_errors.mjs` L114, L126-127) outputs **two separate arrays**: `errors` (hard errors, `throwOnError` catches) **and** `warnings` (strict-mode warnings: `unicodeTextInMathMode`, unknown symbols, etc.). The existing `debug_repair.py` only reads `data.get("errors", [])` (L126) — it has **no code to read `warnings`**.

The proposal's schema says `"katex_warning:unicode_text_in_math"` in `reasons` and `"render_errors"` with `"severity": "warning"` in `source_refs`. This implies the new module will read the `warnings` array from `render_errors.json`. **But today's `blocks_from_render_errors()` function (debug_repair.py L57-102) is wired to handle only `errors`** — it uses `latex_head` prefix matching and `block_ids` to locate blocks, which is the same schema for both errors and warnings (L97-99 in .mjs shows warnings carry `block_ids`, `latex_head`, `page`). So reading warnings is feasible with the same locator logic, but the proposal doesn't acknowledge that this is **new capability not present in `debug_repair.py`** — it presents "render_errors" as an existing input, when warnings are an unexercised path.

**Concrete fix**: Explicitly state that `formula_candidates.py` will introduce its own `warnings`-reading logic (or factor out `blocks_from_render_errors` to accept both error and warning records). Add a test that constructs a `render_errors.json` with a `warnings` array entry carrying `block_ids` and verifies it becomes a candidate. Without this, the "7 katex_warning" count in the summary example is aspirational, not grounded in existing code.

---

### C2. `candidate_id` format includes `block_label` but `(page, block_id)` is the actual dedupe key — inconsistency

Looking at every module in the pipeline:
- [corrections.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/corrections.py) L41, L55-56: dedupe/match key is `(page, block_id)`
- [debug_repair.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/debug_repair.py) L106-116 `_merge_hits`: dedupe key is `block_id` alone (within a page)
- [vision_repair.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/vision_repair.py) L219: key is `f"{item['page']}_{item['block_id']}"`
- [debug_payload.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/debug_payload.py) L108: corrections indexed by `block_id`

The proposal says dedupe by `(page, block_id)`, which is correct. But `candidate_id = "p0049-b0003-display_formula"` includes `block_label` — if `block_label` changes across sources, IDs would diverge even though the dedupe key matches.

**Concrete fix**: Either (a) define `candidate_id = f"p{page:04d}-b{block_id:04d}"` without `block_label`, matching `vision_repair.py`'s `key = f"{page}_{block_id}"` pattern, or (b) explicitly document that `candidate_id` is a display string, not a key. Recommend (a).

---

## IMPORTANT

### I1. `kind` field is ambiguous with `block_label` and with existing `kinds` array in `worklist.json`

In `debug_repair.py` worklist schema (L37-43, L75-81), each item has `"kinds": ["bare_op"]` — these are **reason kinds** (why the block is suspicious), not block type. The proposal introduces `"kind": "display_formula"` which is actually the block type, duplicating `block_label`.

Additionally, `vision_repair.py` L191 uses `"kind": "+".join(item.get("kinds", []))` — downstream expects `kind` to be a join of reason kinds, not the block type.

**Concrete fix**: Drop the `kind` field or rename it. Keep `reasons` as the array of why the block is a candidate. Avoids collision with `vision_repair._correction_record`'s `kind`.

---

### I2. `selfcheck.json` does not contain per-block locatable formula misses — the proposal assumes data that doesn't exist

Looking at `selfcheck.py`'s actual output schema (assembled in `convert.py` L200-205):

| Field | Type | Locatable? |
|-------|------|-----------|
| `missing` (block_coverage) | `list[str]` — first 40 chars of content | ❌ No page/block_id/bbox |
| `formula_suspicions` | `[{"op": "\\oint", "count": 13}]` | ❌ Doc-level aggregates |
| `katex_incompat` | `list[str]` — command names | ❌ Just names |

**None of these contain `(page, block_id, bbox)` data.** The `[待确认]` caveat would result in **zero items from selfcheck** reaching the JSONL — everything goes to `unresolved_inputs`.

**Concrete fix**: Either (a) accept selfcheck contributes zero JSONL candidates in v1, note as known gap, or (b) re-derive selfcheck-like signals from `page_NNNN_res.json` per-block (duplicating `debug_repair.py`'s `find_suspicious_blocks` logic). Recommend (a) for v1.

---

### I3. Missing `DocLayout` properties for new paths

Every artifact path goes through `DocLayout` in [paths.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/paths.py) L37-58. The proposal writes to `repair_dir` but doesn't add layout properties.

**Concrete fix**: Add to `DocLayout`:
```python
@property
def formula_candidates_path(self) -> str:
    return os.path.join(self.repair_dir, "formula_candidates.jsonl")

@property
def formula_candidates_summary_path(self) -> str:
    return os.path.join(self.repair_dir, "formula_candidates_summary.json")
```
Plus corresponding tests in `test_paths.py`.

---

### I4. `source_refs` and `reasons` are dual representations of the same info — pick one

The candidate schema has both:
- `reasons: ["worklist:bare_op", "katex_warning:unicode_text_in_math"]` — flat
- `source_refs: [{"source": "worklist", "kind": "bare_op"}, ...]` — structured

These can drift. Existing pipeline style uses simple flat arrays (`worklist.json` items have `"kinds": [...]`). `vision_repair.py` L191 joins kinds with `"+"`.

**Concrete fix**: Keep only `reasons` (flat, consistent with pipeline style). Derive structured data on read if needed later. Don't ship both.

---

### I5. CLI `--out` vs actual output location is misleading

All output goes to `--work-dir` (the `repair_dir` under `work_root`), not `--out` (`deliverables_root`). `--out` is only needed to construct `DocLayout`. This should be documented to avoid user confusion.

---

### I6. Token estimator depends on crop existence, conflicting with "don't re-rasterize" boundary

If crops are missing → can't measure pixels → `crop_pixels` incomplete → `token_estimate` inaccurate.

**Concrete fix**: For missing crops, use `bbox` dimensions as proxy: `bbox_width * (repair_dpi / res_dpi_equivalent)`. State and test this explicitly. Consistent with the "don't re-rasterize" boundary.

---

## MINOR

### M1. `.jsonl` format is inconsistent with rest of pipeline (all `.json`)

Every existing artifact uses `.json` with a top-level object. Unless there's a specific streaming/append requirement, consider a single `formula_candidates.json` with `{"candidates": [...], "summary": {...}}`. If JSONL is preferred for line-level tooling (`grep`, `head`), state the reason explicitly.

### M2. `crop_path` uses absolute paths — fragile across machines

Mirrors `debug_repair.py` L194 (`os.path.abspath`), so consistent. But relative paths from `repair_dir` would be more portable. Low priority for internal artifact.

### M3. No round-trip compatibility test with `vision_repair.py`

If candidates eventually replace the worklist as input to vision repair, test the adapter. Can defer to integration phase.

### M4. `by_reason` summary semantics unclear

`by_reason: {"worklist": 28, ...}` — are these unique `(page, block_id)` pairs per source, or total reason hits? A single worklist item with `kinds: ["bare_op", "frac_primed_denom"]` counts as 1 or 2?

---

## Rule Compliance

| Check | Result |
|-------|--------|
| AGENTS.md §D.1 placement | ✅ `scripts/pipelines/textbooks/` |
| AGENTS.md §H.1/H.2 path handling | ✅ Uses `resolve_layout` |
| AGENTS.md §H.3 pipeline self-contained | ✅ No cross-pipeline deps |
| AGENTS.md §H.5 adaptive I/O | ✅ CLI pattern matches existing |
| AGENTS.md §D.2 naming | ✅ `formula_candidates.py` follows `{action}.py` |
| SOP-03 Tier0 | ⬜ N/A — aggregator, not conversion step |
| No modification of `02_Source/` | ✅ Read-only aggregation |

---

## VERDICT: `approve_with_changes`

The module boundary is sound — a read-only aggregation layer between `debug_repair` outputs and `vision_repair` input is the right abstraction. Required changes before implementation:

1. **C1**: Explicitly handle KaTeX warnings (new capability, not inherited from `debug_repair.py`)
2. **C2**: Fix `candidate_id` to exclude `block_label` or document it as display-only
3. **I1**: Resolve `kind` vs `block_label` naming collision
4. **I2**: Accept selfcheck = zero locatable candidates in v1; be honest about it
5. **I3**: Add `DocLayout` properties for new output paths
6. **I4**: Pick one of `reasons`/`source_refs`, not both

None of these require rethinking the module's purpose or boundaries — they're schema fixes and honest scoping of v1 capabilities.
