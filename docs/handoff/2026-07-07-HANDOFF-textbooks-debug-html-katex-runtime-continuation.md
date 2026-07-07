# HANDOFF: textbooks debug HTML KaTeX runtime issue continuation

Date: 2026-07-07

## Context

Current task: continue debugging a mismatch where the final Markdown previews correctly in VS Code, but the generated debug HTML is reported by the user to still display broken inline formulas on page 691.

User preference:

- Keep KaTeX. Do not switch the main audit path to MathJax.
- Debug HTML should match the practical reading experience closely enough to avoid false formula alarms.
- Continue from evidence, not guesses.

Important workspace rules:

- New session must first read `01_System/SOP-03_Conversion_SelfCheck.md`.
- Do not modify anything under `02_Source/`.
- Use `.venv-textbooks\Scripts\python.exe -X utf8` for Python commands in this textbooks workflow.
- Do not run `vision_repair` or visual/model repair without explicit user approval.

## Key Paths

- Repo/workspace: `G:\Projects\Project_scholar-md`
- Source PDF, read-only: `02_Source/textbooks_samples/Paul_Analysis_MTL_scan.pdf`
- Deliverables dir: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/`
- Final Markdown: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan.md`
- Deliverable debug HTML: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_debug.html`
- Work debug HTML: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html`
- Render errors JSON: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_render_errors.json`
- Repair worklist: `D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_repair\worklist.json`
- Conversion report: `03_Output/textbooks/paul_scan_full_20260706/deliverables/Paul_Analysis_MTL_scan/Paul_Analysis_MTL_scan_conversion_report_2026-07-07.md`

## What Changed In This Session

This was not only a test-file change. The session changed pipeline/debug code and tests:

- `scripts/pipelines/textbooks/reconstruct.py`
  - Added deterministic KaTeX-oriented LaTeX cleanup:
    - `\boldmath` stripping.
    - known HTML entity decoding in math.
    - duplicate embedded `\tag{...}` stripping when adjacent `formula_number` exists.
    - double superscript handling.
    - unmatched top-level `}` removal.
    - literal unescaped `$` removal inside extracted formula bodies.
    - empty `\left.` / `\right.` delimiter cleanup for specific OCR damage.
    - narrow p477 moment-row repair.
  - Applies math-span sanitization in text/table/footnote/figure-title fragments.
- `scripts/pipelines/textbooks/debug_assets/app.js`
  - Added safe raw `<table>` rendering while keeping global `markdown-it({ html: false })`.
  - Added `normalizeInlineMathPadding()` for `$ x $` style inline formulas before rendering in debug HTML.
  - Routes page problem scan and fragment rendering through `renderMarkdownFragment()`.
- Tests:
  - `scripts/pipelines/textbooks/tests/test_reconstruct.py`
  - `scripts/pipelines/textbooks/tests/test_debug_view.py`
- Output artifacts refreshed:
  - final `.md`
  - debug HTML
  - render errors JSON
  - repair worklist
  - conversion report

## Current Verified State

Verified commands/results from this session:

```powershell
.\.venv-textbooks\Scripts\python.exe -X utf8 -m pytest scripts/pipelines/textbooks/tests -q
```

Result:

```text
302 passed in 2.42s
```

```powershell
node --check scripts/pipelines/textbooks/debug_assets/app.js
```

Result: exit code 0.

Official KaTeX scan after deterministic fixes:

```text
total formulas: 7703
hard errors: 9
warnings: 11
remaining hard-error pages: 54, 230, 532, 681, 699, 699, 728, 728, 746
```

The pages the user previously called out as broken (`361`, `477`, `656`) are no longer KaTeX hard errors in the official scan.

Repair worklist after refresh:

```text
35 suspicious formula items
```

Generated/copy-refreshed debug HTML:

```text
G:\Projects\Project_scholar-md\03_Output\textbooks\paul_scan_full_20260706\deliverables\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html
Length: 159651318
LastWriteTime: 2026/7/6 23:45:36
```

## Current User-Reported Unresolved Issue

User reports page 691 in debug HTML still renders this passage badly:

```text
then (12.76) represent $n$uncoupled sets of two-conductor lines, each with incident field excitation through elements of the vectors$\mathbf{V}{\mathrm{Fm}}(z,t)
a
n
d
and\mathbf{I}{\mathrm{Fm}}(z,t)$. Once ...
```

However, local disk evidence from the current deliverable files shows normal content.

Verified final Markdown contains:

```text
then (12.76) represent $n$ uncoupled sets of two-conductor lines, each with incident field excitation through elements of the vectors $\mathbf{V}_{\mathrm{Fm}}(z,t)$ and $\mathbf{I}_{\mathrm{Fm}}(z,t)$. Once ...
```

Verified current deliverable debug HTML contains the same normal embedded payload:

```text
good_present True
bad_index -1
... vectors $\\mathbf{V}_{\\mathrm{Fm}}(z,t)$ and $\\mathbf{I}_{\\mathrm{Fm}}(z,t)$ ...
```

The same check was also done against the D: work debug HTML; it also contains the normal payload and does not contain `vectors$\\mathbf{V}`.

## Strongly Suspected But Not Yet Proven

- [待确认] The user may still be viewing an older static HTML instance, browser cache, or a different file path.
- [待确认] VS Code Simple Browser / external browser may have cached the huge static HTML/JS aggressively.
- [待确认] There may be a runtime rendering path in the browser that differs from the source payload checks, but this has not been reproduced locally yet.

Do not assume OCR or Markdown reconstruction is still wrong until browser DOM/runtime evidence shows the current HTML payload is being rendered incorrectly.

## Evidence Commands To Re-run First

Use these early in the next session.

Check page 691 reconstructed fragment directly from D: work JSON:

```powershell
.\.venv-textbooks\Scripts\python.exe -X utf8 -c "import json,pathlib; from scripts.pipelines.textbooks.debug_payload import build_page_payload; base=pathlib.Path(r'D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\_work'); p=691; data=json.load(open(base/f'page_{p:04d}_res.json',encoding='utf-8')); payload=build_page_payload(data,p,'Paul_Analysis_MTL_scan'); [print('BIDS',f['bids'],'\n'+f['md']) for f in payload['frags'] if 'uncoupled sets' in f['md']]"
```

Check current deliverable debug HTML payload:

```powershell
.\.venv-textbooks\Scripts\python.exe -X utf8 -c "from pathlib import Path; p=Path(r'03_Output\textbooks\paul_scan_full_20260706\deliverables\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html'); s=p.read_text(encoding='utf-8'); good='vectors $\\\\mathbf{V}_{\\\\mathrm{Fm}}(z,t)$ and $\\\\mathbf{I}_{\\\\mathrm{Fm}}(z,t)$'; bad='vectors$\\\\mathbf{V}'; print('good_present', good in s); print('bad_index', s.find(bad)); i=s.find(good); print(s[i-100:i+240] if i!=-1 else 'not found')"
```

Expected current result:

```text
good_present True
bad_index -1
```

Compare file timestamps:

```powershell
Get-Item "D:\scholar-md-work\paul_scan_full_20260706\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html", "03_Output\textbooks\paul_scan_full_20260706\deliverables\Paul_Analysis_MTL_scan\Paul_Analysis_MTL_scan_debug.html" | Format-List FullName,Length,LastWriteTime
```

Expected current result:

```text
Length: 159651318
LastWriteTime: 2026/7/6 23:45:36
```

## Recommended Next Debug Steps

1. Ask the user for the exact path they opened, or have them screenshot the browser address/path.
2. If feasible, reproduce with a browser/DOM inspection instead of source grep:
   - open the current `Paul_Analysis_MTL_scan_debug.html`;
   - navigate to page 691;
   - inspect the `.mdblk` for block id `4`;
   - compare `textContent` and `innerHTML`.
3. If browser automation is available, use Playwright or a local Chromium launch to:
   - load the `file:///G:/Projects/.../Paul_Analysis_MTL_scan_debug.html`;
   - set page input to `691`;
   - query `#mdOut .mdblk[data-bids~="4"]`;
   - screenshot the right pane.
4. If DOM rendering is wrong while `window.DEBUG_DATA` is right, focus on `scripts/pipelines/textbooks/debug_assets/app.js` and the vendored `markdown-it-katex.js` delimiter logic.
5. If DOM rendering is right locally, likely resolution is operational:
   - close the old Simple Browser/browser tab;
   - reopen the exact deliverable file path;
   - hard refresh;
   - avoid opening stale D: vs G: copies from earlier timestamps.

## Potential Code Direction If Runtime Really Fails

Keep KaTeX, but make debug HTML math tokenization closer to VS Code:

- Add a targeted regression test for the exact page 691 fragment.
- Consider replacing the current `normalizeInlineMathPadding()` with a more robust pre-render normalization that:
  - preserves final Markdown bytes;
  - affects debug HTML rendering only;
  - normalizes common OCR/markdown inline math delimiter adjacency before `markdown-it-katex` sees it.
- Do not patch the vendored `markdown-it-katex.js` blindly. First create a minimal failing case proving the plugin fails on the current page 691 string.

Minimal page 691 fragment to use for tests:

```text
then (12.76) represent $n$ uncoupled sets of two-conductor lines, each with incident field excitation through elements of the vectors $\mathbf{V}_{\mathrm{Fm}}(z,t)$ and $\mathbf{I}_{\mathrm{Fm}}(z,t)$. Once the solution to these uncoupled two-conductor lines has been obtained, we return to the original variables via the transformations given in (12.75).
```

## Current Git/Dirty State Caveat

The worktree already had unrelated/pre-existing dirty files before this handoff. Do not revert unrelated changes.

Observed dirty set during this session included:

```text
 M .gitignore
 M scripts/pipelines/textbooks/batch.py
 M scripts/pipelines/textbooks/debug_assets/app.css
 M scripts/pipelines/textbooks/debug_assets/app.js
 M scripts/pipelines/textbooks/debug_assets/scan_katex_errors.mjs
 M scripts/pipelines/textbooks/debug_repair.py
 M scripts/pipelines/textbooks/katex_scan.py
 M scripts/pipelines/textbooks/reconstruct.py
 M scripts/pipelines/textbooks/tests/test_batch.py
 M scripts/pipelines/textbooks/tests/test_debug_repair.py
 M scripts/pipelines/textbooks/tests/test_debug_view.py
 M scripts/pipelines/textbooks/tests/test_katex_scan.py
 M scripts/pipelines/textbooks/tests/test_reconstruct.py
 ?? docs/handoff/2026-07-06-HANDOFF-textbooks-katex-table-debug-continuation.md
```

This new handoff file is additive.

## Completion Boundary

This handoff does not claim the user-visible issue is solved. It records that:

- current source payload and Markdown are verified normal for page 691;
- user still reports bad debug HTML rendering;
- the next session should inspect browser runtime/DOM or stale-file path before changing reconstruction logic again.
