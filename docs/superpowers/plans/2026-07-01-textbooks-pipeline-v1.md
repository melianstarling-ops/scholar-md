# textbooks 管线首版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把无/低质文本层的扫描教科书单份 PDF 转成 Typora 结构 Markdown（PaddleOCR-VL 1.6 视觉识别 + 确定性重组）。

**Architecture:** 分诊(判文本层可信度) → 需 OCR 的走 PDF→PNG → PaddleOCR-VL predict 出 `parsing_res_list` JSON → 确定性 `reconstruct` 按 `block_order` 重组 Markdown（公式编号按 order 相邻绑定、页眉页脚按 order=None 剔除、着重号还原）→ Tier0 block 覆盖 lint → 输出 `<name>.md` + `<name>.assets/`。

**Tech Stack:** Python 3.12（`.venv-textbooks`）、paddlepaddle-gpu 3.2.1(cu126)、paddleocr[doc-parser] 3.7.0、PyMuPDF、pytest。

## Global Constraints

- Python 解释器固定用 `.venv-textbooks/Scripts/python.exe`（每管线独立环境，禁止用 `.venv`）。
- 引擎调用固定 `PaddleOCRVL(pipeline_version="v1.6")`。
- 新代码只落 `scripts/pipelines/textbooks/`，**不改 patents/general**。
- CLI 遵循 `--src`（文件/目录/多个）/`--out`（默认就地），对齐 AGENTS H.5。
- 扫描件必须先 `PDF→PNG`（`get_pixmap(dpi=200)`）再喂引擎（直喂扫描 pdf 会崩）。
- 输出 Typora 结构：`<doc_id>.md` + `<doc_id>.assets/`。
- 首版**单文档**跑通；分块(≤50页)/批量/Opus 审查/debug_view HTML 属后续，不在本计划。
- commit message 用中文，风格 `feat(textbooks): …`，结尾带 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

## File Structure

```
scripts/pipelines/textbooks/
  __init__.py
  triage.py         # 分诊:文本层覆盖度 + 质量 → A/B/C
  preprocess.py     # PDF → PNG(200dpi)
  engine.py         # PaddleOCR-VL 封装 → parsing_res_list(list[dict])
  reconstruct.py    # parsing_res_list → Markdown(核心自研)
  selfcheck.py      # Tier0 block 覆盖 lint
  convert.py        # 单文档编排 + CLI
  README.md
  requirements.txt
  tests/
    __init__.py
    fixtures/
      paul_p200_res.json      # 实测:英文扫描(18 blocks)
      jackson_p200_res.json   # 实测:中文扫描(8 blocks)
    test_triage.py
    test_reconstruct.py
    test_selfcheck.py
```

数据流依赖：`convert` 编排 `triage → preprocess → engine → reconstruct → selfcheck`。`reconstruct`/`selfcheck` 消费 `engine` 产出的 `parsing_res_list`（`list[dict]`，每 dict 含 `block_label`/`block_content`/`block_bbox`/`block_order`）。

---

### Task 1: 管线骨架 + 依赖 + 测试夹具

**Files:**
- Create: `scripts/pipelines/textbooks/__init__.py`（空）
- Create: `scripts/pipelines/textbooks/requirements.txt`
- Create: `scripts/pipelines/textbooks/README.md`
- Create: `scripts/pipelines/textbooks/tests/__init__.py`（空）
- Create: `scripts/pipelines/textbooks/tests/fixtures/paul_p200_res.json`（从会话暂存区拷贝实测产物）
- Create: `scripts/pipelines/textbooks/tests/fixtures/jackson_p200_res.json`

**Interfaces:**
- Produces: 两份 golden fixture JSON，后续 reconstruct/selfcheck 任务的测试输入。

- [ ] **Step 1: 建目录与空文件**

```bash
cd /d/Projects/Project_scholar-md
mkdir -p scripts/pipelines/textbooks/tests/fixtures
touch scripts/pipelines/textbooks/__init__.py scripts/pipelines/textbooks/tests/__init__.py
```

- [ ] **Step 2: 写 requirements.txt**

`scripts/pipelines/textbooks/requirements.txt`:
```
# textbooks 管线专属环境(.venv-textbooks)，勿装进 patents/general 的 .venv
# 安装:
#   .venv-textbooks/Scripts/python -m pip install paddlepaddle-gpu==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
#   .venv-textbooks/Scripts/python -m pip install -U "paddleocr[doc-parser]"
paddlepaddle-gpu==3.2.1
paddleocr[doc-parser]==3.7.0
PyMuPDF>=1.27
pytest>=8
```

装 pytest（若上面一行未生效）：`.venv-textbooks/Scripts/python -m pip install "pytest>=8"`

- [ ] **Step 3: 拷贝实测 json 作 golden fixture**

```bash
SCRATCH="C:/Users/hulia/AppData/Local/Temp/claude/d--Projects-Project-scholar-md/e30fd570-bcea-42ce-9b97-9c50a94b0266/scratchpad"
cp "$SCRATCH/paul_out/paul_p200_hd_res.json" scripts/pipelines/textbooks/tests/fixtures/paul_p200_res.json
cp "$SCRATCH/jackson_out/jackson_p200_hd_res.json" scripts/pipelines/textbooks/tests/fixtures/jackson_p200_res.json
```

- [ ] **Step 4: 写 README.md 骨架**

`scripts/pipelines/textbooks/README.md`:
```markdown
# textbooks 管线

扫描/教科书 PDF → Markdown（PaddleOCR-VL 1.6 + 确定性重组）。
设计见 `docs/superpowers/specs/2026-07-01-textbooks-pipeline-design.md`。

## 环境
独立 `.venv-textbooks`（勿混用 patents/general 的 .venv）。装 `requirements.txt`。

## 用法
    .venv-textbooks/Scripts/python scripts/pipelines/textbooks/convert.py --src <pdf> [--out <dir>]

## 首版范围
单文档、无/低质文本层扫描件走 OCR 主路。分块/批量/Opus 审查/HTML 复核待后续。
```

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks
git commit -m "feat(textbooks): 管线骨架 + 依赖清单 + 实测 golden fixture

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: triage — 文本层覆盖度闸

**Files:**
- Create: `scripts/pipelines/textbooks/triage.py`
- Test: `scripts/pipelines/textbooks/tests/test_triage.py`

**Interfaces:**
- Produces: `sample_text_coverage(pdf_path: str, sample: int = 5) -> float` — 采样若干页，返回每页平均可提取字符数。

- [ ] **Step 1: 写失败测试**

`scripts/pipelines/textbooks/tests/test_triage.py`:
```python
import fitz
from scripts.pipelines.textbooks.triage import sample_text_coverage


def _make_pdf(tmp_path, texts):
    doc = fitz.open()
    for t in texts:
        pg = doc.new_page()
        if t:
            pg.insert_text((72, 72), t)
    p = tmp_path / "x.pdf"
    doc.save(str(p))
    return str(p)


def test_coverage_zero_for_blank(tmp_path):
    pdf = _make_pdf(tmp_path, ["", "", ""])
    assert sample_text_coverage(pdf) == 0.0


def test_coverage_high_for_text(tmp_path):
    pdf = _make_pdf(tmp_path, ["hello world " * 20] * 3)
    assert sample_text_coverage(pdf) > 100
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_triage.py -v`
Expected: FAIL（`ModuleNotFoundError: triage` 或 `ImportError`）

- [ ] **Step 3: 实现 sample_text_coverage**

`scripts/pipelines/textbooks/triage.py`:
```python
"""输入分诊:按文本层可信度判 A(无层)/B(优质层)/C(低质层)。"""
from __future__ import annotations

import fitz


def sample_text_coverage(pdf_path: str, sample: int = 5) -> float:
    """采样均匀分布的若干页,返回每页平均可提取文本字符数。"""
    doc = fitz.open(pdf_path)
    n = doc.page_count
    if n == 0:
        return 0.0
    idxs = sorted({int(n * f) for f in (0.1, 0.3, 0.5, 0.7, 0.9)})[:sample]
    idxs = [min(i, n - 1) for i in idxs]
    total = sum(len(doc[i].get_text().strip()) for i in idxs)
    doc.close()
    return total / len(idxs)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_triage.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/triage.py scripts/pipelines/textbooks/tests/test_triage.py
git commit -m "feat(textbooks): triage 文本层覆盖度闸

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: triage — 质量闸 + A/B/C 路由

**Files:**
- Modify: `scripts/pipelines/textbooks/triage.py`
- Modify: `scripts/pipelines/textbooks/tests/test_triage.py`

**Interfaces:**
- Consumes: `sample_text_coverage`（Task 2）。
- Produces:
  - `text_badness(pdf_path: str, sample: int = 5) -> float` — 有文本层时的坏度分（0~1，越高越坏）。
  - `triage(pdf_path: str) -> str` — 返回 `"A"`（无层→OCR）/`"B"`（优质层→登记不转）/`"C"`（低质层→OCR）。

- [ ] **Step 1: 写失败测试**

追加到 `scripts/pipelines/textbooks/tests/test_triage.py`:
```python
from scripts.pipelines.textbooks.triage import text_badness, triage


def test_badness_low_for_clean(tmp_path):
    pdf = _make_pdf(tmp_path, ["the quick brown fox jumps over the lazy dog " * 10] * 3)
    assert text_badness(pdf) < 0.2


def test_badness_high_for_garbled(tmp_path):
    # 大量替换符/私用区字符 → 高坏度
    junk = "�CaSOS ringS �� " * 30
    pdf = _make_pdf(tmp_path, [junk] * 3)
    assert text_badness(pdf) > 0.3


def test_triage_A_for_blank(tmp_path):
    assert triage(_make_pdf(tmp_path, ["", "", ""])) == "A"


def test_triage_B_for_clean(tmp_path):
    assert triage(_make_pdf(tmp_path, ["the quick brown fox jumps " * 20] * 3)) == "B"


def test_triage_C_for_garbled(tmp_path):
    junk = "�� CaSOS " * 40
    assert triage(_make_pdf(tmp_path, [junk] * 3)) == "C"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_triage.py -v`
Expected: FAIL（`ImportError: cannot import name 'text_badness'`）

- [ ] **Step 3: 实现 text_badness + triage**

追加到 `scripts/pipelines/textbooks/triage.py`:
```python
COVERAGE_MIN = 50.0     # 每页平均字符 < 此 → 判无层(A)
BADNESS_MAX = 0.25      # 坏度 ≥ 此 → 判低质(C)

# 私用区(PUA)与替换符:文本层坏字形/CID 缺失的典型标志
_PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))


def _is_bad_char(ch: str) -> bool:
    o = ord(ch)
    if ch == "�":                       # replacement char
        return True
    return any(lo <= o <= hi for lo, hi in _PUA_RANGES)


def text_badness(pdf_path: str, sample: int = 5) -> float:
    """坏度分:采样文本中替换符/私用区字符占非空白字符的比例。"""
    doc = fitz.open(pdf_path)
    n = doc.page_count
    if n == 0:
        return 0.0
    idxs = sorted({int(n * f) for f in (0.1, 0.3, 0.5, 0.7, 0.9)})[:sample]
    idxs = [min(i, n - 1) for i in idxs]
    text = "".join(doc[i].get_text() for i in idxs)
    doc.close()
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for c in chars if _is_bad_char(c))
    return bad / len(chars)


def triage(pdf_path: str) -> str:
    """A=无层(OCR) / B=优质层(登记不转) / C=低质层(OCR)。"""
    if sample_text_coverage(pdf_path) < COVERAGE_MIN:
        return "A"
    return "C" if text_badness(pdf_path) >= BADNESS_MAX else "B"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_triage.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/triage.py scripts/pipelines/textbooks/tests/test_triage.py
git commit -m "feat(textbooks): triage 质量闸 + A/B/C 路由

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

> 注:`COVERAGE_MIN`/`BADNESS_MAX` 为初值,实现后期用真实样本标定(设计 §5.1)。非词率/中文词典等更强信号列 backlog,首版先用替换符/PUA 比例这一确定性信号。

---

### Task 4: reconstruct — 排序、过滤、基础分派（text / paragraph_title）

**Files:**
- Create: `scripts/pipelines/textbooks/reconstruct.py`
- Test: `scripts/pipelines/textbooks/tests/test_reconstruct.py`

**Interfaces:**
- Produces: `reconstruct_markdown(blocks: list[dict]) -> str` — 输入 `parsing_res_list`，输出 Markdown。本任务先处理排序/过滤/`text`/`paragraph_title`，公式与着重号在 Task 5/6 补齐。

- [ ] **Step 1: 写失败测试**

`scripts/pipelines/textbooks/tests/test_reconstruct.py`:
```python
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown


def test_drops_order_none_blocks():
    # header/number(page) 的 block_order 为 None,应被剔除
    blocks = [
        {"block_label": "header", "block_content": "PAGE HEADER", "block_order": None},
        {"block_label": "number", "block_content": "186", "block_order": None},
        {"block_label": "text", "block_content": "Body text.", "block_order": 1},
    ]
    md = reconstruct_markdown(blocks)
    assert "PAGE HEADER" not in md
    assert "186" not in md
    assert "Body text." in md


def test_sorts_by_order():
    blocks = [
        {"block_label": "text", "block_content": "second", "block_order": 2},
        {"block_label": "text", "block_content": "first", "block_order": 1},
    ]
    md = reconstruct_markdown(blocks)
    assert md.index("first") < md.index("second")


def test_paragraph_title_becomes_heading():
    blocks = [{"block_label": "paragraph_title", "block_content": "第五章 静磁学", "block_order": 1}]
    md = reconstruct_markdown(blocks)
    assert md.strip().startswith("## 第五章 静磁学")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: FAIL（`ModuleNotFoundError: reconstruct`）

- [ ] **Step 3: 实现基础 reconstruct**

`scripts/pipelines/textbooks/reconstruct.py`:
```python
"""parsing_res_list(PaddleOCR-VL) → Markdown 的确定性重组。"""
from __future__ import annotations


def reconstruct_markdown(blocks: list[dict]) -> str:
    """按 block_order 排序、剔除 order=None(页眉页脚页码)、逐块转 Markdown。"""
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    parts: list[str] = []
    for b in ordered:
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        if not content:
            continue
        if label == "paragraph_title":
            parts.append(f"## {content}")
        elif label == "text":
            parts.append(content)
        # display_formula / formula_number 在 Task 5 处理
    return "\n\n".join(parts) + "\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/reconstruct.py scripts/pipelines/textbooks/tests/test_reconstruct.py
git commit -m "feat(textbooks): reconstruct 排序/过滤/text/标题

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: reconstruct — 公式块 + 编号按 order 相邻绑定

**Files:**
- Modify: `scripts/pipelines/textbooks/reconstruct.py`
- Modify: `scripts/pipelines/textbooks/tests/test_reconstruct.py`

**Interfaces:**
- 扩展 `reconstruct_markdown`：处理 `display_formula`；其后若紧邻 `formula_number`（order 相邻），把编号以 `\tag{...}` 并入公式，`formula_number` 自身不再单独成行。

- [ ] **Step 1: 写失败测试**

追加到 `test_reconstruct.py`:
```python
def test_display_formula_binds_adjacent_number():
    blocks = [
        {"block_label": "display_formula",
         "block_content": r" $$ \mathbf{N}=\boldsymbol{\mu}\times\mathbf{B} $$ ", "block_order": 4},
        {"block_label": "formula_number", "block_content": "(5.1)", "block_order": 5},
    ]
    md = reconstruct_markdown(blocks)
    assert r"\tag{5.1}" in md          # 编号并入公式
    assert md.count("$$") == 2         # 只一个公式块
    assert "\n(5.1)" not in md         # 编号不再单独成行


def test_formula_number_without_formula_kept_inline():
    # 落单的 formula_number(前面不是公式) 保留为文本,不丢
    blocks = [
        {"block_label": "text", "block_content": "see below", "block_order": 1},
        {"block_label": "formula_number", "block_content": "(9.9)", "block_order": 2},
    ]
    md = reconstruct_markdown(blocks)
    assert "(9.9)" in md
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: FAIL（`\tag{5.1}` 不在输出）

- [ ] **Step 3: 重写 reconstruct 支持公式绑定**

替换 `scripts/pipelines/textbooks/reconstruct.py` 的 `reconstruct_markdown`:
```python
import re

_NUM_RE = re.compile(r"^\(?([\w.\-]+)\)?$")   # (5.30) / 5.30 → 5.30


def _formula_body(content: str) -> str:
    """去掉外层 $$ 包裹,取纯公式体。"""
    s = content.strip()
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2].strip()
    return s


def reconstruct_markdown(blocks: list[dict]) -> str:
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        b = ordered[i]
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        if not content:
            i += 1
            continue
        if label == "paragraph_title":
            parts.append(f"## {content}")
        elif label == "text":
            parts.append(content)
        elif label == "display_formula":
            body = _formula_body(content)
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            if nxt and nxt.get("block_label") == "formula_number":
                m = _NUM_RE.match((nxt.get("block_content") or "").strip())
                tag = m.group(1) if m else (nxt.get("block_content") or "").strip()
                parts.append(f"$$ {body} \\tag{{{tag}}} $$")
                i += 1                      # 吸收编号块
            else:
                parts.append(f"$$ {body} $$")
        elif label == "formula_number":
            parts.append(content)           # 落单编号,保留不丢
        i += 1
    return "\n\n".join(parts) + "\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/reconstruct.py scripts/pipelines/textbooks/tests/test_reconstruct.py
git commit -m "feat(textbooks): reconstruct 公式编号按 order 相邻绑定(\\tag)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: reconstruct — 中文着重号还原 + golden fixture 端到端

**Files:**
- Modify: `scripts/pipelines/textbooks/reconstruct.py`
- Modify: `scripts/pipelines/textbooks/tests/test_reconstruct.py`

**Interfaces:**
- 新增 `restore_emphasis_dots(text: str) -> str` — 把 `\underset{\cdot}{X}` 序列还原为纯文字 `X`（去着重号 LaTeX）。在 `text` 块内容上应用。
- golden 测试:用 Task 1 的 fixture 跑通整份 json → md,断言关键内容与剔除项。

- [ ] **Step 1: 写失败测试**

追加到 `test_reconstruct.py`:
```python
import json
from pathlib import Path
from scripts.pipelines.textbooks.reconstruct import restore_emphasis_dots

FIX = Path(__file__).parent / "fixtures"


def test_restore_emphasis_dots():
    s = r"根本差别：$ \underset{\cdot}{没}\underset{\cdot}{有}\underset{\cdot}{自}\underset{\cdot}{由} $。"
    out = restore_emphasis_dots(s)
    assert out == "根本差别：没有自由。"


def test_golden_jackson_chinese():
    blocks = json.loads((FIX / "jackson_p200_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md = reconstruct_markdown(blocks)
    assert md.strip().startswith("## 第五章")          # 标题
    assert r"\mathbf{N}=\boldsymbol{\mu}\times\mathbf{B}" in md  # 公式
    assert r"\tag{5.1}" in md                          # 编号绑定
    assert "186" not in md                             # 页码(order=None)剔除
    assert "underset" not in md                        # 着重号已还原


def test_golden_paul_english():
    blocks = json.loads((FIX / "paul_p200_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md = reconstruct_markdown(blocks)
    assert r"\tag{5.30}" in md
    assert r"\tag{5.33}" in md                          # 编号全部绑回(md 端到端曾丢失的)
    assert "THE PER-UNIT-LENGTH" not in md              # 页眉(order=None)剔除
    assert "178" not in md                              # 页码剔除
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: FAIL（`restore_emphasis_dots` 未定义；golden 里 `underset` 仍在）

- [ ] **Step 3: 实现着重号还原并接入**

在 `reconstruct.py` 顶部（`_NUM_RE` 附近）加:
```python
_EMPH_RE = re.compile(r"\\underset\{\\cdot\}\{([^{}]*)\}")
_EMPH_WRAP_RE = re.compile(r"\$\s*((?:\\underset\{\\cdot\}\{[^{}]*\}\s*)+)\$")


def restore_emphasis_dots(text: str) -> str:
    """把 \\underset{\\cdot}{X}…(常被整体裹进 $…$) 还原为纯文字 XYZ。"""
    def _unwrap(m):
        return _EMPH_RE.sub(r"\1", m.group(1))
    text = _EMPH_WRAP_RE.sub(_unwrap, text)     # 先解掉包裹的 $…$
    return _EMPH_RE.sub(r"\1", text)            # 再兜底裸露的
```

在 `reconstruct_markdown` 的 `text` 分支改为:
```python
        elif label == "text":
            parts.append(restore_emphasis_dots(content))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/reconstruct.py scripts/pipelines/textbooks/tests/test_reconstruct.py
git commit -m "feat(textbooks): reconstruct 着重号还原 + golden fixture 端到端

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: selfcheck — Tier0 block 覆盖 lint

**Files:**
- Create: `scripts/pipelines/textbooks/selfcheck.py`
- Test: `scripts/pipelines/textbooks/tests/test_selfcheck.py`

**Interfaces:**
- Produces: `block_coverage(blocks: list[dict], md: str) -> dict` — 返回 `{"total": int, "in_md": int, "missing": list[str]}`；`missing` 为有 order 但内容未出现在 md 的块摘要。扫描件无源文本层,故 Tier0 改用"每个有序块的内容都进了 md"。

- [ ] **Step 1: 写失败测试**

`scripts/pipelines/textbooks/tests/test_selfcheck.py`:
```python
from scripts.pipelines.textbooks.selfcheck import block_coverage


def test_all_ordered_blocks_covered():
    blocks = [
        {"block_label": "text", "block_content": "alpha beta", "block_order": 1},
        {"block_label": "header", "block_content": "IGNORED", "block_order": None},
    ]
    md = "alpha beta\n"
    rep = block_coverage(blocks, md)
    assert rep["total"] == 1          # order=None 不计
    assert rep["in_md"] == 1
    assert rep["missing"] == []


def test_detects_missing_block():
    blocks = [
        {"block_label": "text", "block_content": "present text", "block_order": 1},
        {"block_label": "text", "block_content": "LOST paragraph", "block_order": 2},
    ]
    md = "present text\n"
    rep = block_coverage(blocks, md)
    assert rep["in_md"] == 1
    assert any("LOST" in m for m in rep["missing"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_selfcheck.py -v`
Expected: FAIL（`ModuleNotFoundError: selfcheck`）

- [ ] **Step 3: 实现 block_coverage**

`scripts/pipelines/textbooks/selfcheck.py`:
```python
"""Tier0 确定性自检:扫描件无源文本层,改用 block 覆盖率(每个有序块都进了 md)。"""
from __future__ import annotations

import re


def _probe(content: str) -> str:
    """取块内容一段稳定的可检子串(去 LaTeX 包裹与空白,取前 12 个非空字符)。"""
    s = re.sub(r"[\s$]", "", content or "")
    return s[:12]


def block_coverage(blocks: list[dict], md: str) -> dict:
    ordered = [b for b in blocks if b.get("block_order") is not None]
    md_flat = re.sub(r"[\s$]", "", md)
    missing = []
    in_md = 0
    for b in ordered:
        probe = _probe(b.get("block_content", ""))
        if probe and probe in md_flat:
            in_md += 1
        else:
            missing.append((b.get("block_content") or "")[:40])
    return {"total": len(ordered), "in_md": in_md, "missing": missing}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_selfcheck.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/selfcheck.py scripts/pipelines/textbooks/tests/test_selfcheck.py
git commit -m "feat(textbooks): Tier0 block 覆盖 lint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: preprocess — PDF → PNG（200dpi）

**Files:**
- Create: `scripts/pipelines/textbooks/preprocess.py`
- Test: `scripts/pipelines/textbooks/tests/test_preprocess.py`

**Interfaces:**
- Produces: `pdf_to_pngs(pdf_path: str, out_dir: str, dpi: int = 200) -> list[str]` — 每页渲染为 PNG,返回有序路径列表。

- [ ] **Step 1: 写失败测试**

`scripts/pipelines/textbooks/tests/test_preprocess.py`:
```python
import os
import fitz
from scripts.pipelines.textbooks.preprocess import pdf_to_pngs


def test_pdf_to_pngs(tmp_path):
    doc = fitz.open()
    doc.new_page(); doc.new_page()
    pdf = tmp_path / "two.pdf"
    doc.save(str(pdf))
    out = tmp_path / "png"
    pngs = pdf_to_pngs(str(pdf), str(out), dpi=100)
    assert len(pngs) == 2
    assert all(os.path.exists(p) and p.endswith(".png") for p in pngs)
    assert pngs == sorted(pngs)      # 有序
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_preprocess.py -v`
Expected: FAIL（`ModuleNotFoundError: preprocess`）

- [ ] **Step 3: 实现 pdf_to_pngs**

`scripts/pipelines/textbooks/preprocess.py`:
```python
"""PDF → PNG 预处理(扫描件直喂引擎会崩,必须先栅格化)。"""
from __future__ import annotations

import os

import fitz


def pdf_to_pngs(pdf_path: str, out_dir: str, dpi: int = 200) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    paths = []
    for i in range(doc.page_count):
        pix = doc[i].get_pixmap(dpi=dpi)
        p = os.path.join(out_dir, f"page_{i + 1:04d}.png")
        pix.save(p)
        paths.append(p)
    doc.close()
    return paths
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/test_preprocess.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/preprocess.py scripts/pipelines/textbooks/tests/test_preprocess.py
git commit -m "feat(textbooks): preprocess PDF→PNG(200dpi)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: engine — PaddleOCR-VL 封装

**Files:**
- Create: `scripts/pipelines/textbooks/engine.py`

**Interfaces:**
- Consumes: PNG 路径列表（Task 8）。
- Produces: `predict_page(png_path: str, work_dir: str) -> list[dict]` — 对单页 PNG 跑 PaddleOCR-VL 1.6，返回该页 `parsing_res_list`（`list[dict]`）。惰性单例加载模型（避免每页重载）。

> 说明:本任务依赖 GPU + 已下载的 1.6 模型,是**集成**性质,不做纯单元测试(无法在无 GPU CI 上跑)。用一次真实 smoke 验证,不写进 pytest 默认集。

- [ ] **Step 1: 实现 engine（惰性单例 + save_to_json 取 parsing_res_list）**

`scripts/pipelines/textbooks/engine.py`:
```python
"""PaddleOCR-VL 1.6 封装:PNG → parsing_res_list。惰性单例,避免每页重载模型。"""
from __future__ import annotations

import json
import os

_PIPELINE = None


def _get_pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        from paddleocr import PaddleOCRVL
        _PIPELINE = PaddleOCRVL(pipeline_version="v1.6")
    return _PIPELINE


def predict_page(png_path: str, work_dir: str) -> list[dict]:
    """跑单页,落 <stem>_res.json,读回其 parsing_res_list。"""
    os.makedirs(work_dir, exist_ok=True)
    pipe = _get_pipeline()
    results = list(pipe.predict(png_path))
    if not results:
        return []
    results[0].save_to_json(save_path=work_dir)
    stem = os.path.splitext(os.path.basename(png_path))[0]
    jpath = os.path.join(work_dir, f"{stem}_res.json")
    with open(jpath, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("parsing_res_list", [])
```

- [ ] **Step 2: 真实 smoke 验证（一次性，非 pytest）**

Run:
```bash
SCRATCH="C:/Users/hulia/AppData/Local/Temp/claude/d--Projects-Project-scholar-md/e30fd570-bcea-42ce-9b97-9c50a94b0266/scratchpad"
.venv-textbooks/Scripts/python -c "import sys; sys.path.insert(0,'.'); from scripts.pipelines.textbooks.engine import predict_page; b=predict_page(r'$SCRATCH/jackson_p200_hd.png', r'$SCRATCH/eng_smoke'); print('blocks', len(b), 'labels', {x['block_label'] for x in b})"
```
Expected: 打印 `blocks 8`（或近似）+ 含 `paragraph_title/text/display_formula/formula_number` 等 label。

- [ ] **Step 3: Commit**

```bash
git add scripts/pipelines/textbooks/engine.py
git commit -m "feat(textbooks): engine PaddleOCR-VL 1.6 封装(惰性单例)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: convert — 单文档编排 + CLI

**Files:**
- Create: `scripts/pipelines/textbooks/convert.py`

**Interfaces:**
- Consumes: `triage`（Task 3）、`pdf_to_pngs`（Task 8）、`predict_page`（Task 9）、`reconstruct_markdown`（Task 6）、`block_coverage`（Task 7）。
- Produces: `convert_pdf(pdf_path: str, out_dir: str | None) -> dict` — 单文档端到端;返回 `{"route": "A|B|C", "md_path": str|None, "selfcheck": dict|None}`;CLI 入口 `--src/--out`。

- [ ] **Step 1: 实现 convert_pdf + CLI**

`scripts/pipelines/textbooks/convert.py`:
```python
"""单文档编排:分诊 → (A/C)PNG→predict→重组→自检 → Typora md。B 登记不转。"""
from __future__ import annotations

import argparse
import os
import tempfile

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_to_pngs
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown
from scripts.pipelines.textbooks.selfcheck import block_coverage


def convert_pdf(pdf_path: str, out_dir: str | None = None) -> dict:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(pdf_path))
    route = triage(pdf_path)
    if route == "B":
        deferred = os.path.join(out_dir, "_deferred_born_digital")
        os.makedirs(deferred, exist_ok=True)
        with open(os.path.join(deferred, stem + ".txt"), "w", encoding="utf-8") as f:
            f.write(pdf_path + "\n")
        return {"route": "B", "md_path": None, "selfcheck": None}

    # A / C:OCR 主路
    all_blocks: list[dict] = []
    md_pages: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        pngs = pdf_to_pngs(pdf_path, os.path.join(tmp, "png"))
        for png in pngs:
            blocks = predict_page(png, os.path.join(tmp, "json"))
            all_blocks.extend(blocks)
            md_pages.append(reconstruct_markdown(blocks))
    md = "\n\n".join(md_pages) + "\n"

    doc_out = os.path.join(out_dir, stem)
    os.makedirs(doc_out, exist_ok=True)
    md_path = os.path.join(doc_out, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    return {"route": route, "md_path": md_path, "selfcheck": check}


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 单文档转换")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    args = ap.parse_args()
    res = convert_pdf(args.src, args.out)
    print(f"[route={res['route']}] md={res['md_path']}")
    if res["selfcheck"]:
        c = res["selfcheck"]
        print(f"[Tier0] blocks {c['in_md']}/{c['total']} 覆盖, 缺 {len(c['missing'])}")
        for m in c["missing"]:
            print("   MISSING:", m)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 真实端到端 smoke（一次性）**

Run:
```bash
SCRATCH="C:/Users/hulia/AppData/Local/Temp/claude/d--Projects-Project-scholar-md/e30fd570-bcea-42ce-9b97-9c50a94b0266/scratchpad"
.venv-textbooks/Scripts/python -m scripts.pipelines.textbooks.convert --src "D:/Projects/Project_scholar-md/02_Source/textbooks_samples/Paul_Analysis_MTL_scan.pdf" --out "$SCRATCH/convert_smoke"
```
Expected: 打印 `[route=A]`（或 C）+ md 路径 + `[Tier0] blocks N/N 覆盖`。产物 md 里公式带 `\tag{...}`、无页眉页码。

> 注:整本 803 页会很慢(≈78s/页)且首版不分块,smoke 可先用切好的少页样本(如会话暂存区的单页/几页 PDF)验证链路,整本大文件属分块任务(后续)。

- [ ] **Step 3: Commit**

```bash
git add scripts/pipelines/textbooks/convert.py
git commit -m "feat(textbooks): convert 单文档编排 + CLI(--src/--out)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: 全测试回归 + README 收尾

**Files:**
- Modify: `scripts/pipelines/textbooks/README.md`

- [ ] **Step 1: 跑全部单元测试**

Run: `.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v`
Expected: PASS（triage 7 + reconstruct 8 + selfcheck 2 + preprocess 1 = 18 passed）

- [ ] **Step 2: README 补真实用法与首版边界**

在 `README.md` 末尾追加:
```markdown
## 模块
- triage.py — 文本层可信度判 A(无层)/B(优质,登记不转)/C(低质) → A/C 走 OCR
- preprocess.py — PDF→PNG 200dpi
- engine.py — PaddleOCR-VL 1.6 封装(惰性单例)
- reconstruct.py — parsing_res_list → md(按 order 重组、公式编号 \tag 绑定、页眉页脚 order=None 剔除、着重号还原)
- selfcheck.py — Tier0 block 覆盖 lint

## 测试
    .venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v

## 已知边界(后续)
分块(≤50页)/批量/断点续跑、Opus AI 审查、debug_view HTML 复核、B 路文本层直取、triage 阈值标定、vllm 加速。
```

- [ ] **Step 3: Commit**

```bash
git add scripts/pipelines/textbooks/README.md
git commit -m "docs(textbooks): README 收尾 + 首版边界

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 首版完成标准

- 18 个单元测试全绿（triage/reconstruct/selfcheck/preprocess）。
- reconstruct golden fixture 证明:公式编号 `\tag` 全绑回、页眉页码 order=None 剔除、着重号还原、中英文均可。
- engine + convert 真实 smoke 跑通单/少页扫描 PDF → Typora md + Tier0 覆盖报告。
- 不触碰 patents/general;全部落 `scripts/pipelines/textbooks/`;`.venv-textbooks` 独立。

## 明确不在首版（后续计划）

分块(≤50页)/批量/断点续跑 · Opus AI 审查(Tier1) · debug_view HTML 人工复核 · B 路文本层直取 · triage 阈值实测标定 · 图片资源(figure 块)抽取到 `.assets/` · vllm 加速。
