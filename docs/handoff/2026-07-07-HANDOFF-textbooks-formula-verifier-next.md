# HANDOFF: textbooks formula verifier next

Date: 2026-07-07

## Context

This handoff continues the `Paul_Analysis_MTL_scan` formula candidate workflow after the dry-run aggregator and debug HTML review UI were added.

Important constraints:

- Do not touch `02_Source/`.
- The owner reported the long-running conversion can be ignored for this task, but do not restructure pipeline I/O semantics while it runs.
- Use `.venv-textbooks\Scripts\python.exe -X utf8` for textbooks Python commands.
- The owner accepts a **high-recall candidate funnel**. Do not spend the next session trying to make heuristic rules perfectly precise.

## What Exists Now

Implemented and already in the dirty worktree:

- `formula_candidates.py` aggregates candidate formulas from:
  - debug-repair worklist heuristics
  - KaTeX errors / warnings
  - selfcheck unresolved summary
- `debug_view.py` / `debug_payload.py` pass formula candidates into debug HTML.
- `debug_assets/app.js` / `app.css` render candidate review cards with `OCR 正确` checkboxes.
- Export JSON now includes `candidate_reviews`.

Key exported review file:

- `03_Output/textbooks/paul_scan_full_20260706/_work_root/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_annotations.json`

## Current Review State

From the exported annotations JSON:

- `candidate_reviews = 39`
- `ocr_correct = 12`
- `needs_repair = 27`

However, manual `ocr_correct` marks were **not treated as final truth**. A follow-up read-only visual verification pass was run on the 12 marked-good candidates.

## Verified Result Of The 12 Marked-Good Candidates

Confirmed correct:

- `p0163-b0004`
- `p0192-b0012`
- `p0196-b0004`
- `p0199-b0003`
- `p0200-b0009`
- `p0218-b0015`
- `p0605-b0015`
- `p0610-b0005`

Should be reverted back to `needs_repair`:

- `p0230-b0006`
  - The long summed term should close with a visible right brace on the last line.
  - Current OCR LaTeX also contains malformed `ln` / `tan` tokenization.
- `p0253-b0009`
  - The first two integrals visibly carry lower limit `s_i`.
  - Current OCR LaTeX drops those integral bounds.
- `p0676-b0002`
  - The inner column-vector entry is structurally misparsed.
  - The image shows one integral from `a` to `a'` of `\hat{E}_t^{inc} \cdot dl`, but current OCR splits `a'` and `a` into separate rows and misuses `\overrightarrow{E}_t` as an upper limit.
- `p0698-b0005`
  - The first-line integrand field symbol has a hat on `E` in the image.
  - Current OCR LaTeX drops that hat.

Net effect after verifier override:

- final confirmed-correct count should become `8`
- final repair-needed count should become `31`

## Why Correct Formulas Are Still Flagged

This is expected under the current design.

The current detector is a **candidate recall funnel**, not a correctness judge:

1. `selfcheck.py` flags heuristic structural suspicions such as bare large operators and primed-denominator `\frac` shapes.
2. `debug_repair.py` converts those suspicions plus KaTeX hard errors into crop-backed worklist items.
3. `formula_candidates.py` unions worklist reasons with KaTeX warnings / errors and unresolved selfcheck summaries.

Therefore a candidate can be:

- syntactically legal
- visually correct
- still flagged as suspicious

This is acceptable for the owner’s preferred operating point.

## Recommendation For The Next Session

Do **not** spend the next session primarily tuning heuristic formula rules.

Instead, continue with this sequence:

1. Build a deterministic importer for `candidate_reviews`.
2. Add a second input channel for verifier overrides.
   - Suggested schema: stable per-candidate verdict records, separate from human checkbox export.
3. Produce a final repair input list:
   - human/verifier confirmed correct -> exclude
   - unresolved or verifier-rejected -> include
4. Focus future effort on:
   - visual verifier
   - then visual corrector

Rules should still be kept for high recall and prioritization, but they are no longer the main place to chase truth.

## Suggested Minimal Next Implementation

Create a deterministic importer that reads:

- `Paul_Analysis_MTL_scan_annotations.json`
- `formula_candidates.jsonl`
- optional verifier verdict file

and emits a final repair list preserving:

- `candidate_id`
- `page`
- `block_id`
- `bbox`
- `crop_path`
- `reasons`
- `engine_latex`
- final decision source (`human`, `verifier`, or fallback default)

Suggested logic:

- `ocr_correct` + no verifier rejection -> exclude
- verifier `likely_wrong` -> include
- `needs_repair` -> include
- missing review -> include

## Verification State

Earlier in the session, textbooks tests were reported green after the formula candidate / debug UI work:

- `321 passed in 2.92s`

Because more workflow state changed afterward and this handoff adds only docs, rerun fresh verification before claiming code completion in the next session:

```powershell
.venv-textbooks\Scripts\python.exe -X utf8 -m pytest scripts/pipelines/textbooks/tests/
```

## Files Worth Opening First Next Session

- `scripts/pipelines/textbooks/formula_candidates.py`
- `scripts/pipelines/textbooks/debug_repair.py`
- `scripts/pipelines/textbooks/selfcheck.py`
- `03_Output/textbooks/paul_scan_full_20260706/_work_root/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_annotations.json`
- `docs/handoff/2026-07-07-HANDOFF-textbooks-formula-candidates-dev-continuation.md`
- `docs/handoff/2026-07-07-HANDOFF-textbooks-formula-vision-repair-next.md`

## One-Sentence Resume Prompt

Continue from `docs/handoff/2026-07-07-HANDOFF-textbooks-formula-verifier-next.md`; keep the high-recall candidate funnel, do not over-optimize heuristic rules, and implement the deterministic importer plus verifier-aware final repair list before moving on to visual correction.
