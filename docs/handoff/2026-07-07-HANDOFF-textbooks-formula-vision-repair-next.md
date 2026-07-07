# HANDOFF: textbooks formula visual repair next pipeline

Date: 2026-07-07

## Context

Current milestone: `Paul_Analysis_MTL_scan` has passed deterministic KaTeX hard-error cleanup.

User manually checked the recent KaTeX fixes and table rendering and reported both look good. Treat that as operator QA evidence for the sampled pages, not as a whole-book visual proof.

Important workspace rules:

- Read `01_System/SOP-03_Conversion_SelfCheck.md` before conversion/selfcheck work in a new session.
- Do not modify anything under `02_Source/`.
- Use `.venv-textbooks\Scripts\python.exe -X utf8` for Python commands in this textbooks workflow.
- Do not run `vision_repair` or model/visual repair without explicit owner approval.
- For commit-time lesson capture, follow `01_System/SOP-06_Lesson_Capture.md`.

## Key Paths

- Repo/workspace: `G:\Projects\Project_scholar-md`
- Source PDF, read-only: `02_Source/textbooks_samples/Paul_Analysis_MTL_scan.pdf`
- Deliverables dir: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/`
- Final Markdown: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan.md`
- Work root: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\`
- Render errors JSON: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_render_errors.json`
- Repair worklist: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_repair\worklist.json`
- Debug HTML: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html`
- Conversion report: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_conversion_report_2026-07-07.md`

## Current Verified State

Fresh verification from this continuation:

```text
KaTeX scan: 7703 formulas, 0 hard errors, 7 warnings
Repair worklist: 28 suspicious formula items
Textbooks tests: 310 passed
node --check scripts/pipelines/textbooks/debug_assets/app.js: exit 0
```

Formula counts in final Markdown:

```text
Total math spans: 7703
Display math spans: 2772
Inline math spans: 4931
OCR display_formula blocks: 2771
OCR table blocks: 16
```

Remaining review items:

- 7 KaTeX warnings:
  - p44: display `\\` strict warning.
  - p49: Unicode `↳` in math.
  - p154: en dash in inline math.
  - p172: `Ω` used as a formula tag.
  - p433: display `\\` strict warning.
  - p627: display `\\` strict warning.
  - p745: array column-count warning.
- 28 repair worklist suspicious formula items.
- 16 selfcheck formula-fragment coverage misses.

User-requested manual audit samples already provided:

- KaTeX fix sample pages: p532, p681, p728.
- Table sample pages: p76, p207, p210.

## What Was Fixed Before This Handoff

Deterministic reconstruction/sanitization now handles the hard-error classes found in `Paul_Analysis_MTL_scan`:

- Broken debug-HTML inline math delimiter padding on p691.
- OCR pseudo `\bmatrix{...}\end{bmatrix}` wrapped in `\mathrm`.
- Split `\left...\right` delimiters across aligned/array rows.
- One-argument malformed integral `\frac{...}` runs.
- Split `a^{\prime}` integral upper limit before E-field integrands.
- Scripts attached to spacing commands such as `\quad_{(12)}`.
- Missing `\end{array}` before a closing `\right]`.
- Underspecified array column specs where the row evidence is deterministic.

## Problem To Solve Next

The next pipeline should detect OCR formula errors that do not necessarily produce KaTeX hard errors.

KaTeX hard-error scan is necessary but insufficient:

- It catches parse failures and some renderer-incompatibility issues.
- It does not prove the formula visually matches the source.
- It cannot catch semantically wrong but syntactically valid OCR, such as wrong subscript letter, dropped prime, wrong sign, wrong vector accent, or row-order damage.

## Error Detection Strategy

Use a tiered funnel, cheapest first:

1. Deterministic gates:
   - KaTeX hard errors from `render_errors.json`.
   - KaTeX strict warnings.
   - `selfcheck` formula-fragment coverage misses.
   - `debug_repair` worklist suspicion signals (`\int`, `\sum`, `\oint`, suspicious denominator fragments, etc.).
   - Block-level anomalies: formula/table labels, missing formula numbers, duplicate/stranded tags, array column mismatch, unknown labels such as `inline_formula`.

2. Local visual evidence:
   - For each candidate, crop the formula bbox from the source page image.
   - Preserve context metadata: page, block_id, bbox, engine LaTeX, nearby formula_number, current KaTeX status, suspicion reasons.
   - Do not send the whole page unless the formula is split across blocks or context is required.

3. Model/agent verification:
   - Ask the model to compare crop image against current LaTeX.
   - Output a structured verdict:
     - `accept`: current LaTeX visually matches.
     - `correct`: provide corrected LaTeX.
     - `uncertain`: needs human review.
     - `not_formula_error`: issue is table/layout/debug rendering, not formula OCR.
   - Require a short evidence note keyed to visible symbols, not a free-form explanation.

4. Human gate:
   - Accepted AI corrections must remain reviewable in debug HTML.
   - Do not auto-apply model corrections into final Markdown without either explicit owner approval or an accepted correction status.

## Agent Arrangement

Recommended multi-agent layout:

1. Inventory agent:
   - Read only.
   - Groups candidates from `render_errors.json`, `worklist.json`, selfcheck misses, and warnings.
   - Produces a deduped JSONL candidate list.

2. Visual verifier agents:
   - Work on disjoint candidate shards.
   - Each receives only crop path + current LaTeX + metadata.
   - Returns structured verdicts.
   - No code edits.

3. Deterministic-pattern agent:
   - Looks for repeated OCR patterns among model/human-confirmed corrections.
   - Proposes sanitizer rules only when 2+ similar cases exist or one case is clearly structural and safe.
   - Must write failing tests before code changes.

4. Integrator/reviewer:
   - Applies accepted corrections or deterministic sanitizer changes.
   - Reruns KaTeX scan, worklist generation, debug HTML generation, and tests.
   - Confirms no new hard errors and no regression in table rendering.

Do not dispatch multiple writer agents against `reconstruct.py` at the same time. Visual verifier agents can run in parallel because they should be read-only or produce separate verdict files.

## Token Budget Notes

Current single-book estimates:

```text
Display formula crops: 2771
Average display crop: about 474 x 113 px
Estimated display-crop image input: about 0.90M image tokens
Display formula output text: about 180k-250k tokens
Display-only total: about 1.1M-1.3M tokens
```

All formula spans:

```text
Total formulas: 7703
Inline formulas: 4931
Display formulas: 2772
Estimated all-formula visual coverage: about 2.0M-3.0M tokens
```

Whole-page visual pass:

```text
Pages: 803
Estimated whole-page image input: about 2.1M image tokens
With output: about 2.3M-2.5M tokens
```

Recommendation: do not send the whole book to vision. Send only the candidate funnel first:

- 28 repair worklist items.
- 7 KaTeX warnings.
- 16 selfcheck misses.
- Any new hard errors in future books.

Expected cost for candidate-only review should usually be tens of thousands of tokens, not millions.

## Proposed Next Implementation Steps

1. Build a candidate aggregator:
   - Input: `render_errors.json`, `worklist.json`, `selfcheck.json`, page JSONs.
   - Output: stable JSONL with `candidate_id`, `page`, `block_id`, `bbox`, `kind`, `reason`, `engine_latex`, `crop_path`.
   - Deduplicate by `(page, block_id, kind)`.

2. Add crop generation/reuse:
   - Reuse existing `debug_repair` crop conventions where possible.
   - Keep crops under the repair work dir, not in git.

3. Add visual-verdict schema:
   - Store verdicts separately from corrections.
   - Suggested path: `<stem>_repair/visual_verdicts.jsonl`.
   - Do not overwrite `corrections.json` until accepted.

4. Add a dry-run command:
   - Produce candidate list and token estimate without calling a model.
   - This is the default safe mode.

5. Add model execution behind explicit flag:
   - Example: `--run-vision`.
   - Require owner approval before using it in this workspace.

6. Integrate with debug HTML:
   - Show candidate reason, crop, engine LaTeX, proposed corrected LaTeX, and verdict.
   - Keep accept/reject workflow.

7. Verification:
   - Unit tests for candidate aggregation and dedupe.
   - Fixture tests for warning/selfcheck/worklist inputs.
   - Full `scripts/pipelines/textbooks/tests`.
   - KaTeX scan before and after accepted corrections.

## Known Cautions

- Do not treat KaTeX warnings as automatic defects. Some are harmless strict-mode warnings.
- Do not auto-normalize nonnumeric formula tags such as `Ω` without deciding whether the stranded adjacent numeric tag should replace it.
- Do not use broad sanitizer rewrites for `\left`, `\right`, `array`, or scripts. Prior fixes are intentionally pattern-gated.
- Visual model corrections must be evidence-backed and reviewable; formulas are dense, and plausible LaTeX can still be visually wrong.

## Suggested Resume Prompt

Continue from `docs/handoff/2026-07-07-HANDOFF-textbooks-formula-vision-repair-next.md`.

Goal: design and implement the next formula visual repair pipeline for textbooks. Start with a dry-run candidate aggregator and token estimator. Do not run model/vision repair without explicit owner approval. Preserve the current deterministic KaTeX hard-error status: `7703 formulas / 0 hard errors / 7 warnings` for `Paul_Analysis_MTL_scan`.
