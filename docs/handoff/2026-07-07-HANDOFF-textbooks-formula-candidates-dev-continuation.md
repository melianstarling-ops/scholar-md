# HANDOFF: textbooks formula candidates review continuation

Date: 2026-07-07

## Read This First

The owner is currently running a conversion task. In the next session, do not move, delete, or rewrite output/process directories until the running conversion is confirmed finished.

Important constraints:

- Do not touch `02_Source/`.
- Do not change the earlier conversion pipeline structure unless the owner explicitly asks.
- Do not change `convert.py`, `paths.py`, batch output semantics, or the existing `--out` / `--work-dir` contract just to improve directory aesthetics.
- If a path is under OneDrive private mirror or reached through symlink/junction, resolve the real path and use elevated read/write as needed. Do not treat sandbox access denial as "file missing".
- Use `.venv-textbooks\Scripts\python.exe -X utf8` for textbooks Python commands.
- Do not run vision/model repair without explicit owner approval.

## Current Goal

Continue the formula OCR review workflow for `Paul_Analysis_MTL_scan`.

The current implemented direction is:

- Build a dry-run formula candidate aggregator.
- Estimate candidate review cost.
- Generate a debug HTML that lets the owner mark candidates whose OCR is correct.
- Treat unchecked candidates as needing repair in the next workflow step.

The owner does not want the previous conversion pipeline changed while a conversion is running.

## Current Output Layout

The Paul batch directory was manually flattened during this session because an extra `deliverables/` layer caused confusion.

Current workspace layout:

```text
03_Output/textbooks/paul_scan_full_20260706/
  Paul_Analysis_MTL_scan/
    Paul_Analysis_MTL_scan.md
    Paul_Analysis_MTL_scan.assets/
    Paul_Analysis_MTL_scan_conversion_report_2026-07-07.md

  _work_root/
    Paul_Analysis_MTL_scan/
      Paul_Analysis_MTL_scan_debug.html
      Paul_Analysis_MTL_scan_render_errors.json
      Paul_Analysis_MTL_scan_selfcheck.json
      Paul_Analysis_MTL_scan_repair/
        worklist.json
        formula_candidates.jsonl
        formula_candidates_summary.json
        crops/
```

Note: IDE open tabs may still point at the old path:

```text
03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/...
```

That path was removed/flattened for the existing Paul output. However, if the currently running conversion task still uses `--out .../deliverables`, it may recreate the `deliverables/` directory. Do not "fix" this while the conversion is active. First do a read-only inventory after conversion finishes.

## D Drive Process Root

An older external process root still existed during the last check:

```text
D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan
```

Do not delete it unless the owner explicitly confirms. It may still be useful as a recovery copy or may be referenced by the running task.

## Implemented Code Changes

Current dirty git status includes:

```text
 M scripts/pipelines/textbooks/README.md
 M scripts/pipelines/textbooks/debug_assets/app.css
 M scripts/pipelines/textbooks/debug_assets/app.js
 M scripts/pipelines/textbooks/debug_payload.py
 M scripts/pipelines/textbooks/debug_view.py
 M scripts/pipelines/textbooks/paths.py
 M scripts/pipelines/textbooks/tests/test_debug_payload.py
 M scripts/pipelines/textbooks/tests/test_debug_view.py
 M scripts/pipelines/textbooks/tests/test_paths.py
?? docs/superpowers/specs/2026-07-07-textbooks-formula-candidates-design.md
?? docs/temp/
?? scripts/pipelines/textbooks/formula_candidates.py
?? scripts/pipelines/textbooks/tests/test_formula_candidates.py
```

`docs/temp/` contains owner/reviewer temporary review files. Do not delete or rewrite it unless asked.

Main feature pieces:

- `formula_candidates.py` aggregates formula candidates from worklist, KaTeX warnings/errors, page OCR JSON, and selfcheck unresolved summary.
- Candidate output:
  - `<repair_dir>/formula_candidates.jsonl`
  - `<repair_dir>/formula_candidates_summary.json`
- `debug_view.py` and `debug_payload.py` load formula candidates into the debug payload.
- `debug_assets/app.js` shows candidate cards and an `OCR 正确` checkbox.
- Candidate checkbox state is stored in browser `localStorage` with key pattern `tbdbgcand:<stem>`.
- Export JSON includes `candidate_reviews`, where unchecked defaults to `needs_repair` and checked becomes `ocr_correct`.

Known candidate dry-run result for Paul:

```text
deduped candidates: 39
raw reason hits: 49
worklist candidates: 28
KaTeX warning-derived candidates: 12
bbox_proxy candidates: 11
crop-backed candidates: 28
selfcheck unresolved count: 16
```

## Verification Already Done

Earlier in this session, the full textbooks tests passed after the formula candidate/debug UI changes:

```text
321 passed in 2.92s
```

Because output directories and `AGENTS.md` were later changed, rerun verification before claiming final completion:

```powershell
.venv-textbooks\Scripts\python.exe -X utf8 -m pytest scripts/pipelines/textbooks/tests/
```

Do not run this while the owner's conversion task is actively using the same outputs if there is any risk of file contention.

## AGENTS.md Update

`AGENTS.md` was updated in its OneDrive real path to add this rule:

```text
OneDrive private mirror / symlink / junction paths should be resolved to their real path, and elevated access should be used for required read/write/move operations instead of repeatedly failing in the normal sandbox.
```

This was added because private `03_Output/` and root `AGENTS.md` can be OneDrive-backed and may need elevated read/write even when the path appears inside the workspace.

## Important Mistakes To Avoid

- Do not create temporary output folders outside the workspace without explicit owner approval.
- Do not copy D-drive process artifacts into the final Markdown/assets directory.
- Do not move process artifacts while conversion is running.
- Do not restructure the earlier conversion pipeline or rename its public concepts unless the owner asks for a separate design pass.
- Do not assume `deliverables/` is wrong globally. In this session, the owner objected to extra nesting for the current task. The safe next move is to observe the active conversion's actual output path before changing anything else.

## Recommended Next Session Flow

1. Start with a read-only status check.

```powershell
git status --short
Get-ChildItem -LiteralPath "03_Output\textbooks\paul_scan_full_20260706" -Force
```

Use elevated access for OneDrive-backed private output paths if needed.

2. Ask/confirm whether the active conversion task has finished.

Do not touch output directories until it has finished.

3. If conversion is finished, inventory where new files landed.

Look for whether `deliverables/` was recreated and whether `_work_root/` has changed.

4. Regenerate or locate the current debug HTML for candidate review.

Expected current debug HTML after flattening:

```text
03_Output/textbooks/paul_scan_full_20260706/_work_root/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_debug.html
```

If a new conversion recreated old layout, the debug HTML may instead be under:

```text
03_Output/textbooks/paul_scan_full_20260706/deliverables/_work_root/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_debug.html
```

5. Owner reviews the debug HTML.

Owner should check `OCR 正确` only for formula candidates that are actually correct. Unchecked candidates are treated as needing repair.

6. Next implementation step after owner review:

Create a deterministic importer for exported `candidate_reviews` that produces a repair input list:

- checked `ocr_correct` -> exclude from repair
- unchecked/missing `needs_repair` -> include for repair
- preserve `candidate_id`, `page`, `block_id`, bbox/crop path, reasons, and current LaTeX

Do this with tests first, but do not call any model.

## Suggested Handoff Summary For The Next Agent

We are continuing from a partially completed formula candidate review feature. The candidate aggregator and debug checkbox UI exist, but owner review and downstream repair-import are not finished. The owner is currently running a conversion, so begin read-only. Do not alter conversion pipeline semantics or output folders until conversion finishes and owner confirms. The current Paul output was flattened to remove an extra `deliverables/` nesting, but a running conversion may recreate it depending on its `--out` argument.
