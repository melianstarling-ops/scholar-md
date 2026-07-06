# textbooks 管线 I/O 重构 + 模块化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 textbooks 转换管线产物拆成「交付根 / 过程根」双目录,路径逻辑收口到单一 `paths.py`,KaTeX 报错扫描接入为一等 CLI 积木,更新 README。

**Architecture:** 新增 `paths.py` 的 `DocLayout`/`resolve_layout` 作为唯一路径真相源;各 stage 的**编排层函数**改吃 `DocLayout`(内部纯函数多已接收显式路径,不动);新增 `katex_scan.py` 薄壳 CLI 并由 batch 默认接入。设计见 `docs/superpowers/specs/2026-07-06-textbooks-io-restructure-design.md`。

**Tech Stack:** Python 3(`.venv-textbooks`)、dataclasses、pytest、subprocess、Node(仅 katex_scan 外调)。

## Global Constraints

- **范围红线**:只改 I/O 布局 + 路径模块化 + katex 接入 + README。**不动** `vision_repair.py` 的 AI 调用逻辑(`call_*_vision*`/prompt),**只穿它读写路径**。不动引擎/重组/自检算法。
- **人工确认门红线不变**:corrections 的 `status` 语义不动。
- **`02_Source/` 只读**;`03_Output/` 是 gitignore 的 OneDrive symlink,产物不入版本库。
- **测试基线**:`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/` 改造后仍须全绿。
- **UTF-8**:读写 `.md`/`.json`/`.py` 一律 `encoding="utf-8"`。
- **迁移已完成**(上一会话):旧产物已在双根布局,`work_root` 默认 = `<out>/_work_root`。新代码路径推导必须与已迁移落点一致。
- **文本文件编码**:Windows 上跑测试用 `.venv-textbooks/Scripts/python.exe`;涉及中文内容读写优先 Git Bash。

## 路径契约(全 Task 共用,来自 `DocLayout`)

`resolve_layout(stem, deliverables_root, work_root=None)`,`work_root` 缺省 `<deliverables_root>/_work_root`:

| property | 路径 | 侧 |
|---|---|---|
| `doc_deliverable_dir` | `<deliverables>/<stem>` | 交付 |
| `md_path` | `<deliverables>/<stem>/<stem>.md` | 交付 |
| `assets_dir` | `<deliverables>/<stem>/<stem>.assets` | 交付 |
| `doc_work_dir` | `<work>/<stem>` | 过程 |
| `work_dir` | `<work>/<stem>/_work` | 过程 |
| `repair_dir` | `<work>/<stem>/<stem>_repair` | 过程 |
| `worklist_path` | `<work>/<stem>/<stem>_repair/worklist.json` | 过程 |
| `render_errors_path` | `<work>/<stem>/<stem>_render_errors.json` | 过程 |
| `corrections_path` | `<work>/<stem>/<stem>_corrections.json` | 过程 |
| `selfcheck_path` | `<work>/<stem>/<stem>_selfcheck.json` | 过程 |
| `debug_html_path` | `<work>/<stem>/<stem>_debug.html` | 过程 |

---

## Task 1: `paths.py` —— DocLayout + resolve_layout(走 TDD)

**Files:**
- Create: `scripts/pipelines/textbooks/paths.py`
- Create: `scripts/pipelines/textbooks/tests/test_paths.py`

**Interfaces:**
- Produces: `DocLayout`(frozen dataclass,含上表所有 property)、`resolve_layout(stem, deliverables_root, work_root=None) -> DocLayout`。全 Task 消费。

- [ ] **Step 1: 写失败测试**

`tests/test_paths.py`:

```python
import os
from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout


def test_work_root_defaults_to_work_root_subdir_of_deliverables():
    lay = resolve_layout("Book", "/out")
    assert lay.work_root == os.path.join("/out", "_work_root")


def test_explicit_work_root_overrides_default():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.work_root == "/scratch"


def test_deliverable_side_paths():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.doc_deliverable_dir == os.path.join("/out", "Book")
    assert lay.md_path == os.path.join("/out", "Book", "Book.md")
    assert lay.assets_dir == os.path.join("/out", "Book", "Book.assets")


def test_process_side_paths():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.doc_work_dir == os.path.join("/scratch", "Book")
    assert lay.work_dir == os.path.join("/scratch", "Book", "_work")
    assert lay.repair_dir == os.path.join("/scratch", "Book", "Book_repair")
    assert lay.worklist_path == os.path.join(
        "/scratch", "Book", "Book_repair", "worklist.json")
    assert lay.render_errors_path == os.path.join(
        "/scratch", "Book", "Book_render_errors.json")
    assert lay.corrections_path == os.path.join(
        "/scratch", "Book", "Book_corrections.json")
    assert lay.selfcheck_path == os.path.join(
        "/scratch", "Book", "Book_selfcheck.json")
    assert lay.debug_html_path == os.path.join(
        "/scratch", "Book", "Book_debug.html")


def test_debug_html_is_process_side_not_deliverable():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.debug_html_path.startswith(os.path.join("/scratch", "Book"))
    assert "/out" not in lay.debug_html_path.replace("\\", "/")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_paths.py -v`
Expected: FAIL,`ModuleNotFoundError: ...paths`。

- [ ] **Step 3: 实现 `paths.py`**

```python
"""管线产物路径的单一真相源:交付根(md+assets)/ 过程根(_work/修复/报错/自检)双目录。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DocLayout:
    stem: str
    deliverables_root: str
    work_root: str

    # 交付侧 --------------------------------------------------------------
    @property
    def doc_deliverable_dir(self) -> str:
        return os.path.join(self.deliverables_root, self.stem)

    @property
    def md_path(self) -> str:
        return os.path.join(self.doc_deliverable_dir, f"{self.stem}.md")

    @property
    def assets_dir(self) -> str:
        return os.path.join(self.doc_deliverable_dir, f"{self.stem}.assets")

    # 过程侧 --------------------------------------------------------------
    @property
    def doc_work_dir(self) -> str:
        return os.path.join(self.work_root, self.stem)

    @property
    def work_dir(self) -> str:
        return os.path.join(self.doc_work_dir, "_work")

    @property
    def repair_dir(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_repair")

    @property
    def worklist_path(self) -> str:
        return os.path.join(self.repair_dir, "worklist.json")

    @property
    def render_errors_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_render_errors.json")

    @property
    def corrections_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_corrections.json")

    @property
    def selfcheck_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_selfcheck.json")

    @property
    def debug_html_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_debug.html")


def resolve_layout(stem: str, deliverables_root: str,
                   work_root: str | None = None) -> DocLayout:
    """work_root 缺省 = <deliverables_root>/_work_root(交付根下的显眼子树,好 gitignore/好删)。"""
    wr = work_root or os.path.join(deliverables_root, "_work_root")
    return DocLayout(stem=stem, deliverables_root=deliverables_root, work_root=wr)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_paths.py -v`
Expected: 5 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/paths.py scripts/pipelines/textbooks/tests/test_paths.py
git commit -m "feat(textbooks): 加 paths.py 双根布局单一真相源(DocLayout)"
```

---

## Task 2: convert.py —— convert_pdf + reassemble_md 吃双根

> 机械改签名,不新套 test-first;做法=改签名 + 重定向现有 test_convert + 一次真实端到端。

**Files:**
- Modify: `scripts/pipelines/textbooks/convert.py`(`convert_pdf`、`reassemble_md`、`main`)
- Modify: `scripts/pipelines/textbooks/tests/test_convert.py`(重定向到双根)

**Interfaces:**
- Consumes: `resolve_layout`(Task 1)。
- Produces: `convert_pdf(pdf_path, deliverables_dir=None, work_dir=None, dpi=cp.DEFAULT_DPI, write_selfcheck=True) -> dict`;`reassemble_md(layout, pdf_path, dpi) -> str | None`。

- [ ] **Step 1: 改 `convert_pdf` 用 DocLayout**

将现签名 `convert_pdf(pdf_path, out_dir=None, dpi=..., write_selfcheck=True)` 改为:

```python
def convert_pdf(pdf_path: str, deliverables_dir: str | None = None,
                work_dir: str | None = None, dpi: int = cp.DEFAULT_DPI,
                write_selfcheck: bool = True) -> dict:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    deliverables_dir = deliverables_dir or os.path.dirname(os.path.abspath(pdf_path))
    layout = resolve_layout(stem, deliverables_dir, work_dir)
    route = triage(pdf_path)
    if route == "B":
        return _register_deferred(pdf_path, deliverables_dir, stem)
    work_dir_ = layout.work_dir          # 原 os.path.join(doc_out, "_work")
    assets_dir = layout.assets_dir       # 原 os.path.join(doc_out, stem + ".assets")
    ...
```

把函数体内原先从 `doc_out` 派生的 `work_dir`/`assets_dir`/`md_path`/`selfcheck_path` 全部换成 `layout.*`:
- `md_path` → `layout.md_path`(写前 `os.makedirs(layout.doc_deliverable_dir, exist_ok=True)`)
- `selfcheck_path` → `layout.selfcheck_path`(写前 `os.makedirs(layout.doc_work_dir, exist_ok=True)`)
- `assemble(...)` 调用改传 `layout.work_dir` 与 `layout.assets_dir`(`assemble` 内部签名不变,已接显式路径)。
- 顶部 import 增 `from scripts.pipelines.textbooks.paths import resolve_layout`。

在 `reset_work_dir`/清 assets 那段,`assets_dir` 用 `layout.assets_dir`。

- [ ] **Step 2: 改 `reassemble_md` 吃 layout**

```python
def reassemble_md(layout, pdf_path: str | None, dpi: int) -> str | None:
    manifest = cp.load_manifest(layout.work_dir)
    if not manifest:
        return None
    total = manifest["fingerprint"]["page_count"]
    if not total:
        return None
    result = assemble(layout.work_dir, total, layout.stem, layout.assets_dir, pdf_path, dpi)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    with open(layout.md_path, "w", encoding="utf-8") as f:
        f.write(result["md"])
    return layout.md_path
```

（注意:`assemble` 内部对 `doc_dir` 的推导 `doc_dir = os.path.dirname(os.path.normpath(work_dir))` 用于 `load_corrections(doc_dir)`。改为让 `assemble` 接 `corrections_dir` 或直接接 `layout`。**最小改法**:给 `assemble` 增形参 `corrections_dir: str | None = None`,`load_corrections` 用它;`convert_pdf`/`reassemble_md` 传 `layout.doc_work_dir`。相应更新 `corrections.load_corrections` 若它假定固定文件名——确认它读 `<dir>/<stem>_corrections.json`,传 `layout.doc_work_dir` 即对。)

- [ ] **Step 3: 改 `main()` 加 `--work-dir`**

`--out` 语义变为交付根(帮助文本更新为「交付根(md+assets)」);新增 `--work-dir`(默认 None → `<out>/_work_root`)。调用改 `convert_pdf(args.src, args.out, args.work_dir, dpi=..., write_selfcheck=...)`。

- [ ] **Step 4: 重定向 `test_convert.py`**

把断言里 `<out>/<stem>/_work`、`<out>/<stem>/<stem>_selfcheck.json` 等旧路径改为 `resolve_layout(stem, out)` 的对应 property。md/assets 仍在 `<out>/<stem>/`。用 `resolve_layout` 构造期望路径,别手写字符串。

- [ ] **Step 5: 跑测试 + 真实端到端**

```bash
.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_convert.py -v
```
Expected: PASS。再跑一次真实单页端到端(用已存在的小样源 PDF;若无方便小样,可跳过并在 commit message 注明仅单测):确认 md 落 `<out>/<stem>/`、`_work` 落 `<out>/_work_root/<stem>/_work`。

- [ ] **Step 6: Commit**

```bash
git add scripts/pipelines/textbooks/convert.py scripts/pipelines/textbooks/tests/test_convert.py
git commit -m "refactor(textbooks): convert 吃双根布局(DocLayout)"
```

---

## Task 3: debug_repair.py —— build_repair_worklist 吃双根

**Files:**
- Modify: `scripts/pipelines/textbooks/debug_repair.py`(`build_repair_worklist`、`_render_errors_by_page`、`main`)
- Modify: `scripts/pipelines/textbooks/tests/test_debug_repair.py`

**Interfaces:**
- Consumes: `resolve_layout`。
- Produces: `build_repair_worklist(layout, pdf_path=None, repair_dpi=300, pad=10) -> dict`。

- [ ] **Step 1: 改签名用 layout**

`build_repair_worklist` 现从 `doc_dir` 派生 `work_dir=<doc_dir>/_work`、`repair_dir=<doc_dir>/<stem>_repair`、`render_errors=<doc_dir>/<stem>_render_errors.json`。改为吃 `layout`:
- `work_dir` → `layout.work_dir`
- `repair_dir` → `layout.repair_dir`、`crops_dir` → `os.path.join(layout.repair_dir, "crops")`、`pages_dir` → `os.path.join(layout.repair_dir, "_pages")`
- `worklist_path` → `layout.worklist_path`
- `_render_errors_by_page(layout)` → 读 `layout.render_errors_path`
- `stem` 用 `layout.stem`;manifest 从 `layout.work_dir` 读。

- [ ] **Step 2: 改 `main()`**

`--doc` 拆成 `--out`(交付根)+ `--work-dir` + `--stem`(或从 `--src` PDF 推 stem)。最小改法:加 `--out`/`--work-dir`/`--stem`,`layout = resolve_layout(args.stem, args.out, args.work_dir)`;保留 `--src` 覆盖源 PDF。

- [ ] **Step 3: 重定向 `test_debug_repair.py`**

测试里构造 doc_dir 与断言 worklist/crops 路径的,改用 `resolve_layout` 构 layout 并断言 `layout.worklist_path` 等。render_errors 测试写到 `layout.render_errors_path`。

- [ ] **Step 4: 跑测试**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_repair.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/debug_repair.py scripts/pipelines/textbooks/tests/test_debug_repair.py
git commit -m "refactor(textbooks): debug_repair 吃双根布局"
```

---

## Task 4: vision_repair.py —— run_vision_repair 吃双根(仅路径)

**Files:**
- Modify: `scripts/pipelines/textbooks/vision_repair.py`(`run_vision_repair`、`main`)
- Modify: `scripts/pipelines/textbooks/tests/test_vision_repair.py`

**Interfaces:**
- Consumes: `resolve_layout`。
- Produces: `run_vision_repair(layout, batch_fn=..., vision_fn=..., batch_size=5, parallel=3, timeout=300) -> dict`。

**红线**:本 Task **只改路径**。`call_claude_vision*`/`build_*_prompt`/`_correction_record`/打包并发/单图回炉全不动。

- [ ] **Step 1: 改签名用 layout**

`run_vision_repair` 现从 `doc_dir` 派生 `worklist_path=<doc_dir>/<stem>_repair/worklist.json`、`corrections_path=<doc_dir>/<stem>_corrections.json`。改为:
- 首参 `doc_dir` → `layout`
- `stem` → `layout.stem`
- `worklist_path` → `layout.worklist_path`
- `corrections_path` → `layout.corrections_path`(写前 `os.makedirs(layout.doc_work_dir, exist_ok=True)`)

- [ ] **Step 2: 改 `main()`**

`--doc` → `--out`/`--work-dir`/`--stem`,构 `layout = resolve_layout(args.stem, args.out, args.work_dir)`,传 `run_vision_repair(layout, ...)`。

- [ ] **Step 3: 重定向 `test_vision_repair.py` 的 run_vision_repair 相关测试**

现有 `_write_worklist(doc_dir, stem, items)` 与断言 `corrections_path` 的测试:改用 `resolve_layout` 构 layout,worklist 写到 `layout.worklist_path`,断言 `layout.corrections_path`。**注意**:多后端相关测试(若已存在/将来加)不在本 Task,勿动 `call_*`/`resolve_backend` 测试。

- [ ] **Step 4: 跑测试**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_vision_repair.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/vision_repair.py scripts/pipelines/textbooks/tests/test_vision_repair.py
git commit -m "refactor(textbooks): vision_repair 吃双根布局(仅路径,不动 AI 逻辑)"
```

---

## Task 5: debug_view.py —— 双根 + debug.html 落过程根

**Files:**
- Modify: `scripts/pipelines/textbooks/debug_view.py`
- Modify: `scripts/pipelines/textbooks/tests/test_debug_view.py`

**Interfaces:**
- Consumes: `resolve_layout`、`reassemble_md(layout, ...)`(Task 2)、`corrections.load_corrections`。

- [ ] **Step 1: 改 main / 渲染函数吃 layout**

debug_view 现从 `--doc`(doc_dir)派生 `_work`、md、corrections、render_errors、`<stem>_debug.html`。改为 `--out`/`--work-dir`/`--stem` → `layout`:
- 页 blocks 读 `layout.work_dir`
- render_errors 读 `layout.render_errors_path`
- corrections 读/写 `layout.doc_work_dir`(经 load_corrections/set_correction_status)
- reassemble 调 `reassemble_md(layout, ...)`
- **静态 HTML 输出落 `layout.debug_html_path`(过程根),绝不落交付根**
- 页图源 PDF 从 manifest.pdf_path 或 `--src`。

- [ ] **Step 2: 重定向 `test_debug_view.py`**

断言 `_debug.html` 落点的测试改断言 `layout.debug_html_path` 在过程根下、且不在交付根下(照抄 Task1 的 `test_debug_html_is_process_side` 精神)。

- [ ] **Step 3: 跑测试**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_view.py -v`
Expected: PASS。

- [ ] **Step 4: Commit**

```bash
git add scripts/pipelines/textbooks/debug_view.py scripts/pipelines/textbooks/tests/test_debug_view.py
git commit -m "refactor(textbooks): debug_view 吃双根;debug.html 落过程根"
```

---

## Task 6: katex_scan.py —— 新 CLI 积木(轻测)

**Files:**
- Create: `scripts/pipelines/textbooks/katex_scan.py`
- Create: `scripts/pipelines/textbooks/tests/test_katex_scan.py`

**Interfaces:**
- Produces: `scan_katex(md_path, out_path, node_bin=None) -> dict | None`(node 缺失返回 None)。供 Task 7 batch 接入。

- [ ] **Step 1: 写失败测试**

`tests/test_katex_scan.py`:

```python
import scripts.pipelines.textbooks.katex_scan as ks


def test_scan_katex_returns_none_when_node_missing(monkeypatch):
    monkeypatch.setattr(ks.shutil, "which", lambda name: None)
    assert ks.scan_katex("book.md", "out.json") is None


def test_scan_katex_invokes_node_with_mjs_and_md_and_out(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(ks.shutil, "which", lambda name: "C:/node/node.exe")

    out_file = tmp_path / "render_errors.json"

    class FakeProc:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        out_file.write_text('{"errors": []}', encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(ks.subprocess, "run", fake_run)
    result = ks.scan_katex("book.md", str(out_file))

    assert captured["argv"][0] == "C:/node/node.exe"
    assert any(a.endswith("scan_katex_errors.mjs") for a in captured["argv"])
    assert "book.md" in captured["argv"]
    assert str(out_file) in captured["argv"]
    assert result == {"errors": []}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_katex_scan.py -v`
Expected: FAIL,`ModuleNotFoundError`。

- [ ] **Step 3: 实现**

```python
"""KaTeX 硬报错扫描的 Python 薄壳:外调 debug_assets/scan_katex_errors.mjs 产
<stem>_render_errors.json。node 不在 PATH → 返回 None(优雅跳过,调用方打警告不失败)。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess

_MJS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "debug_assets", "scan_katex_errors.mjs")


def scan_katex(md_path: str, out_path: str, node_bin: str | None = None,
               timeout: int = 120) -> dict | None:
    node = node_bin or shutil.which("node")
    if not node:
        return None
    argv = [node, _MJS, "--md", md_path, "--out", out_path]
    subprocess.run(argv, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=timeout)
    if not os.path.exists(out_path):
        return None
    with open(out_path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser(description="KaTeX 硬报错扫描(薄壳调 .mjs)")
    ap.add_argument("--md", required=True, help="输入 md 路径")
    ap.add_argument("--out", required=True, help="输出 render_errors.json 路径")
    args = ap.parse_args()
    result = scan_katex(args.md, args.out)
    if result is None:
        print("[katex_scan] node 缺失或未产出,已跳过")
    else:
        print(f"[katex_scan] {len(result.get('errors', []))} 处硬报错 → {args.out}")


if __name__ == "__main__":
    main()
```

（实现前**确认** `scan_katex_errors.mjs` 的实际 flag:设计假定 `--md <path> --out <path>`,与文件头注释一致;若实际不同,以 .mjs 为准调整 argv。）

- [ ] **Step 4: 跑测试通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_katex_scan.py -v`
Expected: 2 个 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/katex_scan.py scripts/pipelines/textbooks/tests/test_katex_scan.py
git commit -m "feat(textbooks): 加 katex_scan CLI 积木(薄壳调 .mjs,node 缺失优雅跳过)"
```

---

## Task 7: batch.py —— 双根 + katex 默认接入

**Files:**
- Modify: `scripts/pipelines/textbooks/batch.py`(`run`、`_job_argv`、`_already_done`、`_read_summary`、`main`)
- Modify: `scripts/pipelines/textbooks/tests/test_batch.py`

**Interfaces:**
- Consumes: `resolve_layout`、`scan_katex`(Task 6)。

- [ ] **Step 1: 双根穿透**

- `run(...)` 与 `main()` 加 `--work-dir`(默认 None → 每本 `resolve_layout(stem, out, work_dir)`)。
- `_job_argv` 把 `--work-dir` 透传给 convert 子进程(convert.main 已在 Task 2 支持)。
- `_already_done(out_root, work_root, pdf, dpi)`:manifest 从 `layout.work_dir` 读(原 `out_root/<stem>/_work`)。
- `_read_summary(out_root, work_root, pdf)`:selfcheck 从 `layout.selfcheck_path` 读,manifest 从 `layout.work_dir` 读,deferred marker 仍在交付根 `_deferred_born_digital`。

- [ ] **Step 2: katex 默认接入**

每本书 convert 子进程返回 rc==0 且非 B 路后,调 `scan_katex(layout.md_path, layout.render_errors_path)`;`--no-katex-scan` 关。node 缺失(返回 None)打一行警告 `[katex] node 缺失,跳过 <stem>`,不影响该书状态。

`main()` 加 `ap.add_argument("--no-katex-scan", action="store_true")`,透传给 `run(..., katex_scan_enabled=not args.no_katex_scan)`。

- [ ] **Step 3: 重定向 `test_batch.py`**

`_already_done`/`_read_summary` 相关测试改用双根构造(work 数据在 `<out>/_work_root/<stem>/`)。加一条:katex 接入默认调用(monkeypatch `batch.scan_katex` 断言被调;`--no-katex-scan` 时不调)。node 缺失路径可靠 monkeypatch scan_katex 返回 None 验证不掀翻。

- [ ] **Step 4: 跑测试 + 全量回归**

```bash
.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/ -v
```
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/batch.py scripts/pipelines/textbooks/tests/test_batch.py
git commit -m "feat(textbooks): batch 双根 + katex 扫描默认接入(--no-katex-scan 可关)"
```

---

## Task 8: README 更新(文档,无测试)

**Files:**
- Modify: `scripts/pipelines/textbooks/README.md`

- [ ] **Step 1: 重写 README**

覆盖:环境(`.venv-textbooks`)、**双根布局图 + `--out`/`--work-dir`**、完整工作流表(spec §数据流,标 stage/执行者/碰哪根)、**全部 stage CLI 用法**(convert / batch / watchdog / debug_repair / vision_repair / katex_scan / debug_view,各一行示例)、测试命令。删掉「首版范围」「已知边界」里已实现的过时项(分块/批量/断点续跑/debug_view 均已实现)。保留大文件/无人值守那节(仍准确),但把 `--out` 描述更新为交付根、补 `--work-dir`。

- [ ] **Step 2: 人工核对渲染**

Read 一遍确认无残留旧描述(如「Opus 审查待后续」若已由 vision_repair 取代则更新措辞)、无断链。

- [ ] **Step 3: Commit**

```bash
git add scripts/pipelines/textbooks/README.md
git commit -m "docs(textbooks): README 更新为双根布局 + 全 stage CLI 使用说明"
```

---

## Self-Review 记录(写计划时已过一遍)

- **Spec 覆盖**:paths.py(Task1)/ 双根穿透 convert·debug_repair·vision_repair·debug_view·batch(Task2-5,7)/ katex 积木 + 接入(Task6-7)/ README(Task8)/ 迁移(spec 已记为完成)。全覆盖。
- **占位扫描**:paths.py/katex_scan 给了完整实现与测试;编排 Task 按用户「不一刀切 TDD」指令,故意用「改签名 + 重定向现有测试 + 跑绿 + 真实端到端」而非逐行 test-first,已在各 Task 抬头注明,不是占位。
- **类型一致性**:`resolve_layout(stem, deliverables_root, work_root=None) -> DocLayout`;各 Task 一律经 `layout.<property>` 取路径,不手写字符串;`convert_pdf` 与 `reassemble_md`/`build_repair_worklist`/`run_vision_repair` 的首参约定分别为(pdf_path, deliverables_dir, work_dir)与(layout),已在各 Interfaces 块对齐。
- **依赖顺序**:Task1(paths)最先;Task6(katex_scan)先于 Task7(batch 接入);其余 Task2-5 只依赖 Task1,可并行但建议顺序执行以便逐步回归。

## 执行说明

本计划为**独立可执行**文档(零上下文工程师可照做)。每个 Task 以 commit 收口,可逐 Task 审核。
