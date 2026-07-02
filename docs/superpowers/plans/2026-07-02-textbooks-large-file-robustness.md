# textbooks 大文件稳健化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `textbooks` 单文档转换在 700+ 页大部头上稳健——磁盘有界、断点续跑、坏页隔离、进程级崩溃自动恢复。

**Architecture:** 拆一个纯确定性 `checkpoint.py`（manifest/指纹/断点/毒页簿记，无 GPU）；`preprocess.py` 加单页栅格化；`convert.py` 改为逐页流式 + 可续跑编排（每页 res.json 作检查点）；新增 `watchdog.py` supervisor 子进程反复拉起 convert 直到跑完。设计见 [spec](../specs/2026-07-02-textbooks-large-file-robustness-design.md)。

**Tech Stack:** Python 3.11、PyMuPDF(fitz)、pytest；引擎 PaddleOCR-VL 1.6（本计划不碰引擎，测试全程打桩不需 GPU）。

## Global Constraints

- 测试从**仓库根**运行：`.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v`（namespace 包，导入用 `scripts.pipelines.textbooks.X`）。
- 不改 patents/general；不改 `engine.py`（空白页问题在编排层解决）；`02_Source/` 只读。
- 确定性优先、ML 只判断不改字符。写文件一律 `encoding="utf-8"`。
- 默认 DPI = **150**（实测甜区）；毒页最大硬尝试 `MAX_HARD_ATTEMPTS = 2`；看门狗兜底 `MAX_RESTARTS = 50`。
- 检查点文件名：第 i 页（1-indexed）→ PNG `page_{i:04d}.png`、检查点 `page_{i:04d}_res.json`（后者由 `engine.predict_page` 从 PNG stem 自动派生，命名必须对齐）。
- 每个 res.json 内含 `parsing_res_list` 键（引擎产物如此，空白页标记也照此结构）。

---

## File Structure

- `scripts/pipelines/textbooks/checkpoint.py`（新）：manifest 读写、PDF+DPI 指纹、per-page 完成判定、待跑页集、空白页标记、坏页/in_progress/毒页簿记。纯确定性、无 GPU。
- `scripts/pipelines/textbooks/preprocess.py`（改）：新增 `pdf_page_to_png` 单页栅格化；`pdf_to_pngs` 默认 dpi 200→150。
- `scripts/pipelines/textbooks/convert.py`（改）：`convert_pdf` 重写为逐页流式可续跑编排 + 从检查点重组；CLI 加 `--dpi`。
- `scripts/pipelines/textbooks/watchdog.py`（新）：子进程反复拉起 convert 直到 exit 0 或超 `--max-restarts`。
- 测试：`tests/test_checkpoint.py`（新）、`tests/test_preprocess.py`（补）、`tests/test_convert.py`（新）、`tests/test_watchdog.py`（新）。

---

## Task 1: preprocess 单页栅格化 + 默认 DPI 150

**Files:**
- Modify: `scripts/pipelines/textbooks/preprocess.py`
- Test: `scripts/pipelines/textbooks/tests/test_preprocess.py`

**Interfaces:**
- Produces: `pdf_page_to_png(pdf_path: str, page: int, out_dir: str, dpi: int = 150) -> str`（page 为 1-indexed，输出 `<out_dir>/page_{page:04d}.png`，返回该路径）。`pdf_to_pngs(...)` 默认 dpi 改 150。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_preprocess.py` 末尾追加：

```python
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png


def test_pdf_page_to_png_single(tmp_path):
    doc = fitz.open()
    doc.new_page(); doc.new_page(); doc.new_page()
    pdf = tmp_path / "three.pdf"
    doc.save(str(pdf))
    out = tmp_path / "work"
    p = pdf_page_to_png(str(pdf), 2, str(out), dpi=100)
    assert p.endswith("page_0002.png")
    assert os.path.exists(p)
    # 只产该页,不产其它
    produced = [f for f in os.listdir(str(out)) if f.endswith(".png")]
    assert produced == ["page_0002.png"]


def test_pdf_page_to_png_naming_4digits(tmp_path):
    doc = fitz.open(); doc.new_page()
    pdf = tmp_path / "one.pdf"
    doc.save(str(pdf))
    p = pdf_page_to_png(str(pdf), 1, str(tmp_path / "w"))
    assert os.path.basename(p) == "page_0001.png"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_preprocess.py -v`
Expected: FAIL（`ImportError: cannot import name 'pdf_page_to_png'`）

- [ ] **Step 3: Implement**

编辑 `preprocess.py`：把 `pdf_to_pngs` 签名默认 `dpi: int = 200` 改为 `dpi: int = 150`，并在文件末尾追加：

```python
def pdf_page_to_png(pdf_path: str, page: int, out_dir: str, dpi: int = 150) -> str:
    """栅格化 PDF 第 page 页(1-indexed)为单张 PNG,返回其路径。"""
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        p = os.path.join(out_dir, f"page_{page:04d}.png")
        pix.save(p)
        return p
    finally:
        doc.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_preprocess.py -v`
Expected: PASS（含原有 `test_pdf_to_pngs`）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/preprocess.py scripts/pipelines/textbooks/tests/test_preprocess.py
git commit -m "feat(textbooks): preprocess 单页栅格化 + 默认 DPI 150"
```

---

## Task 2: checkpoint — 指纹 + manifest 读写

**Files:**
- Create: `scripts/pipelines/textbooks/checkpoint.py`
- Test: `scripts/pipelines/textbooks/tests/test_checkpoint.py`

**Interfaces:**
- Produces:
  - 常量 `MAX_HARD_ATTEMPTS = 2`、`MAX_RESTARTS = 50`、`DEFAULT_DPI = 150`
  - `pdf_fingerprint(pdf_path: str) -> dict` → `{"page_count": int, "size_bytes": int}`
  - `manifest_path(work_dir: str) -> str`
  - `load_manifest(work_dir: str) -> dict | None`
  - `save_manifest(work_dir: str, manifest: dict) -> None`（写入并刷新 `updated`）
  - `new_manifest(pdf_path: str, fingerprint: dict, dpi: int, route: str) -> dict`
  - `fingerprint_ok(manifest: dict, pdf_path: str, dpi: int) -> bool`
  - `reset_work_dir(work_dir: str) -> None`

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_checkpoint.py`：

```python
import json
import os
import fitz
from scripts.pipelines.textbooks import checkpoint as cp


def _make_pdf(tmp_path, n_pages):
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / "book.pdf"
    doc.save(str(p))
    return str(p)


def test_pdf_fingerprint(tmp_path):
    pdf = _make_pdf(tmp_path, 5)
    fp = cp.pdf_fingerprint(pdf)
    assert fp["page_count"] == 5
    assert fp["size_bytes"] == os.path.getsize(pdf)


def test_manifest_roundtrip(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    m = cp.new_manifest("book.pdf", {"page_count": 5, "size_bytes": 100}, 150, "A")
    cp.save_manifest(work, m)
    loaded = cp.load_manifest(work)
    assert loaded["fingerprint"]["page_count"] == 5
    assert loaded["dpi"] == 150
    assert loaded["route"] == "A"
    assert loaded["failed_pages"] == []
    assert loaded["in_progress"] is None
    assert loaded["restarts"] == 0
    assert "updated" in loaded


def test_load_manifest_absent(tmp_path):
    assert cp.load_manifest(str(tmp_path)) is None


def test_fingerprint_ok_matches(tmp_path):
    pdf = _make_pdf(tmp_path, 3)
    fp = cp.pdf_fingerprint(pdf)
    m = cp.new_manifest(pdf, fp, 150, "A")
    assert cp.fingerprint_ok(m, pdf, 150) is True


def test_fingerprint_ok_dpi_mismatch(tmp_path):
    pdf = _make_pdf(tmp_path, 3)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 150, "A")
    assert cp.fingerprint_ok(m, pdf, 200) is False   # DPI 变 → 失配


def test_fingerprint_ok_size_mismatch(tmp_path):
    pdf = _make_pdf(tmp_path, 3)
    m = cp.new_manifest(pdf, {"page_count": 3, "size_bytes": 999999}, 150, "A")
    assert cp.fingerprint_ok(m, pdf, 150) is False   # size 变 → 失配


def test_reset_work_dir(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    with open(os.path.join(work, "stale.json"), "w") as f:
        f.write("{}")
    cp.reset_work_dir(work)
    assert os.path.isdir(work)
    assert os.listdir(work) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_checkpoint.py -v`
Expected: FAIL（`ModuleNotFoundError: ... checkpoint`）

- [ ] **Step 3: Implement**

创建 `checkpoint.py`：

```python
"""断点/manifest/指纹/毒页 簿记(纯确定性,无 GPU)。"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

import fitz

MAX_HARD_ATTEMPTS = 2      # 某页硬崩进程超此次数 → 标 process-killed 跳过
MAX_RESTARTS = 50          # 看门狗累计重启兜底
DEFAULT_DPI = 150          # 实测甜区


def pdf_fingerprint(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    try:
        n = doc.page_count
    finally:
        doc.close()
    return {"page_count": n, "size_bytes": os.path.getsize(pdf_path)}


def manifest_path(work_dir: str) -> str:
    return os.path.join(work_dir, "manifest.json")


def load_manifest(work_dir: str) -> dict | None:
    p = manifest_path(work_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def save_manifest(work_dir: str, manifest: dict) -> None:
    os.makedirs(work_dir, exist_ok=True)
    manifest["updated"] = datetime.now().isoformat(timespec="seconds")
    with open(manifest_path(work_dir), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def new_manifest(pdf_path: str, fingerprint: dict, dpi: int, route: str) -> dict:
    return {
        "pdf_path": pdf_path,
        "fingerprint": fingerprint,
        "dpi": dpi,
        "route": route,
        "failed_pages": [],
        "in_progress": None,
        "restarts": 0,
    }


def fingerprint_ok(manifest: dict, pdf_path: str, dpi: int) -> bool:
    if manifest.get("dpi") != dpi:
        return False
    return manifest.get("fingerprint") == pdf_fingerprint(pdf_path)


def reset_work_dir(work_dir: str) -> None:
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir, exist_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_checkpoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/checkpoint.py scripts/pipelines/textbooks/tests/test_checkpoint.py
git commit -m "feat(textbooks): checkpoint 指纹(含DPI)+manifest 读写"
```

---

## Task 3: checkpoint — per-page 完成判定 / 待跑页集 / 空白页标记 / 读块

**Files:**
- Modify: `scripts/pipelines/textbooks/checkpoint.py`
- Test: `scripts/pipelines/textbooks/tests/test_checkpoint.py`

**Interfaces:**
- Consumes: 无（同模块）
- Produces:
  - `page_stem(page: int) -> str` → `f"page_{page:04d}"`
  - `page_res_path(work_dir: str, page: int) -> str`
  - `is_page_done(work_dir: str, page: int) -> bool`（res.json 存在且 `json.load` 成功）
  - `write_empty_page(work_dir: str, page: int) -> None`（写 `{"parsing_res_list": []}`）
  - `load_page_blocks(work_dir: str, page: int) -> list[dict]`（读 res.json 的 parsing_res_list；缺失/损坏返回 `[]`）
  - `pages_todo(work_dir: str, total: int) -> list[int]`（1..total 中未完成的页）

- [ ] **Step 1: Write the failing tests**

在 `tests/test_checkpoint.py` 追加：

```python
def test_page_stem_and_res_path(tmp_path):
    assert cp.page_stem(7) == "page_0007"
    assert cp.page_res_path("/w", 7).endswith(os.path.join("/w", "page_0007_res.json").replace("/", os.sep))


def _write_res(work, page, blocks):
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, page), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": blocks}, f)


def test_is_page_done_true_false(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [{"block_order": 0}])
    assert cp.is_page_done(work, 1) is True
    assert cp.is_page_done(work, 2) is False


def test_is_page_done_corrupt_json(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        f.write('{"parsing_res_list": [')   # 半截
    assert cp.is_page_done(work, 1) is False   # 损坏 → 未完成


def test_write_empty_page_marks_done(tmp_path):
    work = str(tmp_path / "_work")
    cp.write_empty_page(work, 3)
    assert cp.is_page_done(work, 3) is True
    assert cp.load_page_blocks(work, 3) == []


def test_load_page_blocks(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [{"block_order": 0, "block_content": "hi"}])
    assert cp.load_page_blocks(work, 1) == [{"block_order": 0, "block_content": "hi"}]
    assert cp.load_page_blocks(work, 9) == []          # 缺失
    with open(cp.page_res_path(work, 2), "w") as f:
        f.write("broken")
    assert cp.load_page_blocks(work, 2) == []          # 损坏


def test_pages_todo(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 1, [])
    _write_res(work, 3, [])
    assert cp.pages_todo(work, 4) == [2, 4]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_checkpoint.py -v -k "page or todo or empty or done or blocks"`
Expected: FAIL（`AttributeError: module ... has no attribute 'page_stem'`）

- [ ] **Step 3: Implement**

在 `checkpoint.py` 追加：

```python
def page_stem(page: int) -> str:
    return f"page_{page:04d}"


def page_res_path(work_dir: str, page: int) -> str:
    return os.path.join(work_dir, f"{page_stem(page)}_res.json")


def is_page_done(work_dir: str, page: int) -> bool:
    p = page_res_path(work_dir, page)
    if not os.path.exists(p):
        return False
    try:
        with open(p, encoding="utf-8") as f:
            json.load(f)
        return True
    except (ValueError, OSError):
        return False


def write_empty_page(work_dir: str, page: int) -> None:
    os.makedirs(work_dir, exist_ok=True)
    with open(page_res_path(work_dir, page), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": []}, f, ensure_ascii=False)


def load_page_blocks(work_dir: str, page: int) -> list[dict]:
    p = page_res_path(work_dir, page)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("parsing_res_list", [])
    except (ValueError, OSError, AttributeError):
        return []


def pages_todo(work_dir: str, total: int) -> list[int]:
    return [i for i in range(1, total + 1) if not is_page_done(work_dir, i)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_checkpoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/checkpoint.py scripts/pipelines/textbooks/tests/test_checkpoint.py
git commit -m "feat(textbooks): checkpoint per-page 完成判定/待跑集/空白页标记/读块"
```

---

## Task 4: checkpoint — 坏页 / in_progress / 毒页簿记

**Files:**
- Modify: `scripts/pipelines/textbooks/checkpoint.py`
- Test: `scripts/pipelines/textbooks/tests/test_checkpoint.py`

**Interfaces:**
- Consumes: 同模块 `is_page_done`、`MAX_HARD_ATTEMPTS`
- Produces（均原地改 `manifest` dict，不落盘，由调用方负责 `save_manifest`）：
  - `record_failure(manifest: dict, page: int, error: str, kind: str) -> None`
  - `set_in_progress(manifest: dict, page: int) -> None`（若已有 in_progress 且同页则 attempts+1，否则 attempts=1）
  - `clear_in_progress(manifest: dict) -> None`
  - `resolve_poison(manifest: dict, work_dir: str, max_hard_attempts: int = MAX_HARD_ATTEMPTS) -> None`
    （启动时调用：若残留 in_progress 指向的页无 res.json 且 attempts≥max → 移入 failed_pages(kind="process-killed")并清 in_progress；若该页已有 res.json → 清 in_progress；否则保留待重试）

- [ ] **Step 1: Write the failing tests**

在 `tests/test_checkpoint.py` 追加：

```python
def test_record_failure(tmp_path):
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.record_failure(m, 5, "CUDA oom", "page-exception")
    assert m["failed_pages"] == [{"page": 5, "error": "CUDA oom", "kind": "page-exception"}]


def test_set_in_progress_first_and_retry(tmp_path):
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.set_in_progress(m, 7)
    assert m["in_progress"] == {"page": 7, "attempts": 1}
    cp.set_in_progress(m, 7)                       # 同页重试
    assert m["in_progress"] == {"page": 7, "attempts": 2}
    cp.set_in_progress(m, 8)                       # 换页 → 重置
    assert m["in_progress"] == {"page": 8, "attempts": 1}


def test_clear_in_progress(tmp_path):
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.set_in_progress(m, 7)
    cp.clear_in_progress(m)
    assert m["in_progress"] is None


def test_resolve_poison_marks_failed_over_threshold(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    m["in_progress"] = {"page": 2, "attempts": cp.MAX_HARD_ATTEMPTS}   # 该页无 res.json
    cp.resolve_poison(m, work)
    assert m["in_progress"] is None
    assert m["failed_pages"] == [{"page": 2, "error": "process killed repeatedly",
                                  "kind": "process-killed"}]


def test_resolve_poison_keeps_under_threshold(tmp_path):
    work = str(tmp_path / "_work")
    os.makedirs(work)
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    m["in_progress"] = {"page": 2, "attempts": 1}
    cp.resolve_poison(m, work)
    assert m["in_progress"] == {"page": 2, "attempts": 1}   # 保留待重试
    assert m["failed_pages"] == []


def test_resolve_poison_page_actually_done(tmp_path):
    work = str(tmp_path / "_work")
    _write_res(work, 2, [])                                 # 崩在写完之后
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    m["in_progress"] = {"page": 2, "attempts": 1}
    cp.resolve_poison(m, work)
    assert m["in_progress"] is None                        # 已完成 → 仅清标记
    assert m["failed_pages"] == []


def test_resolve_poison_noop_when_none(tmp_path):
    m = cp.new_manifest("b.pdf", {"page_count": 3, "size_bytes": 1}, 150, "A")
    cp.resolve_poison(m, str(tmp_path))
    assert m["in_progress"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_checkpoint.py -v -k "failure or in_progress or poison"`
Expected: FAIL（`AttributeError: ... record_failure`）

- [ ] **Step 3: Implement**

在 `checkpoint.py` 追加：

```python
def record_failure(manifest: dict, page: int, error: str, kind: str) -> None:
    manifest["failed_pages"].append({"page": page, "error": error, "kind": kind})


def set_in_progress(manifest: dict, page: int) -> None:
    ip = manifest.get("in_progress")
    attempts = ip["attempts"] + 1 if ip and ip.get("page") == page else 1
    manifest["in_progress"] = {"page": page, "attempts": attempts}


def clear_in_progress(manifest: dict) -> None:
    manifest["in_progress"] = None


def resolve_poison(manifest: dict, work_dir: str,
                   max_hard_attempts: int = MAX_HARD_ATTEMPTS) -> None:
    ip = manifest.get("in_progress")
    if not ip:
        return
    page = ip["page"]
    if is_page_done(work_dir, page):        # 崩在写完 res.json 之后 → 其实已完成
        manifest["in_progress"] = None
        return
    if ip["attempts"] >= max_hard_attempts:  # 反复硬崩进程 → 判毒页,跳过
        record_failure(manifest, page, "process killed repeatedly", "process-killed")
        manifest["in_progress"] = None
    # 否则保留 in_progress,循环会重试该页
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_checkpoint.py -v`
Expected: PASS（全部 checkpoint 测试）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/checkpoint.py scripts/pipelines/textbooks/tests/test_checkpoint.py
git commit -m "feat(textbooks): checkpoint 坏页/in_progress/毒页簿记"
```

---

## Task 5: convert — 逐页流式可续跑编排 + 从检查点重组

**Files:**
- Modify: `scripts/pipelines/textbooks/convert.py`
- Test: `scripts/pipelines/textbooks/tests/test_convert.py`（新）

**Interfaces:**
- Consumes: Task1 `pdf_page_to_png`；Task2-4 `checkpoint` 全 API；现有 `triage`、`engine.predict_page`、`reconstruct_markdown`、`block_coverage`、`katex_incompat_scan`
- Produces:
  - `assemble(work_dir: str, total: int) -> tuple[str, list[dict]]`（按页顺序读检查点 → (md, all_blocks)）
  - `convert_pdf(pdf_path: str, out_dir: str | None = None, dpi: int = 150) -> dict`（返回含 `route`/`md_path`/`selfcheck`/`failed_pages`）

> 说明：本任务实现流式主循环（跳过已完成页、坏页 try/except 隔离、空白页写标记、progress 打印、指纹失配清空、从检查点重组）。in_progress 断点写入与毒页 startup 解析在 Task 6 加。本任务循环里**先不写 in_progress**（Task 6 补），故本任务测试不覆盖毒页。

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_convert.py`：

```python
import json
import os
import fitz
import pytest
from scripts.pipelines.textbooks import convert as cv
from scripts.pipelines.textbooks import checkpoint as cp


def _make_scan_pdf(tmp_path, n_pages):
    """无文本层 PDF(空白页) → triage 判 A。"""
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / "scan.pdf"
    doc.save(str(p))
    return str(p)


def _stub_engine(monkeypatch, behavior):
    """behavior: page(1-indexed)->blocks 或抛异常的可调用。桩掉 predict_page,
    并模拟 engine 对非空结果落 res.json 的副作用(空结果不落,复刻真 engine)。"""
    def fake_predict(png_path, work_dir):
        stem = os.path.splitext(os.path.basename(png_path))[0]  # page_0002
        page = int(stem.split("_")[1])
        blocks = behavior(page)                                 # 可能抛异常
        if blocks:                                              # 复刻 engine:非空才落盘
            os.makedirs(work_dir, exist_ok=True)
            with open(os.path.join(work_dir, f"{stem}_res.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"parsing_res_list": blocks}, f)
        return blocks
    monkeypatch.setattr(cv, "predict_page", fake_predict)


def _one_text_block(page):
    return [{"block_order": 0, "block_label": "text",
             "block_content": f"page {page} content"}]


def test_convert_full_run_A(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert res["route"] == "A"
    assert os.path.exists(res["md_path"])
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md
    assert res["failed_pages"] == []


def test_convert_disk_bounded(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 4)
    seen_png_counts = []

    def behavior(page):
        # predict 时快照 _work 里 png 数量,应 ≤1
        work = os.path.join(str(tmp_path / "out"), "scan", "_work")
        seen_png_counts.append(len([f for f in os.listdir(work) if f.endswith(".png")]))
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert max(seen_png_counts) <= 1
    # 结束后无残留 png
    work = os.path.join(str(tmp_path / "out"), "scan", "_work")
    assert [f for f in os.listdir(work) if f.endswith(".png")] == []


def test_convert_resume_skips_done(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    predicted = []
    def behavior(page):
        predicted.append(page)
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    # 预置第 1、2 页检查点 + 匹配 manifest
    work = os.path.join(str(tmp_path / "out"), "scan", "_work")
    os.makedirs(work, exist_ok=True)
    for pg in (1, 2):
        with open(cp.page_res_path(work, pg), "w", encoding="utf-8") as f:
            json.dump({"parsing_res_list": _one_text_block(pg)}, f)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    cp.save_manifest(work, m)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert predicted == [3]                       # 只跑缺失页


def test_convert_bad_page_isolated(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    def behavior(page):
        if page == 2:
            raise RuntimeError("boom on page 2")
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md
    assert [f["page"] for f in res["failed_pages"]] == [2]
    assert res["failed_pages"][0]["kind"] == "page-exception"


def test_convert_empty_page_checkpointed(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    calls = []
    def behavior(page):
        calls.append(page)
        return [] if page == 1 else _one_text_block(page)  # 第1页空白
    _stub_engine(monkeypatch, behavior)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    work = os.path.join(out, "scan", "_work")
    assert cp.is_page_done(work, 1) is True            # 空白页也落了检查点
    # 再跑一次:空白页不应被重跑
    calls.clear()
    cv.convert_pdf(pdf, out, dpi=100)
    assert calls == []


def test_convert_fingerprint_mismatch_wipes(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    # 预置一个 DPI 不同的旧 manifest + 一页旧检查点
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [{"block_order": 0, "block_label": "text",
                                         "block_content": "STALE 150dpi"}]}, f)
    cp.save_manifest(work, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 150, "A"))
    cv.convert_pdf(pdf, out, dpi=100)                   # 请求 100 ≠ 记录 150
    md = open(os.path.join(out, "scan", "scan.md"), encoding="utf-8").read()
    assert "STALE" not in md                            # 旧检查点被清空重跑


def test_convert_route_B_registers(tmp_path, monkeypatch):
    # 有优质文本层 → triage 判 B,登记不转
    doc = fitz.open()
    for _ in range(3):
        pg = doc.new_page()
        pg.insert_text((72, 72), "the quick brown fox jumps over the lazy dog " * 8)
    pdf = tmp_path / "born.pdf"
    doc.save(str(pdf))
    res = cv.convert_pdf(str(pdf), str(tmp_path / "out"))
    assert res["route"] == "B"
    assert res["md_path"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_convert.py -v`
Expected: FAIL（`AttributeError: module ... has no attribute 'assemble'` 等）

- [ ] **Step 3: Implement**

用下面内容**整体替换** `convert.py`：

```python
"""单文档编排:分诊 → (A/C)逐页流式 OCR(可续跑/磁盘有界/坏页隔离) → 重组 md。B 登记不转。"""
from __future__ import annotations

import argparse
import os
import time

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown
from scripts.pipelines.textbooks.selfcheck import block_coverage, katex_incompat_scan
from scripts.pipelines.textbooks import checkpoint as cp


def assemble(work_dir: str, total: int) -> tuple[str, list[dict]]:
    """按页序读检查点 → (md, all_blocks)。缺失/失败页贡献空串。"""
    md_pages: list[str] = []
    all_blocks: list[dict] = []
    for i in range(1, total + 1):
        blocks = cp.load_page_blocks(work_dir, i)
        all_blocks.extend(blocks)
        page_md = reconstruct_markdown(blocks)
        if page_md:
            md_pages.append(page_md)
    return "\n\n".join(md_pages) + "\n", all_blocks


def _register_deferred(pdf_path: str, out_dir: str, stem: str) -> dict:
    deferred = os.path.join(out_dir, "_deferred_born_digital")
    os.makedirs(deferred, exist_ok=True)
    with open(os.path.join(deferred, stem + ".txt"), "w", encoding="utf-8") as f:
        f.write(pdf_path + "\n")
    return {"route": "B", "md_path": None, "selfcheck": None, "failed_pages": []}


def convert_pdf(pdf_path: str, out_dir: str | None = None,
                dpi: int = cp.DEFAULT_DPI) -> dict:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(pdf_path))
    route = triage(pdf_path)
    if route == "B":
        return _register_deferred(pdf_path, out_dir, stem)

    doc_out = os.path.join(out_dir, stem)
    work_dir = os.path.join(doc_out, "_work")

    # 指纹校验:源或 DPI 变 → 清空全新跑
    manifest = cp.load_manifest(work_dir)
    if manifest is None or not cp.fingerprint_ok(manifest, pdf_path, dpi):
        if manifest is not None:
            print(f"[textbooks] 指纹失配(源或DPI变),清空 {work_dir} 全新跑")
        cp.reset_work_dir(work_dir)
        manifest = cp.new_manifest(pdf_path, cp.pdf_fingerprint(pdf_path), dpi, route)
        cp.save_manifest(work_dir, manifest)

    total = manifest["fingerprint"]["page_count"]
    todo = cp.pages_todo(work_dir, total)
    done = total - len(todo)
    durations: list[float] = []
    for page in todo:
        t = time.time()
        png = pdf_page_to_png(pdf_path, page, work_dir, dpi=dpi)
        try:
            blocks = predict_page(png, work_dir)   # 非空时 engine 已落 res.json
            if not blocks and not cp.is_page_done(work_dir, page):
                cp.write_empty_page(work_dir, page)   # 空白页显式标记完成
        except Exception as e:                        # noqa: BLE001 坏页隔离
            cp.record_failure(manifest, page, f"{type(e).__name__}: {e}",
                              "page-exception")
            cp.save_manifest(work_dir, manifest)
        finally:
            if os.path.exists(png):
                os.remove(png)                        # 磁盘有界:predict 后即删
        done += 1
        durations.append(time.time() - t)
        avg = sum(durations) / len(durations)
        eta_h = avg * (total - done) / 3600
        nfail = len(manifest["failed_pages"])
        print(f"[page {page}/{total}] {durations[-1]:.0f}s "
              f"(完成 {done} 失败 {nfail} ETA {eta_h:.1f}h)")

    # 从检查点重组(每次运行都做,部分完成也产出部分 md)
    md, all_blocks = assemble(work_dir, total)
    os.makedirs(doc_out, exist_ok=True)
    md_path = os.path.join(doc_out, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    cp.save_manifest(work_dir, manifest)
    return {"route": route, "md_path": md_path, "selfcheck": check,
            "failed_pages": manifest["failed_pages"]}


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 单文档转换(可续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    args = ap.parse_args()
    res = convert_pdf(args.src, args.out, dpi=args.dpi)
    print(f"[route={res['route']}] md={res['md_path']}")
    if res.get("failed_pages"):
        print(f"[textbooks] 失败页 {len(res['failed_pages'])}:",
              [f["page"] for f in res["failed_pages"]])
    if res["selfcheck"]:
        c = res["selfcheck"]
        print(f"[Tier0] blocks {c['in_md']}/{c['total']} 覆盖, 缺 {len(c['missing'])}")
        if c.get("katex_incompat"):
            print("[Tier0] KaTeX 不兼容残留:", ", ".join(c["katex_incompat"]))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_convert.py -v`
Expected: PASS（8 个用例）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/convert.py scripts/pipelines/textbooks/tests/test_convert.py
git commit -m "feat(textbooks): convert 逐页流式可续跑+磁盘有界+坏页隔离+从检查点重组"
```

---

## Task 6: convert — in_progress 断点 + 毒页 startup 解析

**Files:**
- Modify: `scripts/pipelines/textbooks/convert.py`
- Test: `scripts/pipelines/textbooks/tests/test_convert.py`

**Interfaces:**
- Consumes: Task4 `cp.set_in_progress`/`clear_in_progress`/`resolve_poison`
- Produces: `convert_pdf` 在指纹校验后调 `resolve_poison`；主循环每页 predict 前 `set_in_progress`+存盘、成功/捕获后 `clear_in_progress`。行为对外不变(返回结构同 Task5),仅增进程级崩溃恢复能力。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_convert.py` 追加：

```python
def test_convert_poison_page_skipped_on_startup(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    # 模拟:第 2 页已硬崩进程 MAX 次(in_progress 残留、该页无 res.json)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    m["in_progress"] = {"page": 2, "attempts": cp.MAX_HARD_ATTEMPTS}
    cp.save_manifest(work, m)
    res = cv.convert_pdf(pdf, out, dpi=100)
    # 第 2 页被判毒页跳过,进 failed_pages(process-killed),1、3 正常
    kinds = {f["page"]: f["kind"] for f in res["failed_pages"]}
    assert kinds.get(2) == "process-killed"
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md


def test_convert_in_progress_cleared_after_success(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    m = cp.load_manifest(os.path.join(out, "scan", "_work"))
    assert m["in_progress"] is None      # 正常跑完不残留 in_progress


def test_convert_failed_pages_deduped_across_runs(tmp_path, monkeypatch):
    # 同页跨多次运行反复失败(page-exception)不应在 failed_pages 累积重复条目
    pdf = _make_scan_pdf(tmp_path, 2)
    def behavior(page):
        if page == 1:
            raise RuntimeError("always fails p1")
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)                 # run1: p1 失败
    res = cv.convert_pdf(pdf, out, dpi=100)           # run2: p1 再次失败
    pages = [f["page"] for f in res["failed_pages"]]
    assert pages.count(1) == 1                        # 只留一条,不累积
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_convert.py -v -k "poison or deduped"`
Expected: FAIL（第 2 页未被跳过 → 无 process-killed 记录；重复条目未去重）

- [ ] **Step 3: Implement**

> 注意：Task 5(含 review 修复)已把 `convert_pdf` 的循环改成 `png = None` 初始化 + `pdf_page_to_png` 在
> try 内 + `finally: if png and os.path.exists(png)`,并在 assemble 前加了 failed_pages 清理。本任务在此
> **已修复版**基础上增量:加毒页 startup 解析、循环包 in_progress、todo 排除毒页、把 failed_pages 清理升级为
> 去重。为避免歧义,下面给出 `convert_pdf` 的**最终完整形态**——用它整体替换现有 `convert_pdf`
> 函数(assemble/_register_deferred/main 不变)：

```python
def convert_pdf(pdf_path: str, out_dir: str | None = None,
                dpi: int = cp.DEFAULT_DPI) -> dict:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(pdf_path))
    route = triage(pdf_path)
    if route == "B":
        return _register_deferred(pdf_path, out_dir, stem)

    doc_out = os.path.join(out_dir, stem)
    work_dir = os.path.join(doc_out, "_work")

    # 指纹校验:源或 DPI 变 → 清空全新跑
    manifest = cp.load_manifest(work_dir)
    if manifest is None or not cp.fingerprint_ok(manifest, pdf_path, dpi):
        if manifest is not None:
            print(f"[textbooks] 指纹失配(源或DPI变),清空 {work_dir} 全新跑")
        cp.reset_work_dir(work_dir)
        manifest = cp.new_manifest(pdf_path, cp.pdf_fingerprint(pdf_path), dpi, route)
        cp.save_manifest(work_dir, manifest)

    # 毒页 startup 解析:上次进程崩在某页且已达硬尝试上限 → 标 process-killed
    cp.resolve_poison(manifest, work_dir)
    cp.save_manifest(work_dir, manifest)

    total = manifest["fingerprint"]["page_count"]
    poisoned = {f["page"] for f in manifest["failed_pages"]
                if f["kind"] == "process-killed"}
    todo = [p for p in cp.pages_todo(work_dir, total) if p not in poisoned]
    done = sum(1 for i in range(1, total + 1) if cp.is_page_done(work_dir, i))
    durations: list[float] = []
    for page in todo:
        t = time.time()
        cp.set_in_progress(manifest, page)   # predict 前留痕:进程硬崩后可检出毒页
        cp.save_manifest(work_dir, manifest)
        png = None
        try:
            png = pdf_page_to_png(pdf_path, page, work_dir, dpi=dpi)
            blocks = predict_page(png, work_dir)   # 非空时 engine 已落 res.json
            if not blocks and not cp.is_page_done(work_dir, page):
                cp.write_empty_page(work_dir, page)   # 空白页显式标记完成
        except Exception as e:                        # noqa: BLE001 坏页隔离
            cp.record_failure(manifest, page, f"{type(e).__name__}: {e}",
                              "page-exception")
        finally:
            if png and os.path.exists(png):
                os.remove(png)                        # 磁盘有界:predict 后即删
        cp.clear_in_progress(manifest)
        cp.save_manifest(work_dir, manifest)
        done += 1
        durations.append(time.time() - t)
        avg = sum(durations) / len(durations)
        eta_h = avg * (total - done) / 3600
        nfail = len(manifest["failed_pages"])
        print(f"[page {page}/{total}] {durations[-1]:.0f}s "
              f"(完成 {done} 失败 {nfail} ETA {eta_h:.1f}h)")

    # 陈旧失败清理 + 去重:已完成的页移除;同页多次失败只留最后一条(含 process-killed)
    dedup: dict[int, dict] = {}
    for f in manifest["failed_pages"]:
        if not cp.is_page_done(work_dir, f["page"]):
            dedup[f["page"]] = f
    manifest["failed_pages"] = list(dedup.values())

    # 从检查点重组(每次运行都做,部分完成也产出部分 md)
    md, all_blocks = assemble(work_dir, total)
    os.makedirs(doc_out, exist_ok=True)
    md_path = os.path.join(doc_out, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    cp.save_manifest(work_dir, manifest)
    return {"route": route, "md_path": md_path, "selfcheck": check,
            "failed_pages": manifest["failed_pages"]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_convert.py -v`
Expected: PASS（Task5 的 9 个 + 本任务 3 个 = 12）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/convert.py scripts/pipelines/textbooks/tests/test_convert.py
git commit -m "feat(textbooks): convert in_progress 断点 + 毒页 startup 跳过"
```

---

## Task 7: watchdog — 子进程反复拉起直到跑完

**Files:**
- Create: `scripts/pipelines/textbooks/watchdog.py`
- Test: `scripts/pipelines/textbooks/tests/test_watchdog.py`（新）

**Interfaces:**
- Consumes: `cp.MAX_RESTARTS`
- Produces:
  - `run_until_done(argv: list[str], max_restarts: int = cp.MAX_RESTARTS, runner=None) -> int`
    （`runner(argv) -> int` 默认用 `subprocess.run` 跑 `python -m scripts.pipelines.textbooks.convert argv`；返回最终退出码：0=跑完，1=超重启上限）
  - `main() -> None`（CLI：`--src`/`--out`/`--dpi`/`--max-restarts`，透传前三者给 convert）

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_watchdog.py`：

```python
from scripts.pipelines.textbooks import watchdog as wd


def test_stops_on_success_first_try():
    calls = []
    def runner(argv):
        calls.append(argv)
        return 0
    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=5, runner=runner)
    assert rc == 0
    assert len(calls) == 1          # 一次成功,不重启


def test_restarts_until_success():
    seq = [1, 1, 0]                 # 崩两次,第三次成功
    def runner(argv):
        return seq.pop(0)
    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=5, runner=runner)
    assert rc == 0


def test_gives_up_over_max_restarts():
    def runner(argv):
        return 1                    # 永远崩
    rc = wd.run_until_done(["--src", "x.pdf"], max_restarts=3, runner=runner)
    assert rc == 1                  # 兜底放弃


def test_counts_restarts_not_first_run():
    calls = []
    def runner(argv):
        calls.append(1)
        return 1
    wd.run_until_done(["--src", "x.pdf"], max_restarts=3, runner=runner)
    # 首跑 + 3 次重启 = 4 次调用
    assert len(calls) == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_watchdog.py -v`
Expected: FAIL（`ModuleNotFoundError: ... watchdog`）

- [ ] **Step 3: Implement**

创建 `watchdog.py`：

```python
"""无人值守 supervisor:子进程反复拉起 convert,进程崩了自动续跑,直到跑完或超上限。"""
from __future__ import annotations

import argparse
import subprocess
import sys

from scripts.pipelines.textbooks import checkpoint as cp


def _default_runner(argv: list[str]) -> int:
    cmd = [sys.executable, "-m", "scripts.pipelines.textbooks.convert", *argv]
    return subprocess.run(cmd).returncode


def run_until_done(argv: list[str], max_restarts: int = cp.MAX_RESTARTS,
                   runner=None) -> int:
    """跑 convert;返回 0 成功;非 0(进程崩)则重启续跑,超 max_restarts 放弃返回 1。"""
    runner = runner or _default_runner
    rc = runner(argv)
    restarts = 0
    while rc != 0:
        if restarts >= max_restarts:
            print(f"[watchdog] 超过 {max_restarts} 次重启仍未跑完,放弃。")
            return 1
        restarts += 1
        print(f"[watchdog] convert 进程退出码 {rc},第 {restarts} 次重启续跑...")
        rc = runner(argv)
    print("[watchdog] convert 跑完(exit 0)。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 无人值守转换(崩溃自动续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--max-restarts", type=int, default=cp.MAX_RESTARTS,
                    help="累计重启兜底上限")
    args = ap.parse_args()
    argv = ["--src", args.src, "--dpi", str(args.dpi)]
    if args.out:
        argv += ["--out", args.out]
    rc = run_until_done(argv, max_restarts=args.max_restarts)
    sys.exit(rc)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_watchdog.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/watchdog.py scripts/pipelines/textbooks/tests/test_watchdog.py
git commit -m "feat(textbooks): watchdog 子进程反复拉起直到跑完(真·无人值守)"
```

---

## Task 8: 全套回归 + README 更新

**Files:**
- Modify: `scripts/pipelines/textbooks/README.md`
- Test: 全套

- [ ] **Step 1: 跑全套测试**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v`
Expected: 原有测试 + 本次新增全绿（checkpoint / preprocess / convert / watchdog）

- [ ] **Step 2: README 补大文件用法**

在 `README.md` 的用法段落追加（贴合现有 README 风格）：

```markdown
## 大文件 / 无人值守

单本大部头(700+ 页)转换耗时以小时计(本机 ~50s/页@DPI150),支持断点续跑与磁盘有界:

- 逐页流式:任一时刻临时目录仅 1 张 PNG,检查点为 `_work/page_NNNN_res.json`(每页)。
- 断点续跑:重跑同命令自动跳过已完成页;PDF 内容或 `--dpi` 变则自动清空重跑(防混合精度)。
- 坏页隔离:单页异常记入 manifest `failed_pages`,不影响其它页。
- 无人值守:用 `watchdog.py` 反复拉起 convert,进程级崩溃(CUDA/驱动/OOM)自动续跑直到跑完。

```bash
# 单趟(可续跑,手动重跑接着走)
.venv-textbooks/Scripts/python -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --dpi 150
# 无人值守(崩了自动续跑)
.venv-textbooks/Scripts/python -m scripts.pipelines.textbooks.watchdog --src book.pdf --out ./out
```
```

- [ ] **Step 3: Commit**

```bash
git add scripts/pipelines/textbooks/README.md
git commit -m "docs(textbooks): README 补大文件续跑/无人值守用法"
```

---

## Self-Review

**Spec coverage：**
- §3.1 工作目录布局 → Task5（`doc_out/_work` 结构、md 落 doc_out）✓
- §3.2 逐页流式 + 空白页检查点 → Task5（流式循环、`write_empty_page`）✓
- §3.3 断点续跑 + 指纹(含DPI) + 半截 json → Task2(`fingerprint_ok`)、Task3(`is_page_done` try/except)、Task5(失配清空、resume 跳过)✓
- §3.4 坏页隔离 → Task5（try/except + `record_failure` kind=page-exception）✓
- §3.5 从检查点重组 + 自检 → Task5（`assemble` + block_coverage/katex）✓
- §3.6 进度反馈 → Task5（每页 ETA 打印）✓
- §3.7 看门狗 + 毒页检测 → Task6（in_progress/resolve_poison）、Task7（watchdog）✓
- §4 manifest 结构（fingerprint/dpi/failed_pages/in_progress/restarts）→ Task2/4 ✓
- §5 CLI 双入口 + --dpi → Task5（convert --dpi）、Task7（watchdog）✓
- §6 测试全项 → Task2-7 各测试用例逐条对应 ✓

**Placeholder scan：** 无 TBD/TODO；每步含完整代码与命令。✓

**Type consistency：** `checkpoint` API 名称在 Task2-4 定义、Task5-7 消费一致（`fingerprint_ok`/`pages_todo`/`set_in_progress`/`resolve_poison`/`load_page_blocks`/`write_empty_page`）；`predict_page` 在 convert 里被 import 且测试 monkeypatch `cv.predict_page`（名称一致）；res.json 结构 `{"parsing_res_list": [...]}` 全程统一。✓

**发现并已内联修正：** Task6 补了"`todo` 排除 process-killed 毒页"逻辑，否则毒页(无 res.json)会被 `pages_todo` 反复纳入重跑——已在 Task6 Step3 显式处理。
