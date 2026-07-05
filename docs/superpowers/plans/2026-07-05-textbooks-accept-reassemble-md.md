# textbooks 采纳后落 .md（reassemble）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** debug 审核里点"采纳"后，修正能进入最终 `doc_dir/stem.md`——通过复用转换管线唯一的 `assemble()`，在启动/翻页/完成/CLI 多点幂等触发重组。

**Architecture:** 新增薄函数 `convert.reassemble_md` 复用 `assemble()` 覆盖写 `stem.md`（幂等）；`debug_view` 加 `/reassemble` 路由 + `dirty` 门控（`handle_post` 纯函数可测）+ `serve` 层 `threading.Lock` 串行化 + 启动即对账 + `--reassemble` CLI；前端翻页 ping + "完成同步"按钮。正式管线 `convert_pdf` 与 `vision_repair` 不动。

**Tech Stack:** Python 3（stdlib `http.server` / `threading` / `argparse`）、pytest、原生 JS（debug_assets/app.js）。

## Global Constraints

- 人工确认门红线：只有 `status=="accepted"` 才落进 md（`apply_corrections` 已保证，不放宽）。
- 不改 `vision_repair.py`；不改 `convert_pdf` 现有流程；不写/不碰 `stem_selfcheck.json`。
- `_work/*_res.json` 只读；`02_Source/` 只读。
- 所有文本文件 UTF-8 读写。
- 测试命令一律用仓库根目录下：`.venv-textbooks/Scripts/python.exe -m pytest ...`。基线：`247 passed`，每步后保持全绿。
- 每个 commit：中文 conventional message，结尾附 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- `reassemble_md` 幂等：相同 blocks + 相同 corrections 覆盖写逐字相同 md。
- 对外操作（装依赖/merge/push）前须所有者确认；本计划不含这些。

---

### Task 1: `convert.reassemble_md` 核心函数

**Files:**
- Modify: `scripts/pipelines/textbooks/convert.py`（在 `assemble()` 之后、`_register_deferred` 之前新增函数）
- Test: `scripts/pipelines/textbooks/tests/test_convert.py`（文件末尾追加）

**Interfaces:**
- Consumes: 既有 `assemble(work_dir, total, stem, assets_dir, pdf_path, dpi) -> dict`（返回含 `"md"`）；`checkpoint.load_manifest(work_dir) -> dict | None`。
- Produces: `reassemble_md(doc_dir: str, pdf_path: str | None, dpi: int) -> str | None`——写 `doc_dir/stem.md` 并返回其路径；manifest 缺失/total 为 0 时返回 `None`（不写、不抛）。

- [ ] **Step 1: 写失败测试（4 个）**

在 `tests/test_convert.py` 末尾追加：

```python
def test_reassemble_md_applies_accepted(tmp_path):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint
    doc_dir = tmp_path / "scan"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "display_formula", "block_id": 5,
             "block_content": original}]}, f)
    cp.save_manifest(str(work), cp.new_manifest(
        "x.pdf", {"page_count": 1, "size_bytes": 0}, 100, "A"))
    corrections = {"stem": "scan", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "accepted"}]}
    with open(doc_dir / "scan_corrections.json", "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    md_path = cv.reassemble_md(str(doc_dir), pdf_path=None, dpi=100)

    assert md_path == str(doc_dir / "scan.md")
    md = open(md_path, encoding="utf-8").read()
    assert "good" in md and "bad" not in md


def test_reassemble_md_does_not_apply_pending(tmp_path):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint
    doc_dir = tmp_path / "scan"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "display_formula", "block_id": 5,
             "block_content": original}]}, f)
    cp.save_manifest(str(work), cp.new_manifest(
        "x.pdf", {"page_count": 1, "size_bytes": 0}, 100, "A"))
    corrections = {"stem": "scan", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(doc_dir / "scan_corrections.json", "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    md_path = cv.reassemble_md(str(doc_dir), pdf_path=None, dpi=100)

    md = open(md_path, encoding="utf-8").read()
    assert "bad" in md and "good" not in md


def test_reassemble_md_idempotent(tmp_path):
    doc_dir = tmp_path / "scan"
    work = doc_dir / "_work"
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(str(work), 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "text",
             "block_content": "hello page 1"}]}, f)
    cp.save_manifest(str(work), cp.new_manifest(
        "x.pdf", {"page_count": 1, "size_bytes": 0}, 100, "A"))

    p1 = cv.reassemble_md(str(doc_dir), pdf_path=None, dpi=100)
    first = open(p1, encoding="utf-8").read()
    p2 = cv.reassemble_md(str(doc_dir), pdf_path=None, dpi=100)
    second = open(p2, encoding="utf-8").read()

    assert first == second


def test_reassemble_md_returns_none_when_no_manifest(tmp_path):
    doc_dir = tmp_path / "scan"
    os.makedirs(doc_dir, exist_ok=True)   # 无 _work / 无 manifest
    assert cv.reassemble_md(str(doc_dir), pdf_path=None, dpi=100) is None
    assert not (doc_dir / "scan.md").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_convert.py -k reassemble -v`
Expected: FAIL —— `AttributeError: module 'convert' has no attribute 'reassemble_md'`。

- [ ] **Step 3: 实现 `reassemble_md`**

在 `scripts/pipelines/textbooks/convert.py` 中，紧接 `assemble()` 定义之后插入：

```python
def reassemble_md(doc_dir: str, pdf_path: str | None, dpi: int) -> str | None:
    """幂等对账:读 _work 检查点 → 应用采纳的修正 → 重组 → 覆盖写 doc_dir/stem.md。
    复用 convert_pdf 用的同一个 assemble(),保证 debug 采纳出的 md 与正式转换逐字一致。
    只写 md,不写 selfcheck、不动 manifest。manifest 缺失/total 为 0 时返回 None。"""
    doc_dir = os.path.abspath(doc_dir)
    stem = os.path.basename(os.path.normpath(doc_dir))
    work_dir = os.path.join(doc_dir, "_work")
    assets_dir = os.path.join(doc_dir, stem + ".assets")
    manifest = cp.load_manifest(work_dir)
    if not manifest:
        return None
    total = manifest["fingerprint"]["page_count"]
    if not total:
        return None
    result = assemble(work_dir, total, stem, assets_dir, pdf_path, dpi)
    md_path = os.path.join(doc_dir, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(result["md"])
    return md_path
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_convert.py -k reassemble -v`
Expected: 4 passed。

- [ ] **Step 5: 全套回归 + 提交**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/ -q`
Expected: `251 passed`（247 基线 + 4 新）。

```bash
git add scripts/pipelines/textbooks/convert.py scripts/pipelines/textbooks/tests/test_convert.py
git commit -F - <<'EOF'
feat(textbooks): convert.reassemble_md 幂等重组落 md(复用 assemble)

只写 md、不碰 selfcheck/manifest;manifest 缺失或 total=0 返回 None。
供 debug 采纳后落盘复用,保证与正式 convert 产物一致。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: `handle_post` 加 `/reassemble` 路由 + dirty 门控

**Files:**
- Modify: `scripts/pipelines/textbooks/debug_view.py:167-183`（`handle_post` 签名与函数体）
- Test: `scripts/pipelines/textbooks/tests/test_debug_view.py`（文件末尾追加）

**Interfaces:**
- Consumes: `convert.reassemble_md`（Task 1）作为默认回调；既有 `set_correction_status`。
- Produces: `handle_post(doc_dir, stem, path, body, state=None, reassemble_fn=None) -> tuple[int, bytes]`。
  - `/corrections` 成功 → 若 `state is not None` 置 `state["dirty"] = True`。
  - `/reassemble` → 若 `state` 存在且 `state.get("dirty")` 且 `reassemble_fn`：调 `reassemble_fn()`、清 `state["dirty"]`；总是回 `200 b"ok"`。
  - 新参数带默认值，不破坏既有 3 个 `handle_post` 测试（4 位置参数调用）。

- [ ] **Step 1: 写失败测试（3 个）**

在 `tests/test_debug_view.py` 末尾追加：

```python
def test_handle_post_reassemble_runs_when_dirty(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    calls = []
    state = {"dirty": True}
    status, body = dv.handle_post(
        str(doc_dir), "book", "/reassemble", "",
        state=state, reassemble_fn=lambda: calls.append(1))
    assert status == 200
    assert calls == [1]                 # dirty → 跑
    assert state["dirty"] is False      # 跑完清脏


def test_handle_post_reassemble_skips_when_clean(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    calls = []
    state = {"dirty": False}
    status, body = dv.handle_post(
        str(doc_dir), "book", "/reassemble", "",
        state=state, reassemble_fn=lambda: calls.append(1))
    assert status == 200
    assert calls == []                  # 无脏 → 秒回不跑


def test_handle_post_corrections_sets_dirty(tmp_path):
    doc_dir = tmp_path / "book"
    os.makedirs(doc_dir, exist_ok=True)
    with open(doc_dir / "book_corrections.json", "w", encoding="utf-8") as f:
        _json.dump({"stem": "book", "corrections": [
            {"page": 1, "block_id": 5, "status": "pending"}]}, f)
    state = {"dirty": False}
    status, body = dv.handle_post(
        str(doc_dir), "book", "/corrections",
        _json.dumps({"page": 1, "block_id": 5, "status": "accepted"}),
        state=state)
    assert status == 200
    assert state["dirty"] is True       # 采纳成功 → 置脏
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_view.py -k "reassemble or sets_dirty" -v`
Expected: FAIL —— `handle_post` 不接受 `state`/`reassemble_fn` 关键字参数（`TypeError`）。

- [ ] **Step 3: 改 `handle_post`**

把 `scripts/pipelines/textbooks/debug_view.py` 的 `handle_post` 替换为：

```python
def handle_post(doc_dir: str, stem: str, path: str, body: str,
                state: dict | None = None, reassemble_fn=None) -> tuple[int, bytes]:
    """POST 路由(纯函数便于单测)。
    `/corrections`:采纳/驳回 → set_correction_status;成功则置 state["dirty"]。
    `/reassemble`:dirty 才调 reassemble_fn 落 md、清脏(否则秒回)——落盘幂等,门控只为省。
    其它路径:沿用标注流程落 <stem>_annotations.json。返回 (status_code, response_body)。"""
    if path == "/corrections":
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return 400, b"bad json"
        try:
            ok = set_correction_status(doc_dir, data["page"], data["block_id"], data["status"])
        except (KeyError, ValueError) as e:
            return 400, str(e).encode("utf-8")
        if ok and state is not None:
            state["dirty"] = True
        return (200, b"ok") if ok else (404, b"not found")
    if path == "/reassemble":
        if state is not None and state.get("dirty") and reassemble_fn is not None:
            reassemble_fn()
            state["dirty"] = False
        return 200, b"ok"
    with open(os.path.join(doc_dir, stem + "_annotations.json"), "w", encoding="utf-8") as f:
        f.write(body)
    return 200, b"ok"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_view.py -v`
Expected: 全部 passed（含既有 3 个 handle_post 测试仍绿 —— 它们不传 state，`/corrections` 分支 `state is None` 跳过置脏）。

- [ ] **Step 5: 全套回归 + 提交**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/ -q`
Expected: `254 passed`（251 + 3 新）。

```bash
git add scripts/pipelines/textbooks/debug_view.py scripts/pipelines/textbooks/tests/test_debug_view.py
git commit -F - <<'EOF'
feat(textbooks): debug handle_post 加 /reassemble 路由 + dirty 门控

采纳成功置脏;/reassemble 脏才跑 reassemble_fn 落 md、跑完清脏。
新参数带默认值,不破坏既有 handle_post 测试。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: `serve` 层 threading.Lock + 启动即对账

**Files:**
- Modify: `scripts/pipelines/textbooks/debug_view.py`（新增模块函数 `_safe_reassemble`；改 `serve`）
- Test: `scripts/pipelines/textbooks/tests/test_debug_view.py`（末尾追加）

**Interfaces:**
- Consumes: `convert.reassemble_md`（Task 1）；`handle_post`（Task 2）。
- Produces: `_safe_reassemble(doc_dir, pdf_path, dpi, reassemble_fn=None) -> str | None`——包 try/except，异常时返回 None 不抛（供启动对账容错）。`serve` 内建 `threading.Lock` 串行化 `do_POST`，并在服务前调 `_safe_reassemble` 对账。

- [ ] **Step 1: 写失败测试（2 个）**

在 `tests/test_debug_view.py` 末尾追加：

```python
def test_safe_reassemble_swallows_exception(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("assemble boom")
    out = dv._safe_reassemble(str(tmp_path), pdf_path=None, dpi=100, reassemble_fn=boom)
    assert out is None                  # 异常被吞,返回 None、不抛


def test_safe_reassemble_returns_path_on_success(tmp_path):
    def ok(doc_dir, pdf_path, dpi):
        return "MD_PATH"
    out = dv._safe_reassemble(str(tmp_path), pdf_path=None, dpi=100, reassemble_fn=ok)
    assert out == "MD_PATH"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_view.py -k safe_reassemble -v`
Expected: FAIL —— `module 'debug_view' has no attribute '_safe_reassemble'`。

- [ ] **Step 3: 加 `_safe_reassemble` + 改 imports/serve**

在 `debug_view.py` 顶部 import 区补上（与既有 import 同段）：

```python
import threading

from scripts.pipelines.textbooks.convert import reassemble_md
```

在 `handle_post` 之前（或 `serve` 之前）新增：

```python
def _safe_reassemble(doc_dir: str, pdf_path: str | None, dpi: int,
                     reassemble_fn=None) -> str | None:
    """调 reassemble 落 md,异常只告警不抛(启动对账/后台落盘不掀翻服务)。"""
    fn = reassemble_fn or reassemble_md
    try:
        return fn(doc_dir, pdf_path, dpi)
    except Exception as e:                                     # noqa: BLE001
        print(f"[debug_view] reassemble 失败(忽略,不影响审核):{e}", flush=True)
        return None
```

把 `serve()` 替换为（新增 `state`/`lock`、启动对账、POST 串行化 + 注入回调）：

```python
def serve(doc_dir: str, pdf_path: str | None, dpi: int, img_dpi: int, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    stem = os.path.basename(os.path.normpath(doc_dir))
    img_cache: dict = {}
    state = {"dirty": False}
    lock = threading.Lock()

    _safe_reassemble(doc_dir, pdf_path, dpi)     # 启动即对账:打开审核界面就把 md 同步到 json

    def reassemble_fn():
        _safe_reassemble(doc_dir, pdf_path, dpi)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默
            pass

        def do_GET(self):
            s, pages = build_payloads(doc_dir, pdf_path, dpi, img_dpi, True, img_cache)
            html = render_html(s, pages, serve=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8")
            with lock:                                # 串行化:杜绝并发写同一 stem.md 的竞态
                status, resp = handle_post(doc_dir, stem, self.path, body,
                                           state=state, reassemble_fn=reassemble_fn)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(resp)

    print(f"[debug_view] serve http://127.0.0.1:{port}/  (Ctrl-C 停)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_view.py -k safe_reassemble -v`
Expected: 2 passed。

- [ ] **Step 5: 全套回归 + 提交**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/ -q`
Expected: `256 passed`（254 + 2 新）。

```bash
git add scripts/pipelines/textbooks/debug_view.py scripts/pipelines/textbooks/tests/test_debug_view.py
git commit -F - <<'EOF'
feat(textbooks): debug serve 加 lock 串行化 + 启动即对账

一把 threading.Lock 把 POST(置脏/check-run-clear)串行化,杜绝并发写同一 md;
serve 启动先跑一次 _safe_reassemble 对账(容错、失败不阻断服务)。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: CLI `--reassemble` 子命令

**Files:**
- Modify: `scripts/pipelines/textbooks/debug_view.py`（`main()` 的 argparse 与分支）
- Test: `scripts/pipelines/textbooks/tests/test_debug_view.py`（末尾追加）

**Interfaces:**
- Consumes: `convert.reassemble_md`（经 `_safe_reassemble`）；既有 `_resolve_pdf`。
- Produces: `python -m scripts.pipelines.textbooks.debug_view --doc <stem-dir> --reassemble` → 落 md、打印路径。无 UI 收尾与 §6 回填共用此入口。

- [ ] **Step 1: 写失败测试**

在 `tests/test_debug_view.py` 末尾追加：

```python
def test_cli_reassemble_calls_reassemble(tmp_path, monkeypatch):
    doc_dir = tmp_path / "scan"
    os.makedirs(doc_dir, exist_ok=True)
    captured = {}
    def fake_reassemble(d, pdf_path, dpi):
        captured["doc_dir"] = d
        return str(doc_dir / "scan.md")
    monkeypatch.setattr(dv, "reassemble_md", fake_reassemble)
    monkeypatch.setattr(dv.cp, "load_manifest",
                        lambda w: {"dpi": 100, "pdf_path": None})
    monkeypatch.setattr("sys.argv",
                        ["debug_view.py", "--doc", str(doc_dir), "--reassemble"])
    dv.main()
    assert captured["doc_dir"] == os.path.abspath(str(doc_dir))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_view.py -k cli_reassemble -v`
Expected: FAIL —— 无 `--reassemble` 参数（argparse 报错）或 `reassemble_md` 未被调用。

- [ ] **Step 3: 改 `main()`**

在 `debug_view.py::main()` 的 argparse 段（`--no-images` 之后）加：

```python
    ap.add_argument("--reassemble", action="store_true",
                    help="幂等重组:应用已采纳修正,覆盖写 <stem>.md 后退出(无 UI 收尾/回填)")
```

现有 `main()` 顶部已算好 `pdf_path` 与 `dpi`（`pdf_path, mdpi = _resolve_pdf(...)` / `dpi = args.dpi or mdpi`）。在 `dpi = args.dpi or mdpi` 之后、`if args.collect:` 之前插入分支，直接复用它们（勿重复调 `_resolve_pdf`）：

```python
    if args.reassemble:
        md_path = _safe_reassemble(doc_dir, pdf_path, dpi)
        print(f"[debug_view] reassemble → {md_path}")
        return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_debug_view.py -k cli_reassemble -v`
Expected: 1 passed。

- [ ] **Step 5: 全套回归 + 提交**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/ -q`
Expected: `257 passed`（256 + 1 新）。

```bash
git add scripts/pipelines/textbooks/debug_view.py scripts/pipelines/textbooks/tests/test_debug_view.py
git commit -F - <<'EOF'
feat(textbooks): debug_view 加 --reassemble CLI(无 UI 收尾/回填入口)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: 前端 —— 翻页 ping + "同步到 md"按钮/快捷键

**Files:**
- Modify: `scripts/pipelines/textbooks/debug_assets/app.js`（`gotoIndex` 内加 ping；新增按钮触发 + 快捷键）
- Modify: `scripts/pipelines/textbooks/debug_assets/template.html`（若工具栏按钮是静态 HTML）或 `app.js`（若按钮由 JS 注入）——落地时按现有工具栏构建方式二选一，保持与既有 `R`/`E` 按钮同一套路。

**说明:** 前端无 pytest 覆盖,靠手动验证(Step 3)。改动前先 Grep 现有 `gotoIndex`、键盘事件绑定(既有 `R`/`E` 快捷键)与工具栏按钮的构建位置,复用同一模式。

- [ ] **Step 1: 定位现有模式**

Run（在仓库根）：
```
grep -n "gotoIndex" scripts/pipelines/textbooks/debug_assets/app.js
grep -n "addEventListener('keydown'" scripts/pipelines/textbooks/debug_assets/app.js
grep -n "'R'\|'E'\|key ===" scripts/pipelines/textbooks/debug_assets/app.js
grep -n "corrections\|fetch(" scripts/pipelines/textbooks/debug_assets/app.js
```
读出：`gotoIndex` 定义处、既有采纳/驳回 `fetch('/corrections'...)` 的写法、键盘分发处、工具栏按钮注入处。按钮/快捷键复用这套。

- [ ] **Step 2: 加翻页 ping**

在 `gotoIndex(...)` 函数体末尾（页面切换完成后）加：

```javascript
  // 翻页时把已采纳修正落进 md(后端 dirty 门控:无改动则秒回、不空跑)
  fetch('/reassemble', { method: 'POST' }).catch(() => {});
```

- [ ] **Step 3: 加"同步到 md"按钮 + 快捷键（页尾兜底）**

在既有键盘分发处（`R`/`E` 同款）加一个键（如 `S`），并在工具栏加一个按钮，二者都触发：

```javascript
function syncToMd() {
  fetch('/reassemble', { method: 'POST' })
    .then(() => flash('已同步到 md'))   // flash: 复用现有的角标/提示函数;若无则 console.log
    .catch(() => {});
}
```

- 键盘分发：`if (e.key === 's' || e.key === 'S') { syncToMd(); }`（与既有 `R`/`E` 分支并列，注意不要与输入框聚焦冲突——沿用既有守卫）。
- 工具栏按钮：`<button id="syncmd" title="同步已采纳修正到 md (S)">同步 md</button>`，`document.getElementById('syncmd').addEventListener('click', syncToMd);`。
- 若现有代码没有 `flash` 类提示函数，则 `syncToMd` 内改为 `.then(() => {})` 并可选 `console.log('synced')`，不新造 UI 组件（YAGNI）。

- [ ] **Step 4: 手动验证（真浏览器）**

```
.venv-textbooks/Scripts/python.exe -m scripts.pipelines.textbooks.debug_view \
  --doc "03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan" --serve
```
浏览器打开 `http://127.0.0.1:8078/`，按以下核对（devtools Network 面板看 `/reassemble` 请求）：
1. 采纳某页一条 pending → 翻到下一页 → 应看到一次 `POST /reassemble` 200；关服务后 `git diff --stat` 或看 `Paul_p1-100_scan.md` mtime 确认 md 已更新。
2. 停在最后一页采纳一条 → 按 `S`（或点"同步 md"按钮）→ 看到 `POST /reassemble` 200，md 更新。
3. 不采纳只翻页 → `/reassemble` 200 但后端不重跑（md mtime 不变或内容一致，dirty 门控生效）。

- [ ] **Step 5: 提交**

```bash
git add scripts/pipelines/textbooks/debug_assets/app.js scripts/pipelines/textbooks/debug_assets/template.html
git commit -F - <<'EOF'
feat(textbooks): debug 前端翻页 ping /reassemble + "同步 md"按钮/快捷键

翻页自动落盘(后端 dirty 门控);页尾用 S 键/按钮手动兜底,根治最后一页采纳不落。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: 一次性数据回填（现存 Paul）

**Files:**
- 数据：`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan.md`（被覆盖写）

**说明:** 非 TDD，是一次数据对账操作。Task 3 的启动即对账已能在 `--serve` 打开时自动完成；此处用 CLI 显式跑一次并核对。

- [ ] **Step 1: 记录当前状态（回填前）**

Run:
```
.venv-textbooks/Scripts/python.exe -c "md=open('03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan.md',encoding='utf-8').read(); print('54/15 in md:', 'lc = -\\\\mu \\\\frac{\\\\displaystyle\\\\pm\\\\int' in md)"
```
Expected（回填前）：`False`（4 条之一尚未落盘）。

- [ ] **Step 2: 跑 CLI 回填**

Run:
```
.venv-textbooks/Scripts/python.exe -m scripts.pipelines.textbooks.debug_view \
  --doc "03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan" --reassemble
```
Expected：打印 `[debug_view] reassemble → ...Paul_p1-100_scan.md`。

- [ ] **Step 3: 核对 4 条已落盘**

Run:
```
.venv-textbooks/Scripts/python.exe -c "
import json
base='03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/'
md=open(base+'Paul_p1-100_scan.md',encoding='utf-8').read()
d=json.load(open(base+'Paul_p1-100_scan_corrections.json',encoding='utf-8'))
missing=[(c['page'],c['block_id']) for c in d['corrections']
         if c['status']=='accepted' and c['corrected_latex'].strip().strip('\$').strip()[:40] not in md]
print('MISSING after backfill:', missing)
"
```
Expected：`MISSING after backfill: []`（13 条全部在 md 里）。

- [ ] **Step 4: 提交回填结果**

```bash
git add "03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan.md"
git commit -F - <<'EOF'
chore(textbooks): 回填 Paul 4 条已采纳修正进 md(50/3,53/20,54/6,54/15)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## 关联 follow-up（不在本计划范围，待所有者决定）

交接文档 §1.2/§3.4 措辞订正（把"禁一切打包"收窄为"仅禁 L-T31 合成图打包"，分清 L-T30/L-T31）——纯文档、`vision_repair.py` 不改。见 spec §9。不在本计划任务内。

## Self-Review

- **Spec 覆盖**:§3 数据流 → Task 1(reassemble_md)+Task 2(/reassemble+dirty)+Task 3(启动对账);§4.1 → Task 1;§4.2 → Task 2/3;§4.3 → Task 5;§4.2 CLI → Task 4;§6 回填 → Task 6;§5 取舍(锁/门控/多点触发)→ Task 2/3;§7 限制(锁不单测)→ Task 3 说明 + Step 用 devtools 手动核并发路径;§8 测试 → Task 1/2/3/4 的测试步骤;§9 follow-up → 明确排除在计划外。全部有落点。
- **占位符扫描**:无 TBD/TODO;所有代码步骤含完整代码;Task 5 前端因无自动测试改为"定位现有模式 + 手动验证"并给出确切 grep 与验证脚本,非占位。
- **类型/命名一致**:`reassemble_md(doc_dir, pdf_path, dpi)` 在 Task 1 定义,Task 3(`_safe_reassemble` 内 `fn(doc_dir, pdf_path, dpi)`)、Task 4 调用签名一致;`handle_post(..., state=None, reassemble_fn=None)` 在 Task 2 定义,Task 3 `serve` 调用一致;`state` 字典键 `"dirty"` 全程一致;测试计数递增 247→251→254→256→257 连贯。
