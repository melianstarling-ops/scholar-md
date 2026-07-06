# textbooks 管线 I/O 重构 + 模块化 设计

> 撰写:2026-07-06。承 [多后端封装 plan](../plans/2026-07-05-textbooks-vision-repair-multibackend.md) 之前先做(解锁转换侧并行)。

## 目标

把转换管线产物拆成**交付根 / 过程根**双目录;所有产物路径收口到单一 `paths.py`;把游离的 KaTeX 报错扫描接入为一等 CLI 积木;更新过时的 README。

## 范围(明确边界)

- **只做** I/O 布局 + 路径模块化 + katex 接入 + README。
- **不含** 多后端封装(独立 plan)。本轮**不动** `vision_repair.py` 的 AI 调用逻辑,**只穿它的读写路径**。
- **不含** 引擎/重组/自检算法本身的改动。

## 动机

1. **交付物与过程物分离**:成果(`md` + `.assets` 图片)要能干净打包分发,不跟几万页的 `_work/*_res.json`、报错、修正搅在一起。
2. **重数据不进同步盘**:过程根可指向本地大盘,交付根(小)留 OneDrive/可打包。
3. **模块化**:每个 stage 是「输入路径 → 输出路径」的纯积木,可单独 CLI 跑某一步(只生图 / 只提公式错),不必走整管线。路径逻辑收口一处,以后改布局只动 `paths.py`。

## 架构

### 双根布局

```
交付根 --out(小/可同步可打包)          过程根 --work-dir(大/本地大盘,默认 <out>/_work_root)
<out>/<stem>/                            <work>/<stem>/
  ├─ <stem>.md                             ├─ _work/                    manifest + 逐页 res.json
  └─ <stem>.assets/                        ├─ <stem>_repair/            裁图 + worklist.json
                                           ├─ <stem>_render_errors.json
                                           ├─ <stem>_corrections.json
                                           ├─ <stem>_selfcheck.json
                                           └─ <stem>_debug.html
```

删掉整个过程根,交付根仍是一套完整可分发成果。`md` 靠相对路径 `![](<stem>.assets/...)` 引用图片,两者同处交付位,链接不破。

### 组件

**① `paths.py` —— 单一真相源(纯函数,值得 TDD)**

```python
@dataclass(frozen=True)
class DocLayout:
    stem: str
    deliverables_root: str
    work_root: str

    # 交付侧
    @property
    def doc_deliverable_dir(self) -> str: ...   # <deliverables>/<stem>
    @property
    def md_path(self) -> str: ...                # <deliverables>/<stem>/<stem>.md
    @property
    def assets_dir(self) -> str: ...             # <deliverables>/<stem>/<stem>.assets

    # 过程侧
    @property
    def doc_work_dir(self) -> str: ...           # <work>/<stem>
    @property
    def work_dir(self) -> str: ...               # <work>/<stem>/_work
    @property
    def repair_dir(self) -> str: ...             # <work>/<stem>/<stem>_repair
    @property
    def worklist_path(self) -> str: ...          # <work>/<stem>/<stem>_repair/worklist.json
    @property
    def render_errors_path(self) -> str: ...     # <work>/<stem>/<stem>_render_errors.json
    @property
    def corrections_path(self) -> str: ...       # <work>/<stem>/<stem>_corrections.json
    @property
    def selfcheck_path(self) -> str: ...         # <work>/<stem>/<stem>_selfcheck.json
    @property
    def debug_html_path(self) -> str: ...        # <work>/<stem>/<stem>_debug.html


def resolve_layout(stem: str, deliverables_root: str,
                   work_root: str | None = None) -> DocLayout:
    """work_root 缺省 = <deliverables_root>/_work_root。"""
```

**② stage 编排层穿 `DocLayout`(机械改造,概念集中在编排边界)**

关键认知:多数**内部纯函数已经接收显式路径**(如 `assemble(work_dir, ..., assets_dir, ...)` 已分别拿 `work_dir`/`assets_dir`),所以布局拆分**集中在编排/CLI 边界的几个函数**,内部积木基本不动。需改的编排函数:

- `convert.convert_pdf(pdf_path, deliverables_dir, work_dir=None, dpi=...)` —— 由 `DocLayout` 决定 md/assets 落交付位、其余落过程位。
- `convert.reassemble_md(layout, pdf_path, dpi)` —— 从 doc_dir 派生改为吃 `DocLayout`。
- `debug_repair.build_repair_worklist(layout, pdf_path=None, ...)` —— 读 `layout.work_dir`,写 `layout.repair_dir`。
- `vision_repair.run_vision_repair(layout, ...)` —— 读 `layout.worklist_path`,写 `layout.corrections_path`。**仅路径,AI 逻辑不动。**
- `batch.run(...)` + `_already_done`/`_read_summary` —— 加 `--work-dir`,逐本构 `DocLayout`。
- `debug_view` main —— 吃两根路径;**生成的 `<stem>_debug.html` 属过程产物,落过程根 `layout.debug_html_path`,不进交付根**。

**③ `katex_scan.py` —— 新 CLI 积木(薄壳,轻测)**

```python
def scan_katex(md_path: str, out_path: str, node_bin: str | None = None) -> dict | None:
    """薄壳 subprocess 调 debug_assets/scan_katex_errors.mjs;node 不在 PATH 时返回
    None(优雅跳过,不抛),调用方据此打警告不失败。"""
```

- CLI:`python -m scripts.pipelines.textbooks.katex_scan --md <> --out <>`。
- batch 每本 convert 完自动跑,**默认开**,`--no-katex-scan` 关;`node` 缺失 → 警告跳过、不掀翻整本书。

**④ README 更新(文档,无测试)**

反映当前 + 新布局的真实使用:全部 stage CLI、双根布局与 `--out`/`--work-dir`、完整工作流(下方数据流表)、环境、测试。删掉「首版范围」「已知边界」里已实现的过时描述。

### 数据流(标注碰哪个根)

| # | 阶段 | 谁执行 | 交付根 | 过程根 |
|---|------|--------|:-:|:-:|
| 1 | 分诊 triage | 确定性 Python | | |
| 2 | 逐页 OCR | GPU 引擎 | 写 assets | 写 _work/res.json |
| 3 | 重组 + 自检 | 确定性 Python | 写 md | 写 selfcheck |
| 4 | katex 扫描 *(新)* | Node+KaTeX(经薄壳) | 读 md | 写 render_errors |
| 5 | 建修复工作单 | 确定性 Python | | 读 _work / 写 _repair |
| 6 | 视觉修复 *(AI,多后端另plan)* | AI CLI | | 读 worklist / 写 corrections |
| 7 | 人工确认门 | 人 | | 改 corrections status |
| 8 | 落 md 对账 | 确定性 Python | 写 md | 读 _work+corrections |

### 统一 CLI 接口

每个 stage CLI 都接 `--out`(交付根)与 `--work-dir`(过程根,缺省 `<out>/_work_root`)。单文档 CLI 另带 `--src`;debug_repair 保留 `--src` 覆盖源 PDF。

## 迁移(已完成 —— 本会话已执行)

旧产物已就地迁入双根布局,两个符合标准布局的目录:
- `03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan` → 过程物入同 out_root 的 `_work_root/Paul_p1-100_scan/`。
- `03_Output/textbooks/Paul_Analysis_MTL_scan_page49` → 过程物入 `03_Output/textbooks/_work_root/Paul_Analysis_MTL_scan_page49/`。

非标准实验/样本目录(`_page49_visual`、`_experiments`、`_firstrun_samples`)保持原样。多后端 plan Task 7 引用的 crop 绝对路径已同步更新。

## 测试策略(按任务轻重,不一刀切 TDD)

遵 [[feedback_right-size-orchestration]]:

- **`paths.py`:走 TDD。** 纯函数、零副作用、路径拼错是隐蔽 bug —— 先写断言(各 property 的期望路径、`work_root` 缺省推导),再实现。高性价比。
- **stage 编排层穿 `DocLayout`:不新套 test-first 仪式。** 这是机械改签名。做法:把**现有**受影响测试(test_convert / test_debug_repair / test_vision_repair / test_batch / test_debug_view)**重定向到新布局**跑绿即可,外加一次真实端到端调用(convert 一个小样 → 看两根各就各位)。不给每行 arg 传递写新前置测试。
- **`katex_scan.py`:2–3 条轻测。** argv 拼装(monkeypatch subprocess)+ `node` 缺失优雅跳过路径。不做完整 TDD。
- **README:无测试。**

基线:`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/`(改造后应仍全绿,数量约等于原 260 + `paths.py`/`katex_scan` 少量新测)。

## 错误处理 / 边界

- **向后兼容**:只给 `--out` 时 work_root 自动落 `<out>/_work_root`,「一条命令就能跑」不变。
- **node 缺失**:katex_scan 返回 None、警告跳过,不阻断转换。
- **同名 stem 跨目录冲突**:batch 现有 `discover` 的冲突检出照旧(双根不影响该判断)。
- **过程根/交付根同盘或异盘**:均可;reassemble 读过程根写交付根,两根路径都由 `DocLayout` 显式持有,无隐式派生。

## 非目标(YAGNI)

- 不做旧布局自动探测/双读兼容(已一次性迁移,新布局即默认)。
- 不做过程根的自动清理/GC(留待日后)。
- 不动 selfcheck 采纳后陈旧那条已知限制(by design)。
