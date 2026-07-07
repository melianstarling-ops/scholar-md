# 2026-07-06 HANDOFF: textbooks KaTeX/Table Debug Continuation

## Current User Intent

Owner wants to continue with deterministic/script-level fixes before any visual-agent OCR repair.

Do **not** run `vision_repair` yet. That stage sends cropped formula images to external/model agents and should wait until:

1. deterministic KaTeX/rendering fixes are exhausted;
2. remaining cases are confirmed to require visual/OCR judgment;
3. owner explicitly approves the specific external agent/backend/model and image set.

Owner specifically observed:

- tables are not displayed normally, example PDF/page 210;
- KaTeX errors still remain;
- pages 361 and 697 still appear to contain formulas that do not render normally in the debug HTML;
- next session should continue fixing formula rendering problems that can be fixed by scripts.

## Important Paths

- Source PDF: `02_Source/textbooks_samples/Paul_Analysis_MTL_scan.pdf`
- Full deliverables: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/`
- Work root: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan`
- Latest debug HTML copied to deliverables:
  `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_debug.html`
- Latest work-side debug HTML:
  `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html`
- Latest render errors:
  `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_render_errors.json`
- Latest repair worklist:
  `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_repair\worklist.json`

## Code Changes Already Made In This Session

These changes are in the working tree and are not yet committed.

### 1. Precise page/block mapping for KaTeX scan

Files:

- `scripts/pipelines/textbooks/katex_scan.py`
- `scripts/pipelines/textbooks/debug_assets/scan_katex_errors.mjs`
- `scripts/pipelines/textbooks/debug_repair.py`
- `scripts/pipelines/textbooks/batch.py`
- tests in `scripts/pipelines/textbooks/tests/`

Behavior:

- `katex_scan.py` can now scan from `_work/page_NNNN_res.json` via `scan_katex_work_pages(...)`.
- The scanner preserves `page`, `block_ids`, and `formula_number`.
- `debug_repair.py` prefers exact `block_ids` over old fuzzy `latex_head` matching.
- `batch.py` now uses work-page scanning for post-conversion KaTeX scan.

This fixed the earlier issue where all render errors had `page=null` and page 106 formula `(2.45a)` could not be mapped back to a crop.

### 2. False-red fix for display formulas

File:

- `scripts/pipelines/textbooks/debug_assets/scan_katex_errors.mjs`

Root cause:

- The old JS extractor used simple `indexOf('$')` for inline math.
- A malformed/unclosed single `$` could consume the first `$` of a later `$$...$$`, leaving the second `$` to start fake inline math.
- Display formulas with `\tag{...}` were then rendered as inline, causing many false errors: `\tag works only in display equations`.

Fix:

- display `$$...$$` and inline `$...$` are now scanned by separate matching logic.
- inline math cannot close on either half of a `$$` delimiter.
- inline scanning stops at page comments, blank-block boundaries, and Markdown image starts to avoid cross-block/page pollution.
- metadata output (`page`, `block_ids`, `formula_number`) is preserved.

### 3. Deterministic double-superscript cleanup

File:

- `scripts/pipelines/textbooks/reconstruct.py`

Behavior:

- Added narrow braced-adjacent superscript cleanup:
  `^{A}^{B}` -> `^{A\ B}`
  and specifically `\omega^{\prime}^{2}` -> `\omega^{\prime 2}`.
- It does not alter legal `x'^{2}`.

This removed the page 106 `(2.45a)` hard error.

### 4. TODO entry added

`TODO.md` now includes:

```markdown
- [ ] **公式视觉识别修复多 agent/多模型后端**：支持 Claude、Codex、Kimi、Antigravity CLI；可按裁图指定后端和具体模型（如 Codex 使用 GPT-5.3）；裁图外传与修正采纳均必须先经人类确认。
```

## Latest Verification

Command:

```powershell
.\.venv-textbooks\Scripts\python.exe -X utf8 -m pytest scripts/pipelines/textbooks/tests -q
```

Result:

```text
288 passed in 2.27s
```

Latest rebuild commands already run:

```powershell
.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.debug_view `
  --out "03_Output\textbooks\paul_scan_full_20260706\deliverables" `
  --work-dir "D:\scholar-md-work\paul_scan_full_20260706" `
  --stem "Paul_Analysis_MTL_scan" `
  --reassemble

.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.katex_scan `
  --deliverables-root "03_Output\textbooks\paul_scan_full_20260706\deliverables" `
  --work-dir "D:\scholar-md-work\paul_scan_full_20260706" `
  --stem "Paul_Analysis_MTL_scan" `
  --out "D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_render_errors.json"

.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.debug_repair `
  --out "03_Output\textbooks\paul_scan_full_20260706\deliverables" `
  --work-dir "D:\scholar-md-work\paul_scan_full_20260706" `
  --stem "Paul_Analysis_MTL_scan" `
  --repair-dpi 300 `
  --pad 12

.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.debug_view `
  --out "03_Output\textbooks\paul_scan_full_20260706\deliverables" `
  --work-dir "D:\scholar-md-work\paul_scan_full_20260706" `
  --stem "Paul_Analysis_MTL_scan"
```

Latest results:

- KaTeX hard errors: `654 -> 50`
- KaTeX warnings: `58 -> 12`
- scanned formulas after fixed extractor: `7703`
- repair worklist count: `66`
- page 106 hard errors: `[]`
- page 106 worklist items: `[]`
- generated debug HTML: 803 pages embedded, 50 marked KaTeX errors, about 152 MB

## Latest Remaining KaTeX Error Summary

From latest `Paul_Analysis_MTL_scan_render_errors.json`:

```text
total formulas: 7703
hard errors: 50
warnings: 12
```

Top error categories:

- `Multiple \tag`: 22
- HTML entity parsed inside math, e.g. `&lt;`, `&gt;`, `&#x27;`: 9+
- `Undefined \boldmath`: 4
- `Expected 'EOF' got '}'`: 3
- `Expected '\right' got '\end'`: 2
- `Expected token`: 2
- singletons: unexpected end of input, literal `$` in math, `Undefined \bmatrix`, double subscript, etc.

Top pages by hard-error count:

- page 383: 8
- pages 448, 521, 646, 699, 707, 728: 2 each
- many single-error pages: 54, 125, 195, 199, 210, 230, 263, 398, 447, 458, 477, 511, 522, ...

Important page-number note:

- Owner reported pages 361 and 697 still visually problematic.
- Latest headless KaTeX scan has `page361_errors=[]` and `page697_errors=[]`.
- This is not enough to dismiss the report. New session should verify in the latest HTML whether:
  1. owner was viewing an older debug HTML;
  2. the debug UI page index differs from PDF/logical page number;
  3. formulas render but layout is visually broken, which current hard-error scan does not catch;
  4. nearby pages now carry the errors, e.g. latest scan has page 699 errors.

## Table Problem: Page 210

User observed tables do not display normally, example PDF page 210.

Inspection of `page_0210_res.json`:

- block 3 label `table`, order `None`
- content is raw HTML table:

```html
<table><tr><td>Entry</td><td>With dielectric (pF/m) C</td><td>Without dielectric (pF/m)  $ C_0 $</td><td>Effective dielectric constant,  $ \varepsilon&#x27;_{r} $</td></tr>...</table>
```

- block 5 is another raw HTML table.
- debug app currently creates markdown-it as:

```js
window.markdownit({ html: false, linkify: true, breaks: false })
```

Likely table issue:

- raw `<table>` is escaped/not rendered because `html: false`;
- formulas inside raw HTML table cells may not be processed by `markdown-it-katex` in the intended way;
- formulas inside table cells can contain HTML entities such as `&#x27;`, producing KaTeX errors (`\varepsilon&#x27;_{r}`).

Recommended next-session table work:

1. Add tests for table rendering expectations.
2. Decide whether to:
   - enable safe HTML rendering in debug only, or
   - convert raw HTML tables to Markdown tables / structured HTML after math sanitization.
3. Add a deterministic entity decode step for math text before KaTeX:
   - `&#x27;` -> `'`
   - `&lt;` -> `<`
   - `&gt;` -> `>`
   - likely needed both for scanner and final rendered markdown fragments.
4. Re-run KaTeX scan and debug HTML generation.

## Recommended Next Steps

### Step 1: Reproduce owner-visible issues in latest HTML

Open latest deliverable HTML, not an older tab:

```text
03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_debug.html
```

Check:

- page 210 tables;
- owner-reported pages 361 and 697;
- latest hard-error pages: 383, 448, 521, 646, 699, 707, 728.

### Step 2: Fix HTML entities inside math

Candidate deterministic cleanup:

- Decode only known safe entities inside LaTeX/math fragments:
  - `&#x27;` / `&#39;` -> `'`
  - `&lt;` -> `<`
  - `&gt;` -> `>`
  - `&amp;` should be handled carefully because `&` is also LaTeX alignment syntax.

Likely affected current errors:

- page 210: `\varepsilon&#x27;_{r}`
- page 383: `R_{S}&lt;Z_{C}`, `R_{L}&gt;Z_{C}`, etc.

### Step 3: Fix table rendering path

Current raw table HTML is passed through as a `table` block, but debug Markdown renderer has `html: false`.

Options:

- Debug-only: allow a constrained/sanitized HTML rendering path for table fragments.
- Pipeline-level: convert raw HTML tables to Markdown tables, preserving inline math.
- Hybrid: render raw table HTML in debug view but keep final Markdown unchanged until a safer table conversion is implemented.

Do not silently enable arbitrary HTML without thinking through safety and rendering parity.

### Step 4: Continue deterministic formula fixes

Candidates from current error categories:

- `Undefined \boldmath`: likely replace/drop within `\mathrm{...}` or convert to `\mathbf`/text-safe equivalent if narrow pattern is proven.
- `Undefined \bmatrix`: likely engine emitted bare `\bmatrix`; should be `\begin{bmatrix}...\end{bmatrix}` only if context supports it. Needs evidence before fixing.
- `Double subscript`: existing double-subscript cleaner did not catch one page 681 case; inspect exact block.
- `Multiple \tag`: likely formula body contains tags in addition to absorbed formula_number, or scanner sees multiple equations in one block. Needs inspect pages around 447/448/383.
- `Expected '\right' got '\end'`, missing braces, unexpected EOF: likely content-level OCR damage; many should remain manual/visual.

### Step 5: Regenerate artifacts

After each deterministic fix:

```powershell
.\.venv-textbooks\Scripts\python.exe -X utf8 -m pytest scripts/pipelines/textbooks/tests -q
```

Then regenerate:

```powershell
.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.debug_view --out "03_Output\textbooks\paul_scan_full_20260706\deliverables" --work-dir "D:\scholar-md-work\paul_scan_full_20260706" --stem "Paul_Analysis_MTL_scan" --reassemble

.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.katex_scan --deliverables-root "03_Output\textbooks\paul_scan_full_20260706\deliverables" --work-dir "D:\scholar-md-work\paul_scan_full_20260706" --stem "Paul_Analysis_MTL_scan" --out "D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_render_errors.json"

.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.debug_repair --out "03_Output\textbooks\paul_scan_full_20260706\deliverables" --work-dir "D:\scholar-md-work\paul_scan_full_20260706" --stem "Paul_Analysis_MTL_scan" --repair-dpi 300 --pad 12

.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.debug_view --out "03_Output\textbooks\paul_scan_full_20260706\deliverables" --work-dir "D:\scholar-md-work\paul_scan_full_20260706" --stem "Paul_Analysis_MTL_scan"
```

Copy latest debug HTML to deliverables if needed:

```powershell
Copy-Item -LiteralPath "D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html" -Destination "G:\Projects\Project_scholar-md\03_Output\textbooks\paul_scan_full_20260706\deliverables\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html" -Force
```

## Current Git State Notes

Working tree has uncommitted changes from this session:

- `scripts/pipelines/textbooks/batch.py`
- `scripts/pipelines/textbooks/debug_assets/scan_katex_errors.mjs`
- `scripts/pipelines/textbooks/debug_repair.py`
- `scripts/pipelines/textbooks/katex_scan.py`
- `scripts/pipelines/textbooks/reconstruct.py`
- tests in `scripts/pipelines/textbooks/tests/`
- this handoff document
- private output/report files under `03_Output/...`
- `TODO.md` private OneDrive file was updated

There is also an unrelated `.gitignore` modification already present in the worktree. Do not revert or stage it unless owner explicitly asks.

## Safety / Process Reminders

- Always use `.venv-textbooks\Scripts\python.exe -X utf8`, not system Python.
- Do not modify anything under `02_Source/`.
- Do not run `vision_repair` or external visual agents until owner explicitly approves the exact external data flow.
- If committing code, run SOP-06 lesson capture backstop first.
- Keep public code changes and private output/docs separate; `03_Output/`, `04_Docs/`, `TODO.md`, and handoff docs are private.

