# textbooks 图片输出与 order=None 块补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `scripts/pipelines/textbooks` 管线不再对 `block_order is None` 的块一律丢弃——`image`/`chart` 裁图输出、`table`/`footnote`/`figure_title` 直通输出,按 y0 归并进正文流,并把裁图/分类过程中的异常从"不可见"变成 selfcheck JSON 里的持久化字段。

**Architecture:** 新增 `images.py`(纯像素裁剪,Pillow,零 DPI 换算);`reconstruct.py` 从单一渲染循环拆成"渲染 ordered(不变)+ 渲染 unordered extras(新)+ 两阶段 y0 归并(新)",返回值从 `str` 改成 `(str, warnings)`;`convert.py` 在逐页 OCR 循环里挂裁图钩子(PNG 删除前),并在 `assemble()` 里补一条"续跑/历史检查点缺资产"的补裁通道;`selfcheck.py` 新增双栏启发式 + 告警汇总,最终 4 个新字段落进 selfcheck JSON。

**Tech Stack:** Python 3.12,pytest,Pillow(`.venv-textbooks` 已含,PaddleOCR 传递依赖,本计划把它提升为显式声明),PyMuPDF(现有)。

## Global Constraints

- 红线(继承自项目 CLAUDE.md/交接文档):不改 patents/general/engine;确定性优先,ML 只判断不改字符;`02_Source/` 只读;每管线独立 venv(`.venv-textbooks`);对外操作(merge/push/装大依赖)前所有者确认——本计划的 Pillow 显式声明属于"确认过的既有依赖升级为显式",不属于新装大依赖,可直接做。
- 所有命令用 `.venv-textbooks/Scripts/python.exe`(Windows,不是系统 python)。
- 每个任务必须先写失败测试(TDD,见 superpowers:test-driven-development),verify RED 后再写最小实现,verify GREEN 后再进下一步。
- 设计依据:[2026-07-03-textbooks-image-output-design.md](../specs/2026-07-03-textbooks-image-output-design.md)(下称"spec"),本计划的每个技术决策都可追溯到 spec 对应章节,不重复其推理过程,只引用结论。

---

## File Structure

**新建:**
- `scripts/pipelines/textbooks/images.py` — 裁图(纯函数命名 + Pillow 像素裁剪)
- `scripts/pipelines/textbooks/tests/test_images.py`
- `scripts/pipelines/textbooks/tests/fixtures/paul_p28_res.json` — 真实语料(单栏,1 text+1 image+4 figure_title)
- `scripts/pipelines/textbooks/tests/fixtures/paul_p6_res.json` — 真实语料(双栏嫌疑页)

**修改:**
- `scripts/pipelines/textbooks/reconstruct.py` — 三层 label 分类、y0 归并、返回值改 `(md, warnings)` 元组
- `scripts/pipelines/textbooks/selfcheck.py` — 新增 `detect_column_layout()`、`aggregate_warnings()`
- `scripts/pipelines/textbooks/convert.py` — 裁图钩子、assets 生命周期、补裁循环、selfcheck 四新字段
- `scripts/pipelines/textbooks/requirements.txt` — Pillow 从传递依赖提升为显式声明
- `scripts/pipelines/textbooks/tests/test_reconstruct.py` — 全部调用点改 `md, _ = reconstruct_markdown(...)`,新增归并/分类测试
- `scripts/pipelines/textbooks/tests/test_convert.py` — 新增裁图钩子/补裁/selfcheck 新字段测试

---

## Task 1: `images.py` 纯函数(命名约定 + label 判定)

**Files:**
- Create: `scripts/pipelines/textbooks/images.py`
- Test: `scripts/pipelines/textbooks/tests/test_images.py`

**Interfaces:**
- Produces: `is_visual_block(label: str) -> bool`,`crop_filename(page: int, block_id) -> str`。Task 2(`crop_block_images`)、Task 3(`reconstruct.py`)、Task 6(`convert.py` 补裁循环)都依赖这两个函数,不得重复定义同名逻辑。

- [ ] **Step 1: 写失败测试**

`scripts/pipelines/textbooks/tests/test_images.py`:

```python
from scripts.pipelines.textbooks.images import is_visual_block, crop_filename


def test_is_visual_block_true_for_image_and_chart():
    assert is_visual_block("image") is True
    assert is_visual_block("chart") is True


def test_is_visual_block_false_for_others():
    assert is_visual_block("header_image") is False
    assert is_visual_block("table") is False
    assert is_visual_block("text") is False


def test_crop_filename_format():
    assert crop_filename(6, 3) == "page_0006_block_3.png"
    assert crop_filename(100, 0) == "page_0100_block_0.png"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_images.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'scripts.pipelines.textbooks.images'`

- [ ] **Step 3: 写最小实现**

`scripts/pipelines/textbooks/images.py`:

```python
"""按 block_bbox 从整页 PNG 裁出 image/chart 类图片块,存入 <stem>.assets/。"""
from __future__ import annotations

_VISUAL_LABELS = {"image", "chart"}


def is_visual_block(label: str) -> bool:
    return label in _VISUAL_LABELS


def crop_filename(page: int, block_id) -> str:
    return f"page_{page:04d}_block_{block_id}.png"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_images.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/images.py scripts/pipelines/textbooks/tests/test_images.py
git commit -m "feat(textbooks): images.py 纯函数——is_visual_block/crop_filename 命名唯一事实源"
```

---

## Task 2: `images.py` 裁图(Pillow,I/O)+ Pillow 显式声明

**Files:**
- Modify: `scripts/pipelines/textbooks/images.py`
- Modify: `scripts/pipelines/textbooks/tests/test_images.py`
- Modify: `scripts/pipelines/textbooks/requirements.txt`

**Interfaces:**
- Consumes: `is_visual_block`、`crop_filename`(Task 1,同文件内)
- Produces: `crop_block_images(png_path: str, blocks: list[dict], assets_dir: str, page: int) -> list[dict]`。Task 6(`convert.py`)在逐页循环里直接调用;返回的告警列表元素 shape 为 `{"kind": "visual_missing_bbox"|"visual_crop_error", "label": str, "page": int, "block_id": ..., "sample": str}`(与 spec §5.5 的告警 schema 对齐,`kind` 取值范围是该 schema 的子集——`crop_block_images` 只产出这两种,`unhandled_label`/`visual_unexpected_content` 由 Task 3 的 `reconstruct.py` 产出)。裁图异常不抛出(spec §5.1:"记告警,不判页失败")。

- [ ] **Step 1: 写失败测试**

追加到 `scripts/pipelines/textbooks/tests/test_images.py`:

```python
import os
from PIL import Image
from scripts.pipelines.textbooks.images import crop_block_images


def _make_test_png(path, size=(200, 200)):
    img = Image.new("RGB", size, color="white")
    for x in range(50, 150):
        for y in range(50, 150):
            img.putpixel((x, y), (255, 0, 0))   # 红色方块 [50,50,150,150]
    img.save(path)


def test_crop_block_images_saves_correct_region(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    blocks = [{"block_label": "image", "block_id": 3,
               "block_bbox": [50, 50, 150, 150], "block_content": ""}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert warnings == []
    saved = Image.open(os.path.join(assets_dir, "page_0001_block_3.png"))
    assert saved.size == (100, 100)
    assert saved.getpixel((10, 10)) == (255, 0, 0)


def test_crop_block_images_skips_non_visual_labels(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    blocks = [{"block_label": "text", "block_id": 1,
               "block_bbox": [0, 0, 10, 10], "block_content": "hi"}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert warnings == []
    assert not os.path.exists(assets_dir)


def test_crop_block_images_missing_bbox_warns_and_skips(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    blocks = [{"block_label": "image", "block_id": 5,
               "block_bbox": None, "block_content": ""}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "visual_missing_bbox"
    assert warnings[0]["block_id"] == 5
    assert not os.path.exists(os.path.join(assets_dir, "page_0001_block_5.png")) \
        if os.path.isdir(assets_dir) else True


def test_crop_block_images_bad_bbox_warns_not_raises(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    # x1<x0:Pillow crop 对反向坐标会抛异常
    blocks = [{"block_label": "image", "block_id": 9,
               "block_bbox": [150, 50, 50, 150], "block_content": ""}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "visual_crop_error"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_images.py -v`
Expected: FAIL,`ImportError: cannot import name 'crop_block_images'`

- [ ] **Step 3: 写最小实现**

在 `scripts/pipelines/textbooks/images.py` 追加(保留 Task 1 的两个函数):

```python
import os

from PIL import Image


def crop_block_images(png_path: str, blocks: list[dict], assets_dir: str, page: int) -> list[dict]:
    """裁 image/chart 类块存盘。返回告警列表(缺 bbox / 裁图异常),裁图失败不抛出。"""
    visual_blocks = [b for b in blocks if is_visual_block(b.get("block_label", ""))]
    if not visual_blocks:
        return []
    warnings: list[dict] = []
    img = None
    for b in visual_blocks:
        bbox = b.get("block_bbox")
        label = b.get("block_label", "")
        block_id = b.get("block_id")
        sample = (b.get("block_content") or "")[:40]
        if not bbox:
            warnings.append({"kind": "visual_missing_bbox", "label": label, "page": page,
                              "block_id": block_id, "sample": sample})
            continue
        try:
            if img is None:
                img = Image.open(png_path)
            os.makedirs(assets_dir, exist_ok=True)
            crop = img.crop(tuple(bbox))
            crop.save(os.path.join(assets_dir, crop_filename(page, block_id)))
        except Exception as e:                                   # noqa: BLE001 裁图失败不掀翻整页
            warnings.append({"kind": "visual_crop_error", "label": label, "page": page,
                              "block_id": block_id, "sample": f"{type(e).__name__}: {e}"[:40]})
    return warnings
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_images.py -v`
Expected: 7 passed

- [ ] **Step 5: Pillow 提升为显式依赖**

`scripts/pipelines/textbooks/requirements.txt` 追加一行(Pillow 已在环境里,这里只是把隐式依赖显式声明,不触发新安装):

```diff
 paddlepaddle-gpu==3.2.1
 paddleocr[doc-parser]==3.7.0
 PyMuPDF>=1.27
+Pillow>=12
 pytest>=8
```

验证:`.venv-textbooks/Scripts/python.exe -c "import PIL; print(PIL.__version__)"` → 输出 `12.1.0`(已装,只是确认版本满足 `>=12`)。

- [ ] **Step 6: Commit**

```bash
git add scripts/pipelines/textbooks/images.py scripts/pipelines/textbooks/tests/test_images.py scripts/pipelines/textbooks/requirements.txt
git commit -m "feat(textbooks): images.py crop_block_images——Pillow 像素裁剪,裁图异常记告警不掀翻整页"
```

---

## Task 3: `reconstruct.py` 三层分类 + y0 归并 + 返回值改元组

这是本计划里最大的一个任务,因为"渲染 ordered(不变)"和"渲染/归并 unordered extras(新)"必须在同一次改动里完成——拆开会产生一个只有部分行为、自身就需要一套用后即弃测试的中间态。

**Files:**
- Modify: `scripts/pipelines/textbooks/reconstruct.py`
- Modify: `scripts/pipelines/textbooks/tests/test_reconstruct.py`(全部 ~26 处调用点改签名 + 新增归并测试)

**Interfaces:**
- Consumes: `images.is_visual_block`、`images.crop_filename`(Task 1)
- Produces: `reconstruct_markdown(blocks: list[dict], stem: str | None = None, page: int | None = None) -> tuple[str, list[dict]]`——返回 `(markdown, warnings)`。Task 6(`convert.py` 的 `assemble()`)是唯一的生产调用方,按新签名传 `stem`/`page`,并收集 `warnings` 汇总进 selfcheck。警告项 shape:`{"kind": "unhandled_label"|"visual_missing_bbox"|"visual_unexpected_content", "label": str, "page": int|None, "block_id": ..., "sample": str}`。

### Step 1: 现有全部调用点先做机械改造(签名改动的真实成本,如实做)

`reconstruct_markdown` 即将从返回 `str` 改成返回 `(str, warnings)`。在写新行为之前,先把现有约 26 处 `md = reconstruct_markdown(...)` 全部改成 `md, _ = reconstruct_markdown(...)`,让新签名一旦落地,现有测试立刻可跑(而不是新旧两套签名混着改)。

- [ ] 在 `scripts/pipelines/textbooks/tests/test_reconstruct.py` 里,把所有形如
  `md = reconstruct_markdown(blocks)` 替换为 `md, _ = reconstruct_markdown(blocks)`
  (文件内每处 `reconstruct_markdown(` 调用都要改;用编辑器的"替换全部"逐个确认,不是全局无差别替换,因为
  个别测试变量名不是 `md`,比如已有的 `test_golden_jackson_chinese`/`test_golden_paul_english` 用的也是
  `md`,查一遍确保没有遗漏)。

此步骤本身不跑测试(还没改实现,跑了也是失败),下一步统一验证。

### Step 2: 写归并算法的失败测试

在 `scripts/pipelines/textbooks/tests/test_reconstruct.py` 末尾追加(不删除任何现有测试):

```python
def test_returns_tuple_of_md_and_warnings():
    blocks = [{"block_label": "text", "block_content": "hi", "block_order": 1}]
    result = reconstruct_markdown(blocks)
    assert isinstance(result, tuple) and len(result) == 2
    md, warnings = result
    assert "hi" in md
    assert warnings == []


def test_passthrough_label_inserted_by_y0():
    # footnote(order=None,y0=200)应插在 y0=100 的正文和 y0=300 的正文之间
    blocks = [
        {"block_label": "text", "block_content": "first", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "footnote", "block_content": "note here", "block_order": None,
         "block_bbox": [0, 200, 10, 210]},
        {"block_label": "text", "block_content": "second", "block_order": 2,
         "block_bbox": [0, 300, 10, 310]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert md.index("first") < md.index("note here") < md.index("second")
    assert warnings == []


def test_tie_y0_extra_goes_after_ordered_fragment():
    # spec §3 反例:extra y0=300、ordered y0 序列 [100,300,500]
    # 权威语义"插在第一个 y0>300 的片段之前" → extra 排在 300 之后、500 之前
    blocks = [
        {"block_label": "text", "block_content": "at100", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "text", "block_content": "at300", "block_order": 2,
         "block_bbox": [0, 300, 10, 310]},
        {"block_label": "text", "block_content": "at500", "block_order": 3,
         "block_bbox": [0, 500, 10, 510]},
        {"block_label": "footnote", "block_content": "tied_extra", "block_order": None,
         "block_bbox": [0, 300, 10, 305]},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert md.index("at300") < md.index("tied_extra") < md.index("at500")


def test_pure_extras_page_degenerates_to_extras_only():
    # 零 ordered 片段(纯图/纯脚注页):归并退化为全部 extra 按 y0 输出
    blocks = [
        {"block_label": "footnote", "block_content": "only content", "block_order": None,
         "block_bbox": [0, 50, 10, 60]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert "only content" in md
    assert md.strip() != ""
    assert warnings == []


def test_extras_without_bbox_appended_at_page_tail_in_original_order():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "footnote", "block_content": "no_bbox_a", "block_order": None,
         "block_bbox": None},
        {"block_label": "footnote", "block_content": "no_bbox_b", "block_order": None,
         "block_bbox": None},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert md.index("body") < md.index("no_bbox_a") < md.index("no_bbox_b")


def test_passthrough_empty_content_silently_skipped():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "figure_title", "block_content": "", "block_order": None,
         "block_bbox": [0, 50, 10, 60]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert md.strip() == "body"
    assert warnings == []


def test_known_noise_labels_silently_dropped():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "header", "block_content": "RUNNING HEADER", "block_order": None,
         "block_bbox": [0, 0, 10, 10]},
        {"block_label": "number", "block_content": "42", "block_order": None,
         "block_bbox": [0, 900, 10, 910]},
        {"block_label": "header_image", "block_content": "", "block_order": None,
         "block_bbox": [0, 0, 10, 10]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert "RUNNING HEADER" not in md
    assert "42" not in md
    assert warnings == []


def test_unknown_unordered_label_with_content_warns_and_drops():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "mystery_label", "block_content": "surprise content",
         "block_order": None, "block_bbox": [0, 50, 10, 60]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert "surprise content" not in md
    assert len(warnings) == 1
    assert warnings[0] == {"kind": "unhandled_label", "label": "mystery_label",
                            "page": None, "block_id": None, "sample": "surprise content"}


def test_visual_block_missing_bbox_warns_and_drops():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": None, "block_id": 7},
    ]
    md, warnings = reconstruct_markdown(blocks, stem="doc", page=3)
    assert ".png" not in md
    assert warnings == [{"kind": "visual_missing_bbox", "label": "image",
                          "page": 3, "block_id": 7, "sample": ""}]


def test_visual_block_emits_image_link_with_stem_and_page():
    blocks = [
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": [0, 50, 10, 60], "block_id": 4},
    ]
    md, warnings = reconstruct_markdown(blocks, stem="mybook", page=6)
    assert "![](mybook.assets/page_0006_block_4.png)" in md
    assert warnings == []


def test_visual_block_without_stem_page_raises():
    blocks = [
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": [0, 50, 10, 60], "block_id": 4},
    ]
    import pytest
    with pytest.raises(ValueError):
        reconstruct_markdown(blocks)


def test_visual_block_unexpected_content_keeps_both_and_warns():
    blocks = [
        {"block_label": "chart", "block_content": "unexpected data label",
         "block_order": None, "block_bbox": [0, 50, 10, 60], "block_id": 2},
    ]
    md, warnings = reconstruct_markdown(blocks, stem="doc", page=1)
    assert "![](doc.assets/page_0001_block_2.png)" in md
    assert "unexpected data label" in md
    assert warnings == [{"kind": "visual_unexpected_content", "label": "chart",
                          "page": 1, "block_id": 2, "sample": "unexpected data label"}]
```

### Step 3: 跑测试确认失败

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: 大量 FAIL(签名不匹配 / `ValueError: too many values to unpack` 等),这是预期的——Step 1 已经把测试改成新签名,实现还没跟上。

### Step 4: 重写 `reconstruct_markdown`

用以下内容**整体替换** `scripts/pipelines/textbooks/reconstruct.py` 第 55-109 行(`reconstruct_markdown` 函数,从 `def reconstruct_markdown` 到文件末尾),前面 1-53 行(imports、`_NUM_RE` 等常量、`sanitize_latex`/`restore_emphasis_dots`/`_formula_body`/`_hard_breaks`/`_code_fence`)保持不变,只在文件顶部 import 区追加一行:

```python
from scripts.pipelines.textbooks.images import crop_filename, is_visual_block
```

新增常量(放在 `_KATEX_SUB` 定义之后、`sanitize_latex` 定义之前):

```python
_PASSTHROUGH_UNORDERED_LABELS = {"table", "footnote", "figure_title"}
_KNOWN_NOISE_LABELS = {"header", "number", "header_image"}
```

替换 `reconstruct_markdown` 函数本体(原第 55-109 行)为:

```python
def _render_ordered(ordered: list[dict]) -> list[tuple[float, str]]:
    """按 block_order 渲染,含公式吸收,逻辑与改动前完全一致。返回 [(y0, fragment), ...],
    绝不重排——y0 只用于后续归并时判断 extra 该插在哪,不影响这里的相对顺序。"""
    has_paragraph_title = any(b.get("block_label") == "paragraph_title" for b in ordered)
    fragments: list[tuple[float, str]] = []
    i = 0
    while i < len(ordered):
        b = ordered[i]
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        y0 = (b.get("block_bbox") or [0, 0, 0, 0])[1]
        if not content:
            i += 1
            continue
        if label == "paragraph_title":
            fragments.append((y0, f"## {content}"))
        elif label in ("text", "abstract", "reference_content"):
            fragments.append((y0, restore_emphasis_dots(content)))
        elif label == "content":
            fragments.append((y0, _hard_breaks(content)))
        elif label == "algorithm":
            fragments.append((y0, _code_fence(content)))
        elif label == "doc_title":
            if has_paragraph_title:
                # 同页存在 paragraph_title 兄弟块(不一定是章节序号,可能是完整节标题,
                # 实测 p93 样本):经验规则——同页有 paragraph_title 时 doc_title 是被
                # 误标的正文标题,不是封面。100 页语料 4/4 验证成立,非因果机制。
                fragments.append((y0, f"## {content}"))
            else:
                fragments.append((y0, _hard_breaks(content)))
        elif label == "display_formula":
            body = sanitize_latex(_formula_body(content))
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            if nxt and nxt.get("block_label") == "formula_number":
                m = _NUM_RE.match((nxt.get("block_content") or "").strip())
                tag = m.group(1) if m else (nxt.get("block_content") or "").strip()
                fragments.append((y0, f"$$ {body} \\tag{{{tag}}} $$"))
                i += 1                      # 吸收编号块
            else:
                fragments.append((y0, f"$$ {body} $$"))
        elif label == "formula_number":
            fragments.append((y0, content))
        else:
            print(f"[reconstruct] 未知 block_label={label!r},按纯文本兜底落段", file=sys.stderr)
            fragments.append((y0, content))
        i += 1
    return fragments


def _render_unordered(blocks: list[dict], stem: str | None,
                       page: int | None) -> tuple[list[dict], list[dict]]:
    """渲染 block_order is None 的块(spec §2 三层分类)。返回 (extras, warnings)。
    extras 未排序,元素 {"y0": float|None, "seq": int, "fragment": str};seq 是块在
    输入 blocks 里的原始下标,用于稳定排序/页尾组顺序破 tie。"""
    extras: list[dict] = []
    warnings: list[dict] = []
    for seq, b in enumerate(blocks):
        if b.get("block_order") is not None:
            continue
        label = b.get("block_label", "")
        content = (b.get("block_content") or "").strip()
        bbox = b.get("block_bbox")
        y0 = bbox[1] if bbox else None
        block_id = b.get("block_id")

        if is_visual_block(label):
            if not bbox:
                warnings.append({"kind": "visual_missing_bbox", "label": label, "page": page,
                                  "block_id": block_id, "sample": content[:40]})
                continue
            if stem is None or page is None:
                raise ValueError(
                    f"reconstruct_markdown: 遇到 {label!r} 块(block_id={block_id})但未提供 "
                    "stem/page,无法生成图片引用")
            fragment = f"![]({stem}.assets/{crop_filename(page, block_id)})"
            if content:
                warnings.append({"kind": "visual_unexpected_content", "label": label,
                                  "page": page, "block_id": block_id, "sample": content[:40]})
                fragment += "\n\n" + restore_emphasis_dots(content)
            extras.append({"y0": y0, "seq": seq, "fragment": fragment})
        elif label in _PASSTHROUGH_UNORDERED_LABELS:
            if not content:
                continue
            extras.append({"y0": y0, "seq": seq, "fragment": restore_emphasis_dots(content)})
        elif label in _KNOWN_NOISE_LABELS:
            continue
        elif content:
            warnings.append({"kind": "unhandled_label", "label": label, "page": page,
                              "block_id": block_id, "sample": content[:40]})
        # else: 都不命中且内容为空 → 静默丢弃(无害)
    return extras, warnings


def _merge(ordered_fragments: list[tuple[float, str]], extras: list[dict]) -> list[str]:
    """两阶段归并(spec §3):ordered 内部顺序绝不重排。对每个有 y0 的 extra,插在第一个
    y0 严格大于它的 ordered 片段之前;等价的共享指针实现要求 extra.y0 < 片段.y0 用严格 `<`
    (spec §3"等价性条款"——用 `<=` 会导致 y0 相等的 tie 排在错误一侧,已有单测锁死)。
    缺 y0 的 extra 归入页尾组,按原始列表顺序(seq)排在最后。"""
    positioned = sorted((e for e in extras if e["y0"] is not None),
                         key=lambda e: (e["y0"], e["seq"]))
    tail = sorted((e for e in extras if e["y0"] is None), key=lambda e: e["seq"])
    parts: list[str] = []
    ei = 0
    for y0, fragment in ordered_fragments:
        while ei < len(positioned) and positioned[ei]["y0"] < y0:
            parts.append(positioned[ei]["fragment"])
            ei += 1
        parts.append(fragment)
    while ei < len(positioned):
        parts.append(positioned[ei]["fragment"])
        ei += 1
    parts.extend(e["fragment"] for e in tail)
    return parts


def reconstruct_markdown(blocks: list[dict], stem: str | None = None,
                         page: int | None = None) -> tuple[str, list[dict]]:
    """按 block_order 排序渲染有序块(不重排);block_order is None 的块按 spec §2 三层分类,
    真内容按 spec §3 两阶段归并按 y0 插入正文流。返回 (markdown, warnings)。"""
    ordered = sorted(
        (b for b in blocks if b.get("block_order") is not None),
        key=lambda b: b["block_order"],
    )
    ordered_fragments = _render_ordered(ordered)
    extras, warnings = _render_unordered(blocks, stem, page)
    parts = _merge(ordered_fragments, extras)
    return "\n\n".join(parts) + "\n", warnings
```

### Step 5: 跑测试确认通过

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -v`
Expected: 全部 passed(原有 ~26 条 + 本任务新增 12 条)

### Step 6: 跑全量 textbooks 测试确认无回归

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/ -v`
Expected: `test_selfcheck.py`/`test_convert.py` 等尚未触碰的文件应保持原有通过数(`convert.py` 还没改,`assemble()` 里的 `reconstruct_markdown(blocks)` 调用这一步会报错——这是预期的,Task 6 会修;**此步骤允许 `test_convert.py` 挂,只要挂的原因是"少了一个返回值",不是别的**)。

- [ ] **Step 7: Commit**

```bash
git add scripts/pipelines/textbooks/reconstruct.py scripts/pipelines/textbooks/tests/test_reconstruct.py
git commit -m "feat(textbooks): reconstruct.py 三层 label 分类 + y0 归并 order=None 真内容,返回值改 (md, warnings)"
```

---

## Task 4: `selfcheck.py` 双栏启发式

**Files:**
- Modify: `scripts/pipelines/textbooks/selfcheck.py`
- Modify: `scripts/pipelines/textbooks/tests/test_selfcheck.py`

**Interfaces:**
- Produces: `detect_column_layout(blocks: list[dict]) -> bool`。Task 6(`convert.py` 的 `assemble()`)逐页调用,收集命中页号进 `column_layout_suspected`。

- [ ] **Step 1: 写失败测试**

追加到 `scripts/pipelines/textbooks/tests/test_selfcheck.py`:

```python
from scripts.pipelines.textbooks.selfcheck import detect_column_layout


def test_detect_column_layout_true_for_side_by_side_blocks():
    # 两块 y 区间大幅重叠、x 区间完全分离(左右并排)→ 判定双栏
    blocks = [
        {"block_label": "text", "block_order": 1, "block_bbox": [0, 100, 200, 300]},
        {"block_label": "text", "block_order": 2, "block_bbox": [400, 110, 600, 290]},
    ]
    assert detect_column_layout(blocks) is True


def test_detect_column_layout_false_for_single_column():
    # 正常单栏:纵向排列,x 区间重叠
    blocks = [
        {"block_label": "text", "block_order": 1, "block_bbox": [0, 100, 600, 300]},
        {"block_label": "text", "block_order": 2, "block_bbox": [0, 350, 600, 550]},
    ]
    assert detect_column_layout(blocks) is False


def test_detect_column_layout_ignores_order_none_blocks():
    # header/number 等 order=None 块不参与判定(页眉页脚天然左右分布,不代表双栏正文)
    blocks = [
        {"block_label": "header", "block_order": None, "block_bbox": [0, 0, 100, 20]},
        {"block_label": "number", "block_order": None, "block_bbox": [500, 0, 600, 20]},
    ]
    assert detect_column_layout(blocks) is False


def test_detect_column_layout_ignores_non_text_labels():
    # image/figure_title 等不参与判定,只看 text/display_formula
    blocks = [
        {"block_label": "image", "block_order": None, "block_bbox": [0, 100, 200, 300]},
        {"block_label": "figure_title", "block_order": None, "block_bbox": [400, 110, 600, 290]},
    ]
    assert detect_column_layout(blocks) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_selfcheck.py -v`
Expected: FAIL,`ImportError: cannot import name 'detect_column_layout'`

- [ ] **Step 3: 写最小实现**

在 `scripts/pipelines/textbooks/selfcheck.py` 追加(文件末尾即可,`_probe`/`block_coverage` 保持不变):

```python
def detect_column_layout(blocks: list[dict]) -> bool:
    """双栏启发式(spec §5.6):同页 ordered 的 text/display_formula 块两两比较,存在一对
    y 区间重叠比例 > 0.5(相对较矮块的高度)且 x 区间完全分离 → 判定疑似双栏。"""
    candidates = [b for b in blocks
                  if b.get("block_label") in ("text", "display_formula")
                  and b.get("block_order") is not None and b.get("block_bbox")]
    for i in range(len(candidates)):
        x0a, y0a, x1a, y1a = candidates[i]["block_bbox"]
        for j in range(i + 1, len(candidates)):
            x0b, y0b, x1b, y1b = candidates[j]["block_bbox"]
            overlap = min(y1a, y1b) - max(y0a, y0b)
            if overlap <= 0:
                continue
            shorter = min(y1a - y0a, y1b - y0b)
            if shorter <= 0:
                continue
            if overlap / shorter > 0.5 and (x1a < x0b or x1b < x0a):
                return True
    return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_selfcheck.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/selfcheck.py scripts/pipelines/textbooks/tests/test_selfcheck.py
git commit -m "feat(textbooks): selfcheck.py detect_column_layout——双栏启发式产品化(原一次性脚本)"
```

---

## Task 5: `selfcheck.py` 告警汇总

**Files:**
- Modify: `scripts/pipelines/textbooks/selfcheck.py`
- Modify: `scripts/pipelines/textbooks/tests/test_selfcheck.py`

**Interfaces:**
- Consumes: `reconstruct_markdown` 返回的 `warnings: list[dict]`(Task 3 定义的 shape)
- Produces: `aggregate_warnings(warnings: list[dict]) -> dict`,返回 `{"unhandled_labels": {...}, "visual_warnings": [...]}`。Task 6 直接 `check.update(aggregate_warnings(...))` 塞进 selfcheck 报告。

- [ ] **Step 1: 写失败测试**

追加到 `scripts/pipelines/textbooks/tests/test_selfcheck.py`:

```python
from scripts.pipelines.textbooks.selfcheck import aggregate_warnings


def test_aggregate_warnings_groups_unhandled_labels_with_count():
    warnings = [
        {"kind": "unhandled_label", "label": "mystery", "page": 1, "block_id": 1, "sample": "a"},
        {"kind": "unhandled_label", "label": "mystery", "page": 5, "block_id": 2, "sample": "b"},
    ]
    result = aggregate_warnings(warnings)
    assert result["unhandled_labels"] == {"mystery": {"count": 2, "sample": "a"}}


def test_aggregate_warnings_keeps_visual_warnings_as_list():
    warnings = [
        {"kind": "visual_missing_bbox", "label": "image", "page": 3, "block_id": 9, "sample": ""},
        {"kind": "visual_unexpected_content", "label": "chart", "page": 4, "block_id": 1, "sample": "x"},
    ]
    result = aggregate_warnings(warnings)
    assert result["visual_warnings"] == warnings
    assert result["unhandled_labels"] == {}


def test_aggregate_warnings_empty_input():
    assert aggregate_warnings([]) == {"unhandled_labels": {}, "visual_warnings": []}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_selfcheck.py -v`
Expected: FAIL,`ImportError: cannot import name 'aggregate_warnings'`

- [ ] **Step 3: 写最小实现**

在 `scripts/pipelines/textbooks/selfcheck.py` 追加:

```python
def aggregate_warnings(warnings: list[dict]) -> dict:
    """reconstruct_markdown 逐页告警汇总成 selfcheck 报告字段(spec §5.5/§5.6):
    unhandled_labels 专指没见过的 label(按 label 分组计数);visual_warnings 是
    "认识的 label 但行为超预期"(缺 bbox / 意外带文本),原样列出不聚合。"""
    unhandled_labels: dict[str, dict] = {}
    visual_warnings: list[dict] = []
    for w in warnings:
        if w["kind"] == "unhandled_label":
            entry = unhandled_labels.setdefault(w["label"], {"count": 0, "sample": w["sample"]})
            entry["count"] += 1
        else:
            visual_warnings.append(w)
    return {"unhandled_labels": unhandled_labels, "visual_warnings": visual_warnings}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_selfcheck.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/pipelines/textbooks/selfcheck.py scripts/pipelines/textbooks/tests/test_selfcheck.py
git commit -m "feat(textbooks): selfcheck.py aggregate_warnings——reconstruct 告警持久化进报告,不止 stderr"
```

---

## Task 6: `convert.py` 裁图钩子 + 补裁循环 + selfcheck 四新字段

最大的集成任务:把 Task 1-5 的产物接进主流程。拆成两个阶段的原因是——"裁图钩子何时触发"(阶段 A,OCR 时)和"assemble 时如何补裁 + 汇总"(阶段 B)必须在同一批改动里完成,否则阶段 A 单独落地时 `assemble()` 还调用旧签名的 `reconstruct_markdown`,整个 `convert_pdf()` 跑不通,没有独立可测的中间态。

**Files:**
- Modify: `scripts/pipelines/textbooks/convert.py`
- Modify: `scripts/pipelines/textbooks/tests/test_convert.py`

**Interfaces:**
- Consumes: `images.crop_block_images`/`is_visual_block`/`crop_filename`(Task 1-2)、`reconstruct_markdown(blocks, stem, page) -> (md, warnings)`(Task 3)、`selfcheck.detect_column_layout`/`aggregate_warnings`(Task 4-5)
- Produces: `assemble(work_dir, total, stem, assets_dir, pdf_path, dpi) -> dict`(keys: `md`, `blocks`, `warnings`, `missing_assets`, `column_layout_suspected`);`convert_pdf()` 的返回 dict 的 `selfcheck` 字段新增 4 个 key:`unhandled_labels`/`visual_warnings`/`column_layout_suspected`/`missing_assets`。

### Step 1: 写失败测试(裁图钩子 + assets 生命周期)

追加到 `scripts/pipelines/textbooks/tests/test_convert.py`:

```python
def _one_image_block(page):
    return [{"block_order": None, "block_label": "image", "block_id": 1,
             "block_content": "", "block_bbox": [5, 5, 15, 15]}]


def test_convert_crops_images_before_png_deleted(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100)
    assets_dir = os.path.join(out, "scan", "scan.assets")
    assert os.path.exists(os.path.join(assets_dir, "page_0001_block_1.png"))
    md = open(res["md_path"], encoding="utf-8").read()
    assert "scan.assets/page_0001_block_1.png" in md


def test_convert_clears_assets_on_fingerprint_mismatch(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    assets_dir = os.path.join(out, "scan", "scan.assets")
    assert os.path.exists(os.path.join(assets_dir, "page_0001_block_1.png"))
    # 换 DPI 触发指纹失配 → 全新跑,旧资产应被清空(重新裁出的文件名相同,
    # 用一个哨兵文件验证目录整体被清过,而不仅是被覆盖)
    sentinel = os.path.join(assets_dir, "STALE_SENTINEL.png")
    open(sentinel, "w").close()
    cv.convert_pdf(pdf, out, dpi=120)
    assert not os.path.exists(sentinel)
```

### Step 2: 跑测试确认失败

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_convert.py -k "crops_images or clears_assets" -v`
Expected: FAIL(`scan.assets` 目录不存在,或 `TypeError`——`assemble()` 还没改)

### Step 3: 改 `convert.py` 头部与 `assemble()`

**改 imports**(原第 1-14 行),把:

```python
"""单文档编排:分诊 → (A/C)逐页流式 OCR(可续跑/磁盘有界/坏页隔离) → 重组 md。B 登记不转。"""
from __future__ import annotations

import argparse
import json
import os
import time

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown
from scripts.pipelines.textbooks.selfcheck import block_coverage, katex_incompat_scan
from scripts.pipelines.textbooks import checkpoint as cp
```

改成:

```python
"""单文档编排:分诊 → (A/C)逐页流式 OCR(可续跑/磁盘有界/坏页隔离) → 重组 md。B 登记不转。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown
from scripts.pipelines.textbooks.selfcheck import (
    block_coverage, katex_incompat_scan, aggregate_warnings, detect_column_layout,
)
from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import images
```

**整体替换 `assemble()`**(原第 17-27 行)为:

```python
def _expected_visual_filenames(blocks: list[dict], page: int) -> list[str]:
    return [images.crop_filename(page, b.get("block_id"))
            for b in blocks
            if images.is_visual_block(b.get("block_label", "")) and b.get("block_bbox")]


def _backfill_missing_assets(blocks: list[dict], pdf_path: str, dpi: int,
                              work_dir: str, assets_dir: str, page: int) -> None:
    """裁图钩子只覆盖本次运行处理的页;已完成页(续跑/历史检查点)不会重新进入
    OCR 循环,PNG 早已删除。这里对每页核对应有的裁图文件是否在盘,缺失则用
    manifest 记录的 dpi 重新栅格化该页(同 DPI 保证 bbox 对齐)、裁图、删 PNG。"""
    expected = _expected_visual_filenames(blocks, page)
    if not expected:
        return
    if all(os.path.exists(os.path.join(assets_dir, f)) for f in expected):
        return
    png = None
    try:
        png = pdf_page_to_png(pdf_path, page, work_dir, dpi=dpi)
        images.crop_block_images(png, blocks, assets_dir, page)
    except Exception:                                          # noqa: BLE001 补裁失败不掀翻整批
        pass
    finally:
        if png and os.path.exists(png):
            os.remove(png)


def assemble(work_dir: str, total: int, stem: str, assets_dir: str,
             pdf_path: str, dpi: int) -> dict:
    """按页序读检查点 → 重组 md + 补裁缺失资产 + 汇总告警/双栏嫌疑页/缺失资产清单。"""
    md_pages: list[str] = []
    all_blocks: list[dict] = []
    all_warnings: list[dict] = []
    missing_assets: list[str] = []
    column_layout_suspected: list[int] = []
    for i in range(1, total + 1):
        blocks = cp.load_page_blocks(work_dir, i)
        all_blocks.extend(blocks)
        _backfill_missing_assets(blocks, pdf_path, dpi, work_dir, assets_dir, i)
        expected = _expected_visual_filenames(blocks, i)
        missing_assets.extend(f for f in expected
                              if not os.path.exists(os.path.join(assets_dir, f)))
        if detect_column_layout(blocks):
            column_layout_suspected.append(i)
        page_md, warnings = reconstruct_markdown(blocks, stem=stem, page=i)
        all_warnings.extend(warnings)
        if page_md.strip():
            md_pages.append(page_md)
    return {
        "md": "\n\n".join(md_pages) + "\n",
        "blocks": all_blocks,
        "warnings": all_warnings,
        "missing_assets": missing_assets,
        "column_layout_suspected": column_layout_suspected,
    }
```

### Step 4: 改 `convert_pdf()` ——裁图钩子 + assets 生命周期 + 调用新 `assemble()`

在 `doc_out`/`work_dir` 定义之后(原第 46-47 行)追加一行:

```python
    doc_out = os.path.join(out_dir, stem)
    work_dir = os.path.join(doc_out, "_work")
    assets_dir = os.path.join(doc_out, stem + ".assets")
```

指纹失配重置块(原第 49-56 行),把:

```python
    manifest = cp.load_manifest(work_dir)
    if manifest is None or not cp.fingerprint_ok(manifest, pdf_path, dpi):
        if manifest is not None:
            print(f"[textbooks] 指纹失配(源或DPI变),清空 {work_dir} 全新跑")
        cp.reset_work_dir(work_dir)
        manifest = cp.new_manifest(pdf_path, cp.pdf_fingerprint(pdf_path), dpi, route)
        cp.save_manifest(work_dir, manifest)
```

改成:

```python
    manifest = cp.load_manifest(work_dir)
    if manifest is None or not cp.fingerprint_ok(manifest, pdf_path, dpi):
        if manifest is not None:
            print(f"[textbooks] 指纹失配(源或DPI变),清空 {work_dir} 全新跑")
        cp.reset_work_dir(work_dir)
        if os.path.isdir(assets_dir):                # assets 在 doc_out 不在 work_dir,
            shutil.rmtree(assets_dir)                 # reset_work_dir 碰不到,不清会变孤儿文件
        manifest = cp.new_manifest(pdf_path, cp.pdf_fingerprint(pdf_path), dpi, route)
        cp.save_manifest(work_dir, manifest)
```

逐页 OCR 循环里的裁图钩子(原第 80-91 行),把:

```python
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
```

改成:

```python
        png = None
        try:
            png = pdf_page_to_png(pdf_path, page, work_dir, dpi=dpi)
            blocks = predict_page(png, work_dir)   # 非空时 engine 已落 res.json
            if not blocks and not cp.is_page_done(work_dir, page):
                cp.write_empty_page(work_dir, page)   # 空白页显式标记完成
            elif blocks:
                images.crop_block_images(png, blocks, assets_dir, page)  # PNG 删除前裁图
        except Exception as e:                        # noqa: BLE001 坏页隔离
            cp.record_failure(manifest, page, f"{type(e).__name__}: {e}",
                              "page-exception")
        finally:
            if png and os.path.exists(png):
                os.remove(png)                        # 磁盘有界:predict 后即删
```

组装与写盘部分(原第 110-121 行),把:

```python
    # 从检查点重组(每次运行都做,部分完成也产出部分 md)
    md, all_blocks = assemble(work_dir, total)
    os.makedirs(doc_out, exist_ok=True)
    md_path = os.path.join(doc_out, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
```

改成:

```python
    # 从检查点重组(每次运行都做,部分完成也产出部分 md);顺带补裁续跑/历史检查点缺失的资产
    result = assemble(work_dir, total, stem, assets_dir, pdf_path, dpi)
    md, all_blocks = result["md"], result["blocks"]
    os.makedirs(doc_out, exist_ok=True)
    md_path = os.path.join(doc_out, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    check.update(aggregate_warnings(result["warnings"]))
    check["missing_assets"] = result["missing_assets"]
    check["column_layout_suspected"] = result["column_layout_suspected"]
```

### Step 5: 跑测试确认新增测试通过

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_convert.py -k "crops_images or clears_assets" -v`
Expected: 2 passed

### Step 6: 跑全量 textbooks 测试确认无回归

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/ -v`
Expected: 全部 passed(此时应无任何 FAIL——Task 3 遗留的 `test_convert.py` 挂用例在这一步应全部转绿)

### Step 7: 写失败测试(补裁循环 + missing_assets)

追加到 `scripts/pipelines/textbooks/tests/test_convert.py`:

```python
def test_convert_backfills_assets_for_pre_existing_checkpoint(tmp_path, monkeypatch):
    # 模拟"图片功能上线前跑完的检查点":res.json 里有 image 块,但 assets 目录不存在
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": _one_image_block(1)}, f)
    cp.save_manifest(work, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A"))
    res = cv.convert_pdf(pdf, out, dpi=100)          # 该页已"完成",不会重新进 OCR 循环
    assets_dir = os.path.join(out, "scan", "scan.assets")
    assert os.path.exists(os.path.join(assets_dir, "page_0001_block_1.png"))  # 补裁生效
    assert res["selfcheck"]["missing_assets"] == []


def test_convert_missing_assets_reported_when_backfill_impossible(tmp_path, monkeypatch):
    # pdf_path 指向的文件在补裁时已不存在(源文件被移走等极端场景)→ 补裁失败,
    # 但不应崩溃,应如实反映在 missing_assets 里
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": _one_image_block(1)}, f)
    cp.save_manifest(work, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A"))
    os.remove(pdf)                                   # 源文件消失
    res = cv.convert_pdf(pdf, out, dpi=100)
    assert res["selfcheck"]["missing_assets"] == ["page_0001_block_1.png"]


def test_convert_selfcheck_has_four_new_fields(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    for key in ("unhandled_labels", "visual_warnings", "column_layout_suspected", "missing_assets"):
        assert key in res["selfcheck"]
    assert res["selfcheck"]["unhandled_labels"] == {}
    assert res["selfcheck"]["visual_warnings"] == []
    assert res["selfcheck"]["column_layout_suspected"] == []
    assert res["selfcheck"]["missing_assets"] == []
```

### Step 8: 跑测试确认失败,然后确认通过

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_convert.py -k "backfill or missing_assets or four_new_fields" -v`

先确认这三条在 Step 3-4 的实现下已经能过(补裁循环已经在 Step 3 的 `assemble()`/`_backfill_missing_assets` 里写好了)——如果全绿说明 Step 3-4 已经把这部分覆盖了,不需要额外实现;如果有 FAIL,回到 `_backfill_missing_assets`/`assemble()` 补对应分支,直至 3 个测试全部 passed。

- [ ] **Step 9: 跑全量测试确认无回归**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/ -v`
Expected: 全部 passed

- [ ] **Step 10: Commit**

```bash
git add scripts/pipelines/textbooks/convert.py scripts/pipelines/textbooks/tests/test_convert.py
git commit -m "feat(textbooks): convert.py 裁图钩子+补裁循环+assets生命周期+selfcheck四新字段"
```

---

## Task 7: Golden 测试(真实语料,非合成)

**Files:**
- Create: `scripts/pipelines/textbooks/tests/fixtures/paul_p28_res.json`
- Create: `scripts/pipelines/textbooks/tests/fixtures/paul_p6_res.json`
- Modify: `scripts/pipelines/textbooks/tests/test_reconstruct.py`

**Interfaces:**
- Consumes: `reconstruct_markdown`(Task 3)、`selfcheck.detect_column_layout`(Task 4)

- [ ] **Step 1: 拷贝真实语料 fixture**

源文件来自 100 页真实测试产物(§1 已核实过 p28/p6 的具体内容:p28 单栏,1 个 text + 1 个 image + 4 个
figure_title;p6 双栏嫌疑页)。用 Bash/PowerShell 复制,不要手抄内容:

```bash
cp "03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/_work/page_0028_res.json" \
   "scripts/pipelines/textbooks/tests/fixtures/paul_p28_res.json"
cp "03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/_work/page_0006_res.json" \
   "scripts/pipelines/textbooks/tests/fixtures/paul_p6_res.json"
```

- [ ] **Step 2: 写测试(不需要"先失败"仪式——这一步是给已实现的行为补真实语料回归锚点,
  但仍要先跑一次确认测试本身没写错、且在当前实现下真的会通过,而不是断言写错了也green)**

追加到 `scripts/pipelines/textbooks/tests/test_reconstruct.py`:

```python
def test_golden_p28_image_inserted_between_text_and_captions():
    blocks = json.loads((FIX / "paul_p28_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md, warnings = reconstruct_markdown(blocks, stem="paul", page=28)
    assert "paul.assets/page_0028_block_" in md
    # image 在正文 text 之后、figure_title 说明文字之前(该页真实版面顺序)
    text_pos = md.index("cables used to interconnect")
    image_pos = md.index("paul.assets/")
    caption_pos = md.index("FIGURE 1.1")
    assert text_pos < image_pos < caption_pos
    assert warnings == []


def test_golden_p6_column_suspect_output_is_deterministic():
    blocks = json.loads((FIX / "paul_p6_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    from scripts.pipelines.textbooks.selfcheck import detect_column_layout
    assert detect_column_layout(blocks) is True          # 该页已知是双栏嫌疑页(spec §1/§3)
    md1, _ = reconstruct_markdown(blocks, stem="paul", page=6)
    md2, _ = reconstruct_markdown(blocks, stem="paul", page=6)
    assert md1 == md2                                     # 锁"确定性",不锁"正确性"
```

- [ ] **Step 3: 跑测试**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/test_reconstruct.py -k golden -v`
Expected: 全部 passed。若 `test_golden_p28_...` 失败,先用
`python -c "import json; print([b['block_content'][:60] for b in json.load(open('scripts/pipelines/textbooks/tests/fixtures/paul_p28_res.json',encoding='utf-8'))['parsing_res_list']])"`
核对 fixture 里的实际字符串,按真实内容调整断言字符串(不要为了让测试过而弱化断言)。

- [ ] **Step 4: Commit**

```bash
git add scripts/pipelines/textbooks/tests/fixtures/paul_p28_res.json scripts/pipelines/textbooks/tests/fixtures/paul_p6_res.json scripts/pipelines/textbooks/tests/test_reconstruct.py
git commit -m "test(textbooks): 补真实语料 golden 测试——p28 图片插入位置、p6 双栏嫌疑页确定性"
```

---

## Task 8: 全量验证(单测 + 100 页真实语料离线复核)

不写新代码,只验证 Task 1-7 合起来的效果,并且顺手把手头那份 100 页真实测试目录的历史检查点治好(spec §5.3 提到的"顺手治愈现有 100 页测试目录")。

**Files:** 无新增/修改代码文件

- [ ] **Step 1: 全量单测**

Run: `.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/ -v`
Expected: 全部 passed,数量应为 Task 1-7 新增测试数之和 + 之前既有的 102 条。

- [ ] **Step 2: 对 100 页真实语料跑一次真实 `convert_pdf()`(触发补裁,不重新 OCR)**

这份检查点是图片功能上线前跑完的,`_work/` 里全部 100 页 res.json 都在、但没有任何 `.assets/` 目录。
用真实 `convert_pdf()`(不是离线脚本)跑一次,应该触发 Task 6 的补裁循环,不触发任何页面重新 OCR
(因为 `is_page_done` 全部为真):

```bash
.venv-textbooks/Scripts/python.exe -m scripts.pipelines.textbooks.convert \
  --src "03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan_source.pdf" \
  --out "03_Output/textbooks/_realrun_100page_test" \
  --dpi 150
```

> 注意:此命令假设 100 页切片源 PDF 仍在原路径(见 [大文件稳健化交接](../../handoff/2026-07-02-HANDOFF-textbooks-large-file-done.md) §1 的复现方法:
> `fitz.open(src); out.insert_pdf(doc, from_page=0, to_page=99)`)。若源文件已不在,先按该交接文档的方法重新切一份到相同路径,
> 或调整 `--src` 指向实际位置——**只要 `_work/` 目录的检查点不动,补裁逻辑就能在不重新 OCR 的前提下把图片找补回来**。

- [ ] **Step 3: 核对 selfcheck 四个新字段**

```bash
.venv-textbooks/Scripts/python.exe -c "
import json
with open('03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan_selfcheck.json', encoding='utf-8') as f:
    c = json.load(f)
print('missing_assets:', len(c['missing_assets']))
print('column_layout_suspected:', c['column_layout_suspected'])
print('unhandled_labels:', c['unhandled_labels'])
print('visual_warnings count:', len(c['visual_warnings']))
"
```

Expected:
- `missing_assets` 为 `[]`(补裁应已把全部 54 image + 12 chart 找补回来)
- `column_layout_suspected` 只命中 `[2, 6]`(spec §3/§5.6 的基线,交叉验证过两次)
- `unhandled_labels` 为 `{}`(100 页语料的 18 种 label 已全部被模块①②③覆盖)
- `visual_warnings`:预期为空或只含极少量(header_image 已归为噪声,不会触发;若非空,逐条核对 `kind`/`sample` 是否符合预期,不是代码 bug 就是当时 §1 统计有遗漏,两种情况都要交接说明,不能就地忽略)

- [ ] **Step 4: 核对 `.assets` 目录内容**

```bash
ls "03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan.assets/" | wc -l
```

Expected: 66(54 image + 12 chart,与 spec §1 表格一致)

- [ ] **Step 5: 记录结果**

把 Step 3/4 的实际输出贴进 `TODO.md` 当天节(不新起文件),标注模块③完成。若 Step 3 的任何一项与预期不符,
不要自行"合理化"差异——按 [superpowers:systematic-debugging](../../../CLAUDE.md 引用的技能) 定位根因,
必要时回退到相应 Task 补测试用例锁住新发现的边界情况,再重新走一遍本 Task。

---

## Self-Review Notes(写计划时已自查,供执行者参考)

- **Spec 覆盖**:§2(三层分类)→ Task 3;§3(y0 归并 + tie 反例)→ Task 3;§4(Pillow 路线)→ Task 2;
  §5.1-5.4(images.py/钩子/补裁/签名)→ Task 1/2/6;§5.5-5.6(告警 schema/selfcheck 四字段)→ Task 3/5/6;
  §6(测试策略)→ 各任务内联 + Task 7 golden + Task 8 全量验证;§7(遗留)不建任务,已在 spec 里显式承认为
  不做的范围,本计划不重复处理。
- **`assemble()` 返回类型从 tuple 改 dict**:spec 没有明确写这一点(spec §5.4 只提到 `reconstruct_markdown`
  的返回值改 tuple),这是本计划在落实 spec §5.5"告警汇总"+ §5.6"四新字段"时做出的实现选择——`assemble()`
  要同时传回 5 样东西,dict 比不断加长的 tuple 更不容易在调用点错位。`assemble()` 目前没有独立单测直接调用
  (只通过 `convert_pdf()` 间接测),改类型不破坏任何现有测试签名。
- **裁图钩子的告警目前不持久化**(Task 6 Step 4 的钩子调用 `images.crop_block_images(...)` 但丢弃返回值):
  这是有意的设计取舍——OCR 时钩子产生的 `visual_missing_bbox`/`visual_crop_error` 告警,`assemble()` 阶段的
  `reconstruct_markdown`/`_backfill_missing_assets` 会独立地对同一批块重新判定一遍(`visual_missing_bbox` 会
  被 reconstruct 重新发现;`visual_crop_error` 不会,因为 reconstruct 不碰像素)。也就是说**如果 OCR 时裁图
  抛异常(如 bbox 越界),这条 `visual_crop_error` 目前只会打印到 stderr,不会进最终 selfcheck JSON**——这是一
  个已知、有意接受的残余缺口(现有语料 100 页里没有这类样本,不构成当前验证阻碍),类比现有 `restarts` 字段
  "audit-only,不完整"的先例。如果 803 页全量跑批后发现这类情况非罕见,需要单开一个小任务把 OCR 时的裁图告警
  也持久化(比如追加写进 manifest 的 `crop_warnings` 列表,续跑时也能读到)。
