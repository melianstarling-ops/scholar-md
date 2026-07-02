# textbooks batch.py 外部工作区调用 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 textbooks 管线加一个批量入口 `batch.py`，让所有者一次性丢多本 PDF（文件/目录/多个）进去，
逐本以独立 watchdog 子进程无人值守跑完，产物落到任意外部目录，支持 `--resume`/`--limit`/`--list`。

**Architecture:** `batch.py` 同进程内顺序编排：对每本发现的 PDF 构造 `convert.py` 的 argv，调用
`watchdog.run_until_done(argv, max_restarts=..., runner=...)`（子进程崩溃隔离/自动续跑，逐本独立）。
拿不到 Python 返回值，跑完后从磁盘读回 `manifest.json`/`<stem>_selfcheck.json` 拼汇总报告——为此先给
`convert_pdf()` 加 `write_selfcheck` 参数（对齐 `convert_patent.py` 现有模式），并让 `watchdog.py` 透传该 CLI flag。

**Tech Stack:** Python 3.11、pytest、PyMuPDF(fitz)（既有 textbooks 管线技术栈，不引入新依赖）。

## Global Constraints

- 环境变量名精确为 `SCHOLARMD_TEXTBOOKS_SRC`（`--src` 省略时回退，再回退仓库内 `02_Source/textbooks/`）。
- `--out` 省略时默认根为独立的 `03_Output/textbooks/`（不是"就地"——与单文件 `convert.py` 的默认不同，见设计 §9）。
- 不做 `--flat`：每本书始终落 `<out_root>/<stem>/`（`convert_pdf()` 现有行为不变）。
- 返回码语义：仅 GIVEUP（watchdog 达 `--max-restarts` 仍未跑完）计入失败、整体返回 1；SUSPECT（有
  `failed_pages` 但 `rc==0`）不影响返回码。
- `--limit` 在 `--resume` 过滤**之前**应用于 `discover()` 返回的原始列表（截断游标，非"接下来 N 本未完成"）。
- 跨 `--src` 目录同名 stem（不同内容）→ `discover()` 直接抛异常，`main()` 捕获后返回 1、不处理任何一本书。
- `--resume` 跳过判断：B 路（born-digital 登记）**不设跳过短路**，每次都重新走一遍 triage+登记（便宜、幂等、
  自动解决源文件被替换的过期问题）；A/C 路必须先过 `cp.fingerprint_ok(manifest, pdf_path, dpi)`（源/DPI
  变了不算 done），再看 `pages_todo()` 排除 `kind=process-killed`（毒页）后是否为空——**不要求**
  `not manifest["failed_pages"]`（毒页不算未完成，`page-exception` 瞬时失败页仍算未完成、允许重试）。
- `convert_pdf(pdf_path, out_dir=None, dpi=cp.DEFAULT_DPI, write_selfcheck=True)`：路由 A/C 完成后默认写
  `<stem>_selfcheck.json`；路由 B 不受影响。

设计文档：`docs/superpowers/specs/2026-07-02-textbooks-batch-design.md`。

**任务粒度说明：** Task 1 是纯机械的"加个条件判断/加一行 append"（`write_selfcheck` 参数落盘 +
`watchdog.py` 透传该 flag），测试失败的原因显而易见，不走"先确认测试失败"的单独步骤——测试与实现一起写、
跑一次确认通过即可。Task 2（`discover()` 去重+撞名检测）与 Task 3（`_already_done()` 指纹/毒页判断）是本计划
里唯一有真逻辑、边界情况容易出错的部分，保留完整 TDD 红绿闭环。Task 4（编排循环）逻辑分支较多，同样保留完整闭环。

---

### Task 1: selfcheck.json 落盘 —— `convert_pdf()` 参数 + `watchdog.py` 透传

**Files:**
- Modify: `scripts/pipelines/textbooks/convert.py`
- Modify: `scripts/pipelines/textbooks/watchdog.py`
- Test: `scripts/pipelines/textbooks/tests/test_convert.py`
- Test: `scripts/pipelines/textbooks/tests/test_watchdog.py`

**Interfaces:**
- Consumes: 无新依赖，仅在既有 `convert.py`/`watchdog.py` 内加参数/透传。
- Produces: `convert_pdf(pdf_path, out_dir=None, dpi=cp.DEFAULT_DPI, write_selfcheck=True) -> dict`（新增
  `write_selfcheck` 关键字参数，其余签名/返回值不变）；`write_selfcheck=True`（默认）时在
  `doc_out/<stem>_selfcheck.json` 写入与返回值里 `selfcheck` 字段相同的 JSON。`convert.py` CLI 新增
  `--no-selfcheck-json`；`watchdog.py` CLI 同名新增，指定时原样追加到传给 `run_until_done` 的 argv。
  Task 4（`batch.py`）依赖这个 CLI flag 名字原样透传。

- [ ] **Step 1: 写测试 + 实现（一起做，机械改动，跳过单独的"确认先失败"步骤）**

追加到 `scripts/pipelines/textbooks/tests/test_convert.py` 末尾：

```python
def test_convert_writes_selfcheck_json_by_default(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    selfcheck_path = os.path.join(str(tmp_path / "out"), "scan", "scan_selfcheck.json")
    assert os.path.exists(selfcheck_path)
    with open(selfcheck_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == res["selfcheck"]


def test_convert_no_selfcheck_json_when_disabled(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100, write_selfcheck=False)
    selfcheck_path = os.path.join(str(tmp_path / "out"), "scan", "scan_selfcheck.json")
    assert not os.path.exists(selfcheck_path)


def test_convert_cli_no_selfcheck_json_forwards_flag(monkeypatch):
    captured = {}
    def fake_convert_pdf(pdf_path, out_dir, dpi=150, write_selfcheck=True):
        captured["write_selfcheck"] = write_selfcheck
        return {"route": "A", "md_path": "x.md",
                "selfcheck": {"total": 0, "in_md": 0, "missing": []}, "failed_pages": []}
    monkeypatch.setattr(cv, "convert_pdf", fake_convert_pdf)
    monkeypatch.setattr("sys.argv", ["convert.py", "--src", "x.pdf", "--no-selfcheck-json"])
    cv.main()
    assert captured["write_selfcheck"] is False
```

追加到 `scripts/pipelines/textbooks/tests/test_watchdog.py` 顶部加 `import pytest`，文件末尾追加：

```python
def test_main_forwards_no_selfcheck_json_flag(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf", "--no-selfcheck-json"])
    with pytest.raises(SystemExit) as exc:
        wd.main()
    assert exc.value.code == 0
    assert "--no-selfcheck-json" in captured["argv"]


def test_main_omits_no_selfcheck_json_flag_by_default(monkeypatch):
    captured = {}
    def fake_run_until_done(argv, max_restarts):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(wd, "run_until_done", fake_run_until_done)
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--src", "x.pdf"])
    with pytest.raises(SystemExit):
        wd.main()
    assert "--no-selfcheck-json" not in captured["argv"]
```

在 `scripts/pipelines/textbooks/convert.py` 顶部导入区（第 4 行 `import argparse` 之后）插入：

```python
import json
```

（此时导入区变为 `import argparse` / `import json` / `import os` / `import time`，字母序。）

把函数签名（原第 37-38 行）：

```python
def convert_pdf(pdf_path: str, out_dir: str | None = None,
                dpi: int = cp.DEFAULT_DPI) -> dict:
```

改为：

```python
def convert_pdf(pdf_path: str, out_dir: str | None = None,
                dpi: int = cp.DEFAULT_DPI, write_selfcheck: bool = True) -> dict:
```

把收尾部分（原第 115-119 行）：

```python
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    cp.save_manifest(work_dir, manifest)
    return {"route": route, "md_path": md_path, "selfcheck": check,
            "failed_pages": manifest["failed_pages"]}
```

改为：

```python
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    if write_selfcheck:
        selfcheck_path = os.path.join(doc_out, stem + "_selfcheck.json")
        with open(selfcheck_path, "w", encoding="utf-8") as f:
            json.dump(check, f, ensure_ascii=False, indent=2)
    cp.save_manifest(work_dir, manifest)
    return {"route": route, "md_path": md_path, "selfcheck": check,
            "failed_pages": manifest["failed_pages"]}
```

把 `convert.py` 的 `main()`（原第 122-137 行）：

```python
def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 单文档转换(可续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    args = ap.parse_args()
    res = convert_pdf(args.src, args.out, dpi=args.dpi)
```

改为：

```python
def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 单文档转换(可续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--no-selfcheck-json", action="store_true",
                    help="不写 <stem>_selfcheck.json(控制台摘要仍输出)")
    args = ap.parse_args()
    res = convert_pdf(args.src, args.out, dpi=args.dpi,
                      write_selfcheck=not args.no_selfcheck_json)
```

把 `scripts/pipelines/textbooks/watchdog.py` 的 `main()`（原第 33-45 行）：

```python
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
```

改为：

```python
def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 无人值守转换(崩溃自动续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--max-restarts", type=int, default=cp.MAX_RESTARTS,
                    help="累计重启兜底上限")
    ap.add_argument("--no-selfcheck-json", action="store_true",
                    help="不写 <stem>_selfcheck.json(转发给 convert.py)")
    args = ap.parse_args()
    argv = ["--src", args.src, "--dpi", str(args.dpi)]
    if args.out:
        argv += ["--out", args.out]
    if args.no_selfcheck_json:
        argv.append("--no-selfcheck-json")
    rc = run_until_done(argv, max_restarts=args.max_restarts)
    sys.exit(rc)
```

- [ ] **Step 2: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_convert.py scripts/pipelines/textbooks/tests/test_watchdog.py -v --basetemp=./.pytest_tmp`
Expected: 全部 PASS（含既有用例，回归全绿）。

- [ ] **Step 3: Commit**

```bash
git add scripts/pipelines/textbooks/convert.py scripts/pipelines/textbooks/watchdog.py \
        scripts/pipelines/textbooks/tests/test_convert.py scripts/pipelines/textbooks/tests/test_watchdog.py
git commit -m "feat(textbooks): convert_pdf 落盘 selfcheck.json + watchdog 透传 --no-selfcheck-json"
```

---

### Task 2: `batch.py` 骨架 + `discover()`（自适应展开 + 跨目录同名硬失败）

**Files:**
- Create: `scripts/pipelines/textbooks/batch.py`
- Create: `scripts/pipelines/textbooks/tests/test_batch.py`

**Interfaces:**
- Consumes: 无（纯路径展开，不依赖 checkpoint/watchdog）。
- Produces: `discover(src_paths: list[str]) -> list[Path]`（去重排序，跨目录同名 stem 冲突抛
  `ValueError`）；模块常量 `PROJECT_ROOT`、`DEFAULT_SOURCE_ROOT`、`DEFAULT_OUTPUT_ROOT`（均为 `Path`）。
  Task 3/4 在此文件基础上继续加函数。

- [ ] **Step 1: 写失败测试**

创建 `scripts/pipelines/textbooks/tests/test_batch.py`：

```python
import pytest

from scripts.pipelines.textbooks import batch as bp


def test_discover_dir_and_file_mixed_dedup(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    a = d / "A.pdf"
    a.write_bytes(b"%PDF-1.4")
    b = d / "B.pdf"
    b.write_bytes(b"%PDF-1.4")
    result = bp.discover([str(d), str(a)])   # 目录+文件混用,a 只应出现一次
    assert result == [a, b]


def test_discover_skips_non_pdf(tmp_path, capsys):
    d = tmp_path / "src"
    d.mkdir()
    (d / "notes.txt").write_text("x", encoding="utf-8")
    result = bp.discover([str(d / "notes.txt")])
    assert result == []
    assert "跳过" in capsys.readouterr().err


def test_discover_cross_dir_stem_collision_raises(tmp_path):
    d1 = tmp_path / "s1"
    d1.mkdir()
    d2 = tmp_path / "s2"
    d2.mkdir()
    (d1 / "A.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "A.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="跨目录同名"):
        bp.discover([str(d1), str(d2)])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_batch.py -v --basetemp=./.pytest_tmp`
Expected: FAIL（`ModuleNotFoundError`/`ImportError`：`batch.py` 不存在）。

- [ ] **Step 3: 实现**

创建 `scripts/pipelines/textbooks/batch.py`：

```python
"""batch.py — textbooks 管线批量入口(自适应输入/输出,watchdog 子进程隔离)。

用法:
    python -m scripts.pipelines.textbooks.batch --src <dir_or_pdf> [...] --out <dir>
    python -m scripts.pipelines.textbooks.batch --list
    python -m scripts.pipelines.textbooks.batch --resume --max-restarts 80

--src 省略 → 回退 env SCHOLARMD_TEXTBOOKS_SRC → 仓库内 02_Source/textbooks/。
--out 省略 → 仓库内 03_Output/textbooks/(独立产物根,与单文件 convert.py"--out 省略=就地"不同)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_TEXTBOOKS_SRC", str(PROJECT_ROOT / "02_Source" / "textbooks"))
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "textbooks"


def discover(src_paths: list[str]) -> list[Path]:
    """把 --src(文件/目录/多个)展开成去重排序的 PDF 路径列表。

    跨目录同名 stem(不同路径、同文件名)会导致 out_root/<stem>/ 下的检查点互相清空打架,
    属正确性问题,检出即抛 ValueError,调用方(main)应捕获后整批不处理直接返回非零。
    """
    pdfs: list[Path] = []
    seen: set[Path] = set()
    stem_sources: dict[str, Path] = {}
    for sp in src_paths:
        p = Path(sp).resolve()
        if p.is_dir():
            candidates = sorted(p.glob("*.pdf"))
        elif p.is_file() and p.suffix.lower() == ".pdf":
            candidates = [p]
        else:
            print(f"  跳过(既非 PDF 文件也非目录): {p}", file=sys.stderr)
            continue
        for pdf in candidates:
            if pdf in seen:
                continue
            seen.add(pdf)
            if pdf.stem in stem_sources and stem_sources[pdf.stem] != pdf:
                raise ValueError(
                    f"跨目录同名 stem 冲突: '{pdf.stem}' 同时来自 "
                    f"{stem_sources[pdf.stem]} 和 {pdf}"
                )
            stem_sources[pdf.stem] = pdf
            pdfs.append(pdf)
    return pdfs
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_batch.py -v --basetemp=./.pytest_tmp`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/batch.py scripts/pipelines/textbooks/tests/test_batch.py
git commit -m "feat(textbooks): batch.py 骨架 + discover()(自适应展开,跨目录同名硬失败)"
```

---

### Task 3: `_already_done()`（`--resume` 判断：指纹/DPI 校验 + 毒页感知）

**Files:**
- Modify: `scripts/pipelines/textbooks/batch.py`
- Test: `scripts/pipelines/textbooks/tests/test_batch.py`

**Interfaces:**
- Consumes: `checkpoint.py` 的 `load_manifest(work_dir: str) -> dict | None`、
  `fingerprint_ok(manifest: dict, pdf_path: str, dpi: int) -> bool`、
  `pages_todo(work_dir: str, total: int) -> list[int]`、`pdf_fingerprint`、`new_manifest`、`save_manifest`、
  `record_failure`（均已存在，无需改动）。
- Produces: `_already_done(out_root: Path, pdf_path: Path, dpi: int) -> bool`。Task 4 的 `run()` 用它做
  `--resume` 跳过判断。

- [ ] **Step 1: 写失败测试**

追加到 `scripts/pipelines/textbooks/tests/test_batch.py`（文件顶部补 `import json` 和
`import fitz`，以及 `from scripts.pipelines.textbooks import checkpoint as cp`）：

```python
import json

import fitz

from scripts.pipelines.textbooks import checkpoint as cp


def _make_pdf(tmp_path, n_pages, name="book"):
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / f"{name}.pdf"
    doc.save(str(p))
    return p


def _mark_page_done(work: Path, page: int, content: str = "x") -> None:
    work.mkdir(parents=True, exist_ok=True)
    with open(cp.page_res_path(str(work), page), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "text", "block_content": content}]}, f)


def test_already_done_false_when_no_manifest(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    assert bp._already_done(tmp_path / "out", pdf, 150) is False


def test_already_done_true_when_all_pages_done(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    _mark_page_done(work, 2)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 150) is True


def test_already_done_false_on_dpi_mismatch(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 200) is False   # 请求 DPI 200 ≠ 记录 150


def test_already_done_false_on_source_replaced(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    # 同名文件被替换成不同页数(指纹变了)
    doc = fitz.open()
    doc.new_page(); doc.new_page(); doc.new_page()
    doc.save(str(pdf))
    assert bp._already_done(out_root, pdf, 150) is False


def test_already_done_true_when_only_poisoned_page_remains(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.record_failure(m, 2, "process killed repeatedly", "process-killed")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 150) is True     # 毒页不算"未完成"


def test_already_done_false_when_page_exception_pending(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    work = out_root / pdf.stem / "_work"
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.record_failure(m, 2, "transient", "page-exception")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, pdf, 150) is False    # 瞬时失败页仍算未完成,允许重试
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_batch.py -k already_done -v --basetemp=./.pytest_tmp`
Expected: FAIL（`AttributeError: module 'batch' has no attribute '_already_done'`）。

- [ ] **Step 3: 实现**

在 `scripts/pipelines/textbooks/batch.py` 顶部导入区加一行（`from pathlib import Path` 之后）：

```python
from scripts.pipelines.textbooks import checkpoint as cp
```

在 `discover()` 函数之后追加：

```python
def _already_done(out_root: Path, pdf_path: Path, dpi: int) -> bool:
    """--resume 跳过判断:B 路(born-digital 登记)不走这个函数,由 main 直接不做短路
    (triage 便宜、幂等,见设计 §6)。这里只判 A/C 路:指纹/DPI 失配不算 done;
    毒页(process-killed)不算"未完成"(convert_pdf 自己也不会再碰它),
    但瞬时失败页(page-exception)仍算未完成,允许下次 --resume 重试。
    """
    work_dir = out_root / pdf_path.stem / "_work"
    manifest = cp.load_manifest(str(work_dir))
    if manifest is None:
        return False
    if not cp.fingerprint_ok(manifest, str(pdf_path), dpi):
        return False
    total = manifest["fingerprint"]["page_count"]
    poisoned = {f["page"] for f in manifest["failed_pages"] if f["kind"] == "process-killed"}
    todo = [p for p in cp.pages_todo(str(work_dir), total) if p not in poisoned]
    return not todo
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_batch.py -v --basetemp=./.pytest_tmp`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/batch.py scripts/pipelines/textbooks/tests/test_batch.py
git commit -m "feat(textbooks): batch.py _already_done() —— 指纹/DPI 校验 + 毒页感知的 resume 判断"
```

---

### Task 4: `run()` / `main()`（编排循环 + 汇总报告 + CLI）

**Files:**
- Modify: `scripts/pipelines/textbooks/batch.py`
- Test: `scripts/pipelines/textbooks/tests/test_batch.py`

**Interfaces:**
- Consumes: Task 2 的 `discover()`、Task 3 的 `_already_done()`、`watchdog.run_until_done(argv, max_restarts, runner=None) -> int`（已存在，`runner` 参数已支持注入）、`checkpoint.load_manifest`。
- Produces: `run(src_paths, out=None, dpi=cp.DEFAULT_DPI, resume=False, limit=None, max_restarts=cp.MAX_RESTARTS, no_selfcheck_json=False, runner=None) -> tuple[int, list[dict]]`；
  `main() -> int`（CLI 入口，`if __name__ == "__main__": sys.exit(main())`）。

- [ ] **Step 1: 写失败测试**

追加到 `scripts/pipelines/textbooks/tests/test_batch.py`：

```python
def test_run_calls_watchdog_once_per_book(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    (d / "B.pdf").write_bytes(b"%PDF-1.4")
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), runner=fake_runner)
    assert rc == 0
    assert len(calls) == 2
    assert [r["stem"] for r in results] == ["A", "B"]


def test_run_reports_giveup_and_nonzero_rc(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    def fake_runner(argv):
        return 1   # 永远崩
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), max_restarts=2, runner=fake_runner)
    assert rc == 1
    assert results[0]["status"] == "GIVEUP"


def test_run_resume_skips_done_book_without_spawning(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    pdf = d / "A.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(pdf))
    out_root = tmp_path / "out"
    work = out_root / "A" / "_work"
    _mark_page_done(work, 1)
    cp.save_manifest(str(work),
                     cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), cp.DEFAULT_DPI, "A"))
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(out_root), resume=True, runner=fake_runner)
    assert calls == []
    assert results[0]["status"] == "SKIP"


def test_run_limit_truncates_before_resume(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    for name in ("A", "B", "C"):
        (d / f"{name}.pdf").write_bytes(b"%PDF-1.4")
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), limit=1, runner=fake_runner)
    assert len(results) == 1
    assert results[0]["stem"] == "A"


def test_main_list_flag_prints_pdfs_and_returns_zero(tmp_path, monkeypatch, capsys):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr("sys.argv", ["batch.py", "--src", str(d), "--list"])
    rc = bp.main()
    assert rc == 0
    assert "A.pdf" in capsys.readouterr().out


def test_main_returns_nonzero_on_stem_collision(tmp_path, monkeypatch):
    d1 = tmp_path / "s1"
    d1.mkdir()
    d2 = tmp_path / "s2"
    d2.mkdir()
    (d1 / "A.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "A.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr("sys.argv", ["batch.py", "--src", str(d1), str(d2)])
    rc = bp.main()
    assert rc == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_batch.py -v --basetemp=./.pytest_tmp`
Expected: 新增用例 FAIL（`AttributeError: module 'batch' has no attribute 'run'`/`'main'`）。

- [ ] **Step 3: 实现**

把 `scripts/pipelines/textbooks/batch.py` 顶部导入区（Task 2/3 累积后的状态）：

```python
from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts.pipelines.textbooks import checkpoint as cp
```

改为：

```python
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.watchdog import run_until_done
```

在文件末尾（`_already_done()` 之后）追加：

```python
def _job_argv(pdf: Path, out_root: Path, dpi: int, no_selfcheck_json: bool) -> list[str]:
    argv = ["--src", str(pdf), "--out", str(out_root), "--dpi", str(dpi)]
    if no_selfcheck_json:
        argv.append("--no-selfcheck-json")
    return argv


def _read_summary(out_root: Path, pdf: Path) -> dict:
    """跑完一本书(rc==0)后从磁盘读回结构化结果,供汇总报告用(拿不到 Python 返回值)。"""
    deferred_marker = out_root / "_deferred_born_digital" / f"{pdf.stem}.txt"
    if deferred_marker.exists():
        return {"stem": pdf.stem, "status": "B", "route": "B",
                "failed_pages": 0, "selfcheck": None}
    work_dir = out_root / pdf.stem / "_work"
    manifest = cp.load_manifest(str(work_dir))
    failed_pages = manifest["failed_pages"] if manifest else []
    route = manifest["route"] if manifest else "?"
    selfcheck_path = out_root / pdf.stem / f"{pdf.stem}_selfcheck.json"
    selfcheck = None
    if selfcheck_path.exists():
        with open(selfcheck_path, encoding="utf-8") as f:
            selfcheck = json.load(f)
    status = "SUSPECT" if failed_pages else "OK"
    return {"stem": pdf.stem, "status": status, "route": route,
            "failed_pages": len(failed_pages), "selfcheck": selfcheck}


def run(src_paths: list[str], out: str | None = None, dpi: int = cp.DEFAULT_DPI,
        resume: bool = False, limit: int | None = None,
        max_restarts: int = cp.MAX_RESTARTS, no_selfcheck_json: bool = False,
        runner=None) -> tuple[int, list[dict]]:
    pdfs = discover(src_paths)
    if limit:
        pdfs = pdfs[:limit]
    out_root = Path(out).resolve() if out else DEFAULT_OUTPUT_ROOT
    out_root.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    n_giveup = 0
    for pdf in pdfs:
        if resume and _already_done(out_root, pdf, dpi):
            print(f"  [SKIP] {pdf.stem}")
            results.append({"stem": pdf.stem, "status": "SKIP"})
            continue
        argv = _job_argv(pdf, out_root, dpi, no_selfcheck_json)
        rc = run_until_done(argv, max_restarts=max_restarts, runner=runner)
        if rc != 0:
            n_giveup += 1
            print(f"  [GIVEUP] {pdf.stem}")
            results.append({"stem": pdf.stem, "status": "GIVEUP"})
            continue
        summary = _read_summary(out_root, pdf)
        results.append(summary)
        if summary["status"] == "B":
            print(f"  [B] {pdf.stem} — 已登记 deferred")
        else:
            cov = ""
            if summary["selfcheck"]:
                c = summary["selfcheck"]
                cov = f" coverage={c['in_md']}/{c['total']}"
            print(f"  [{summary['status']}] {pdf.stem} — route={summary['route']} "
                  f"failed_pages={summary['failed_pages']}{cov}")

    n_ok = sum(1 for r in results if r["status"] in ("OK", "B"))
    n_suspect = sum(1 for r in results if r["status"] == "SUSPECT")
    n_skip = sum(1 for r in results if r["status"] == "SKIP")
    print(f"\n{'=' * 56}\n批处理完成: {n_ok} OK/B / {n_suspect} SUSPECT / "
          f"{n_giveup} GIVEUP / {n_skip} SKIP → {out_root}")
    return (1 if n_giveup else 0), results


def main() -> int:
    ap = argparse.ArgumentParser(description="textbooks 批量入口(自适应 --src/--out,watchdog 子进程隔离)")
    ap.add_argument("--src", nargs="*", default=None,
                    help="PDF 文件/目录/多个;省略回退 env SCHOLARMD_TEXTBOOKS_SRC 或仓库 02_Source/textbooks/")
    ap.add_argument("--out", default=None, help="产物根目录(省略=仓库 03_Output/textbooks/)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--resume", action="store_true", help="跳过已全部跑完的书")
    ap.add_argument("--limit", type=int, default=None, help="只处理发现列表的前 N 本(调试/小样验证)")
    ap.add_argument("--max-restarts", type=int, default=cp.MAX_RESTARTS,
                    help="透传给每本书 watchdog 的累计重启上限")
    ap.add_argument("--no-selfcheck-json", action="store_true", help="不写 <stem>_selfcheck.json")
    ap.add_argument("--list", action="store_true", help="只列出待处理 PDF,不转换")
    args = ap.parse_args()

    src_paths = args.src if args.src else [str(DEFAULT_SOURCE_ROOT)]
    try:
        if args.list:
            pdfs = discover(src_paths)
            if args.limit:
                pdfs = pdfs[:args.limit]
            for p in pdfs:
                print(f"  {p}")
            print(f"共 {len(pdfs)} 份 @ {src_paths}")
            return 0
        rc, _ = run(src_paths, out=args.out, dpi=args.dpi, resume=args.resume,
                    limit=args.limit, max_restarts=args.max_restarts,
                    no_selfcheck_json=args.no_selfcheck_json)
        return rc
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_batch.py -v --basetemp=./.pytest_tmp`
Expected: 全部 PASS。

再跑整个 textbooks 测试套件确认无回归：

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v --basetemp=./.pytest_tmp`
Expected: 全部 PASS（既有 67 + 本计划新增用例）。

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/batch.py scripts/pipelines/textbooks/tests/test_batch.py
git commit -m "feat(textbooks): batch.py run()/main() —— 编排循环 + 汇总报告 + CLI(--resume/--limit/--list)"
```

---

## Plan Self-Review

**Spec coverage**（对照 `2026-07-02-textbooks-batch-design.md`）：
- §2 进程隔离模型 → Task 4 `run()` 用 `run_until_done(..., runner=runner)`。✓
- §3 目录布局(不做 --flat) → 全程 `out_root / pdf.stem`，未新增平铺分支。✓
- §4 selfcheck.json 落盘 → Task 1(`convert_pdf` + `watchdog.py` 透传)。✓
- §5 CLI 全部 flag(`--src/--out/--dpi/--resume/--limit/--max-restarts/--no-selfcheck-json/--list`) → Task 4 `main()`。✓
- §5 跨目录同名 stem 硬失败 → Task 2 `discover()` + Task 4 `main()` 捕获 `ValueError`。✓
- §6 `--resume` 三条判断(B 不短路/指纹校验/毒页豁免) → Task 3 `_already_done()`；B 不短路体现在 `run()`
  只对 A/C 走 `_already_done`，从不检查 `_deferred_born_digital` 标记做跳过。✓
- §6 `--limit` 先于 `--resume` → Task 4 `run()` 先 `pdfs[:limit]` 再进循环判断跳过。✓
- §7 汇总报告 + rc 语义(仅 GIVEUP→1) → Task 4 `run()` 尾部统计 + 返回值。✓
- §8 测试策略(注入假 runner，全程无真子进程/GPU) → Task 4 测试全部用 `fake_runner`；Task 1 机械改动
  精简为"测试+实现一起写、跑一次确认通过"，逻辑风险低，经所有者确认按此粒度执行。✓
- §9 范围外(`--flat`/`--review`/`--ocr`/就地默认) → 计划全程未引入。✓

**Placeholder 扫描**：全部步骤含完整代码块，无 TBD/"适当处理"/"参考 Task N 类似实现"。

**类型一致性**：`_already_done(out_root: Path, pdf_path: Path, dpi: int) -> bool` 在 Task 3 定义、Task 4
`run()` 内以 `_already_done(out_root, pdf, dpi)` 原样调用，参数顺序/类型一致。`run()` 返回
`tuple[int, list[dict]]`，Task 4 测试解包 `rc, results = bp.run(...)` 与 `main()` 内 `rc, _ = run(...)` 一致。
`discover()` 返回 `list[Path]`，Task 3/4 测试与实现均以 `Path` 对象操作（`.stem`），无 str/Path 混用。
