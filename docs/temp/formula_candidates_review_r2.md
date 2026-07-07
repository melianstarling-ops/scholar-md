# Architecture Review Round 2: `formula_candidates.py` Revised Proposal

> Reviewer: Architecture/code review agent
>
> Date: 2026-07-07
>
> Scope: Second-round review of revised formula candidates aggregator design. All textbooks pipeline modules, tests, SOP-03, AGENTS.md, first-round review report, and KaTeX scanner source have been read.

---

## First-Round Response Assessment

The six items from Round 1 (C1, C2, I1–I4) have each been addressed. Quick checklist:

| R1 Item | Claimed Fix | Verified? | Notes |
|---------|------------|-----------|-------|
| C1: KaTeX warnings | ✅ New read logic, not reusing `debug_repair.py` | ✅ | Correctly scoped — `scan_katex_errors.mjs` L87-114 confirms warnings carry `block_ids`, `latex_head`, `page`, `code`, `message` (same locator structure as errors). Independent read logic is the right call. |
| C2: candidate_id format | ✅ `p0049-b0003`, no block_label | ✅ | Consistent with `vision_repair.py` L219 key pattern `{page}_{block_id}` (separator differs — see **I1** below). |
| I1: kind field collision | ✅ Deleted | ✅ | No longer conflicts with `vision_repair._correction_record` L191 `kind` field. |
| I2: selfcheck not locatable | ✅ Moved to `unresolved_inputs`, no JSONL | ✅ | Honest about v1 gap. `selfcheck.json` indeed has no `(page, block_id, bbox)` per block (confirmed in [selfcheck.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/selfcheck.py) L94-99 `summarize_suspicions` and [convert.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/convert.py) L200-205). |
| I3: DocLayout properties | ✅ Two properties added | ✅ | Placement in `repair_dir` is correct — matches `worklist_path` at [paths.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/paths.py) L41-42. |
| I4: reasons/source_refs dual | ✅ Deleted `source_refs` | ✅ | Single representation, consistent with pipeline style. |

> [!NOTE]
> All six Round 1 items are adequately resolved. No regressions from the fixes.

---

## New/Remaining Issues in the Revised Design

### CRITICAL

**(None found.)**

The revised design has no direction-level errors, no violations of AGENTS.md or SOP-03, and no schema contradictions that would block implementation.

---

### IMPORTANT

#### I1. `candidate_id` separator inconsistency with downstream `key`

The revised `candidate_id` format is `p0049-b0003` (hyphen-separated). The existing downstream key format in [vision_repair.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/vision_repair.py#L219) is `f"{item['page']}_{item['block_id']}"` → `49_3` (underscore-separated, no zero-padding).

If `formula_candidates.py` ever feeds into `vision_repair.py` (which it's architecturally positioned to do — the README [L37-38](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/README.md#L37-L38) shows candidates sitting between worklist and vision_repair), there'll be a key format mismatch. This isn't a blocker for v1 dry-run, but:

**Concrete suggestion**: Document that `candidate_id` is a **human-readable display identifier**, not a machine key. The actual dedupe/join key remains the tuple `(page, block_id)` as integers. If a future adapter feeds candidates into vision_repair, it should construct the key from the tuple, not parse the string. This avoids coupling to either format.

---

#### I2. `by_reason` counting semantics remain ambiguous (R1 M4 not resolved)

The revised `by_reason` example:
```json
{"worklist": 28, "katex_error": 5, "katex_warning": 7}
```

Two ambiguities persist:

1. **Count basis**: Is `"worklist": 28` counting 28 unique `(page, block_id)` pairs sourced from worklist, or 28 individual reason hits? A single worklist item with `kinds: ["bare_op", "frac_primed_denom"]` (two reasons) — does it contribute 1 or 2 to the `worklist` count?

2. **Prefix mapping**: The `reasons` array uses prefixes `worklist:`, `katex_error:`, `katex_warning:`. But `by_reason` keys are the **same** prefixes without the colon+detail. The mapping is implicit. What happens if a block has reasons `["worklist:bare_op", "katex_error:ParseError"]`? It contributes 1 to `worklist` AND 1 to `katex_error`? So `sum(by_reason.values())` ≥ `count` (overcounting deduplicated candidates)?

**Concrete suggestion**: Define explicitly:
- `by_reason[prefix]` = number of **unique candidates** (deduplicated by `(page, block_id)`) that have **at least one** reason with that prefix.
- Document that `sum(by_reason.values()) >= deduped_count` because one candidate can appear under multiple prefixes.
- Alternative: make `by_reason` count candidate-reason pairs (not candidates), and rename to `reason_hits` for clarity.

---

#### I3. `estimate_basis` is per-summary but the situation can be mixed

The revised design says:
- Has crop → `estimate_basis: "crop"`
- No crop → `estimate_basis: "bbox_proxy"`

But `estimate_basis` lives in the summary, not per-candidate. If 30 candidates have crops and 5 don't (e.g., crops deleted for disk space, or new katex_warning candidates that never went through `debug_repair.py` which is the only module that creates crops), the summary-level `estimate_basis` can't honestly be either `"crop"` or `"bbox_proxy"`.

**Evidence**: `debug_repair.py` creates crops (L192-193) for worklist items. But katex_warning-sourced candidates are **new** — they don't pass through `debug_repair.py` and thus have **no crops**. So a mixed situation is not hypothetical; it's the **default v1 scenario** (worklist items have crops, katex_warning items don't).

**Concrete suggestion**: Either:
- (a) Make `estimate_basis` per-candidate (add `estimate_basis` field to JSONL schema), and in summary report `{"crop": 30, "bbox_proxy": 5}` counts. This is the cleanest.
- (b) Use summary-level `estimate_basis: "mixed"` with sub-counts `{"crop_count": 30, "bbox_proxy_count": 5}`.

Recommend (a) since it preserves per-candidate granularity for downstream token budgeting.

---

#### I4. KaTeX warning `reasons` prefix `katex_warning:{code}` — the `code` values should be documented

From [scan_katex_errors.mjs](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/debug_assets/scan_katex_errors.mjs#L93-L101) L93-99, the `strict` callback receives `(code, msg)` where `code` is KaTeX's internal strict warning code. Looking at the actual KaTeX source, the `code` values include:
- `unicodeTextInMathMode`
- `unknownSymbol`
- `htmlExtension`
- `newLineInDisplayMode`
- etc.

The `reasons` format `katex_warning:unicodeTextInMathMode` embeds these codes directly. This is fine, but:

1. **The code set is open-ended** (KaTeX upstream can add new codes). The proposal doesn't state whether unrecognized codes are included or filtered.
2. **The prefix set** (`worklist`, `katex_error`, `katex_warning`) is de facto an enum used by both `reasons` array and `by_reason` keys. It should be defined as a constant (e.g., `REASON_PREFIXES = {"worklist", "katex_error", "katex_warning"}`) for validation and future extensibility.

**Concrete suggestion**: 
- Define `REASON_PREFIXES` as a module-level constant.
- Include all KaTeX warning codes (don't filter — the point of dry-run is to surface everything).
- For worklist reasons, the detail part comes from `kinds` in worklist items: `bare_op`, `frac_primed_denom`, `render_error`. Map these to `reasons` as `worklist:bare_op`, `worklist:frac_primed_denom`. Note: `render_error` in worklist `kinds` is already a `katex_error`-class signal (it came from `blocks_from_render_errors`). The proposal should clarify: does a worklist item with `kinds: ["render_error"]` become `reasons: ["worklist:render_error"]` or `reasons: ["katex_error:..."]`? Currently `debug_repair.py` L79 sets `kinds: ["render_error"]` for KaTeX hard errors — so there's a semantic overlap between worklist's `render_error` kind and the new `katex_error` prefix.

**This is the most subtle issue**: A block can appear in worklist with `kinds: ["render_error"]` (because `debug_repair._render_errors_by_page` reads `errors` from `render_errors.json` at L126) AND simultaneously appear as a `katex_error` source when `formula_candidates.py` independently reads the same `errors` array. The proposal should specify dedupe behavior:
- Same block, same underlying signal (KaTeX hard error) → should produce ONE candidate with ONE reason (not two: `worklist:render_error` + `katex_error:ParseError`).

---

### MINOR

#### M1. JSONL vs JSON format consistency (R1 M1 — not addressed)

All existing pipeline artifacts use `.json` with a top-level object:
- `worklist.json` ([debug_repair.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/debug_repair.py#L201) L201): `{"stem", "count", "items": [...]}`
- `_corrections.json` ([vision_repair.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/vision_repair.py#L256) L256): `{"stem", "corrections": [...]}`
- `_selfcheck.json`, `_render_errors.json`: all top-level objects

The proposal uses `.jsonl` (one JSON object per line). This is a deliberate format divergence. If the reason is line-level tooling (`grep`, `head -n5`), state it. If not, consider using a single `.json` with `{"candidates": [...]}` for consistency. Either choice works — just document the rationale.

**Status**: Still unresolved from R1. Not blocking.

---

#### M2. Absolute `crop_path` (R1 M2 — acknowledged, not changed)

The proposal keeps absolute `crop_path`, consistent with [debug_repair.py](file:///G:/Projects/Project_scholar-md/scripts/pipelines/textbooks/debug_repair.py#L194) L194 (`os.path.abspath`). For internal-only artifacts this is fine. The existing worklist uses the same pattern. No action needed.

**Status**: Accepted as-is. Consistent with existing code.

---

#### M3. No vision_repair compatibility test (R1 M3 — deferred)

The proposal creates `test_formula_candidates.py` but doesn't test round-trip with `vision_repair.py`. Since formula_candidates is a dry-run aggregator (not yet feeding vision_repair), this can be deferred to integration phase.

**Status**: Acceptable for v1. Add a TODO comment in the test file.

---

#### M4. `crop_pixels.max` semantics

The summary has `"crop_pixels": {"total": 1234567, "max": [474, 113]}`. Is `max` the single largest crop by pixel count, or the max width/height across all crops? If it's `[max_width, max_height]`, these might come from different crops (misleading). If it's `[width, height]` of the largest crop, say so.

**Concrete suggestion**: Either:
- `"max_single": {"width": 474, "height": 113, "pixels": 53562}` — dimensions of the single largest crop by area
- Or `"max_width": 474, "max_height": 113` — per-axis maxima (explicitly labeled)

---

#### M5. `deduped_count` vs `count` — define clearly

`count: 43` and `deduped_count: 35` — is `count` the total reason-hits across all sources before dedup, and `deduped_count` the unique `(page, block_id)` candidates after dedup? If so, `deduped_count` should be the JSONL line count. State this explicitly.

---

## Rule Compliance

| Check | Result |
|-------|--------|
| AGENTS.md §D.1 placement | ✅ `scripts/pipelines/textbooks/` |
| AGENTS.md §H.1/H.2 path handling | ✅ Uses `resolve_layout`, no hardcoded paths |
| AGENTS.md §H.3 pipeline self-contained | ✅ No cross-pipeline imports |
| AGENTS.md §H.5 adaptive I/O | ✅ CLI follows existing `--out`/`--work-dir`/`--stem` pattern |
| AGENTS.md §H.6 `-X utf8` | ✅ CLI example includes `-X utf8` |
| AGENTS.md §D.2 naming | ✅ `formula_candidates.py` follows `{action}.py` |
| AGENTS.md §C.1 minimum writes | ✅ Read-only aggregation, no modification of source data |
| SOP-03 Tier0 | ⬜ N/A — aggregator, not conversion step |
| No modification of `02_Source/` | ✅ |
| `paths.py` DocLayout consistency | ✅ New properties follow existing pattern (under `repair_dir`) |

---

## VERDICT: `approve_with_changes`

The revised design is substantially improved from Round 1. All Critical items from R1 are resolved, and no new Critical issues emerge. The remaining issues are Important-level schema clarifications, not architectural problems.

### Required before implementation (Important):

1. **I2**: Define `by_reason` counting semantics explicitly (unique candidates per prefix, or reason-hit count?). Document that `sum(by_reason.values()) >= deduped_count` if using per-candidate counting.

2. **I3**: Handle mixed `estimate_basis` — the default v1 scenario has worklist items with crops and katex_warning items without. Either make `estimate_basis` per-candidate in JSONL, or use a `"mixed"` value with sub-counts.

3. **I4**: Clarify the `worklist:render_error` vs `katex_error` overlap for blocks that appear in worklist via `blocks_from_render_errors`. Define whether the same underlying signal produces one or two reasons. Recommend: if a block is already in worklist with `kinds: ["render_error"]`, its reason is `katex_error:{error_msg_prefix}` (not `worklist:render_error`), and the worklist-sourced reasons only cover heuristic kinds (`bare_op`, `frac_primed_denom`). This cleanly separates "why worklist flagged it" from "KaTeX said it's broken."

### Nice-to-have (Minor, non-blocking):

4. **M1**: Document JSONL format rationale (or switch to `.json`).
5. **M4**: Clarify `crop_pixels.max` semantics.
6. **M5**: State that `deduped_count == JSONL line count`.

### Ready for implementation after addressing I2–I4.

None of these require rethinking the module's purpose, boundaries, or CLI. They're specification tightening for a schema that downstream consumers (human reviewers of dry-run output, and eventually vision_repair adapter) will depend on.
