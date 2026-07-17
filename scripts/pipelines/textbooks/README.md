# textbooks 管线

扫描/教科书 PDF → Markdown（PaddleOCR-VL + 确定性重组 + 可选 AI 公式视觉修复）。
工作区私有设计:`04_Docs/superpowers/specs/2026-07-01-textbooks-pipeline-design.md`、
`04_Docs/superpowers/specs/2026-07-06-textbooks-io-restructure-design.md`(双根布局)。

## 环境
独立 `.venv-textbooks`(勿混用 patents/general 的 `.venv`)。装 `requirements.txt`。
KaTeX 扫描步骤额外需要 `node` 在 PATH(缺失则该步优雅跳过,不影响转换)。

## 双根产物布局

产物分两个根,交付物与过程物分离:

```
交付根 --out(小,可同步/可打包)          过程根 --work-dir(大,可放本地大盘,默认 <out>/_work_root)
<out>/<stem>/                            <work>/<stem>/
  ├─ <stem>.md          ← 成果            ├─ _work/                    manifest + 逐页 res.json
  └─ <stem>.assets/     ← 成果图片        ├─ <stem>_repair/            修复裁图 + worklist.json
                                          ├─ <stem>_render_errors.json
                                          ├─ <stem>_corrections.json
                                          ├─ <stem>_selfcheck.json
                                          └─ <stem>_debug.html
```

删掉整个过程根,交付根仍是一套完整可分发成果(md 靠相对路径引用 `.assets/`)。
`--work-dir` 省略时默认 `<out>/_work_root`,"一条命令就能跑"不变。

## 完整工作流

| # | 阶段 | 执行者 | 输入 → 产出 | 碰交付/过程 |
|---|------|--------|------------|:-:|
| 1 | 分诊 triage | 确定性 Python | PDF → 路线 A/B/C（B=born-digital，默认 `--born-digital-mode defer` 登记不转；`ocr`/`hybrid` 走 OCR 主链；`--force-ocr` 时改走 F=强制 OCR） | — |
| 2 | 逐页 OCR | GPU 引擎 PaddleOCR-VL | PDF 页 → `_work/page_NNNN_res.json` + `.assets/` 裁图 | 双 |
| 3 | 重组 + 自检 | 确定性 Python | `res.json` → `<stem>.md` + `_selfcheck.json` | 双 |
| 4 | KaTeX 报错扫描 | Node + KaTeX(经 katex_scan 薄壳) | `md` → `_render_errors.json` | 双 |
| 5 | 建修复工作单 | 确定性 Python(重栅格化源 PDF) | `res.json`+`render_errors` → `_repair/` + `worklist.json` | 过程 |
| 6 | 公式视觉修复 | AI(claude/agy/codex/kimi CLI) | `worklist.json` 裁图 → `_corrections.json`(status=pending) | 过程 |
| 7 | 人工确认门 | 人(debug_view 浏览器) | 页图 + 待审修正 → 采纳/驳回 | 过程 |
| 8 | 落 md 对账 | 确定性 Python | `res.json` + 已采纳修正 → 覆写 `<stem>.md` | 双 |

第 1–3 步是转换主链(GPU + 确定性),第 4–8 步是后处理(可离线、可延后、可只跑其中某一步)。
人工确认门是红线:第 6 步生成的修正一律 `status=pending`,`accepted` 前不进 md。

## 路线 B(born-digital):`--born-digital-mode` 与 source audit

分诊判路线 B(PDF 已有可信文本层)后,三个入口(`convert.py`/`watchdog.py`/
`batch.py`)都接受 `--born-digital-mode {defer,ocr,hybrid}`(默认 `defer`),
决定这份文本层怎么用:

- **`defer`(默认,完全回退)** —— 不转换,只登记到
  `<out>/_deferred_born_digital/<stem>.txt`;PDF 原文本层完全不被信任、也
  不被消费。
- **`ocr`** —— 忽略文本层,逐页栅格化走完整 OCR 主链(manifest 路由仍记
  `"B"`)。这是块级采信门判定不可靠/失灵时的**内容级回退开关**——怀疑文本层
  质量或采信逻辑本身可疑时,应该退到 `ocr` 而不是 `hybrid`。
- **`hybrid`** —— 块级混合采信:**结构/公式/表格/图片永远走 OCR**
  (`display_formula`/`inline_formula`/`formula_number`/`table`/`image` 等
  非正文标签块绝不采信文本层),只有**纯正文块**在采信门判定该块健康
  (字符/长度比例、归一化编辑距离等阈值内)时才用 PDF 文本层替换 OCR 结果;
  判不健康的正文块仍回退到 OCR。每个块的 provenance(`content_source`:
  `source_text`/`ocr`,以及回退原因)都记录在 `<stem>_source_audit.json` 里。

### 独立 source audit CLI

`source_audit.py` 是只读、独立于转换主链的审计工具——**永远只做 dry-run**,
只读 PDF 与已落盘的 OCR `res.json`,绝不调用 OCR 引擎、绝不改写 Markdown/
任何产物:

```bash
python -X utf8 -m scripts.pipelines.textbooks.source_audit \
    --src <PDF> --out <DELIVERABLES> --work-dir <WORK> --stem <STEM>
```

报告写到 `<out>/<stem>/<stem>_source_audit.json`(与 convert.py 主链复用
同一文件;独立重跑该 CLI 时报告里 `adoption_source` 恒为 `"dry_run"`——采信
只是现场推演用于审计分派,不代表任何内容已被改写)。

文档级 `summary.status` 取以下四种之一:

- **`OK`** —— 全部页均可打分且无 issue。
- **`SUSPECT`** —— 至少一页判定有问题(`missing_prose`/`prose_mismatch`/
  `numeric_mismatch`/`ocr_addition`/`sequence_disorder`/`adoption_error`/
  `audit_error` 等 issue code)。
- **`UNSCORABLE`** —— 全文档没有任何可打分页(例如页面几何无法标定、检查点
  缺失/不完整)。
- **`NOT_APPLICABLE`** —— 路线 A(无文本层)文档;审计天然不适用,只落一份
  显式的最小报告,不静默不写。

`batch.py` 汇总时进一步把 issue 计数分成 **severe**(`adoption_error`/
`audit_error`/`numeric_mismatch`/`numeric_missing`/`sign_flip`/
`decimal_shift`/`exponent_change`)与 **mild**(其余 issue code)两级,便于
快速判断哪些书需要优先人工复核。

**审计能证明什么、不能证明什么**:source audit 检测的是文本层与 OCR 输出
之间的**结构与数值保真信号**(字符/token 召回、块级 NED、数字 token 召回、
页面顺序一致性等)——它不宣称、也不能证明公式或表格的**语义**是正确的
(公式块只记录源文本层字符统计,不做 LaTeX 内容对比;表格审计只给结构/
数值层面的信号,不判断表格语义是否正确)。

### Stage 9: 公式 Agent 终检(formula_agents)

转换的最后一道处理环节。把候选漏斗里的可疑公式送给冻结模型链
(Kimi → Gemini → Codex → Claude,失败/低置信才逐级降级),通过五道确定性
准入闸后自动应用,直接产出最终 Markdown。

```powershell
# 默认:全自动应用
.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.formula_agents.cli `
  --stem <STEM> --deliverables-root <DELIV> --work-dir <WORK> --pdf <PDF>

... --dry-run     # 只估算,不调模型
... --propose     # 只落 pending 供 debug 抽查,不改 md
... --rollback    # 回滚最近一轮
```

五道准入闸(任一不过 → 该条不改,绝不改坏):

1. **KaTeX 可渲染门** —— 建议 LaTeX 必须能被 KaTeX 解析(node 缺失时整轮降级 propose)
2. **相似度门** —— 长度比 `[0.5, 2.0]`、符号重合度 ≥ 30%,挡幻觉与候选串位
3. **退化门** —— 空值 / 错误话术(额度耗尽、rate limit、拒答)不得冒充公式
4. **全局熔断** —— 单轮修改比例 > 60% 视为模型状态异常,整轮降级 propose
5. **回归守卫** —— 应用后 KaTeX 硬错增加即**自动回滚**整轮

debug 视图不再是必经环节,降级为可选抽查工具。

## 各 stage CLI(积木,可单独跑)

所有涉及产物的 CLI 都接 `--out`(交付根)与 `--work-dir`(过程根,默认 `<out>/_work_root`)。

```bash
V=.venv-textbooks/Scripts/python.exe

# 单本转换(可续跑;指纹或 --dpi 变则自动清空重跑)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out [--work-dir ./scratch] [--dpi 150]

# 无人值守单本(进程崩溃自动续跑,直到跑完或超上限)
$V -m scripts.pipelines.textbooks.watchdog --src book.pdf --out ./out [--work-dir ./scratch]

# 批量(目录/多文件;--resume 跳过已跑完;每本转完默认自动跑 KaTeX 扫描)
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out [--work-dir ./scratch] [--resume] [--no-katex-scan]
# 对含干净文本层的教材也逐页栅格化并 OCR；默认每连续运行 6h，GPU 休息 40min 后自动续跑
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --force-ocr --work-hours 6 --rest-minutes 40
$V -m scripts.pipelines.textbooks.batch --list          # 只列出待处理 PDF

# 路线 B(born-digital)采信模式:三入口均支持,watchdog/batch 原样透传给
# convert.py 子进程(默认 defer,登记不转)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --born-digital-mode hybrid
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --born-digital-mode ocr

# 独立 source audit(只读 dry-run,不改 md/不调 OCR 引擎)
$V -m scripts.pipelines.textbooks.source_audit --src book.pdf --out ./out --work-dir ./scratch --stem book

# KaTeX 硬报错扫描(单独跑;node 缺失优雅跳过)
$V -m scripts.pipelines.textbooks.katex_scan --md ./out/book/book.md --out ./scratch/book/book_render_errors.json

# 建公式修复工作单(裁疑似块)
$V -m scripts.pipelines.textbooks.debug_repair --out ./out --work-dir ./scratch --stem book [--src book.pdf]

# 公式视觉复核候选 dry-run(只聚合疑点+估算成本,不调用模型/不改 md)
$V -m scripts.pipelines.textbooks.formula_candidates --out ./out --work-dir ./scratch --stem book

# 公式视觉修复(读裁图 → pending corrections;默认 claude 后端)
$V -m scripts.pipelines.textbooks.vision_repair --out ./out --work-dir ./scratch --stem book [--backend claude|agy|codex|kimi]

# 人工确认门 / 调试可视化(浏览器审阅;debug.html 落过程根)
$V -m scripts.pipelines.textbooks.debug_view --out ./out --work-dir ./scratch --stem book [--serve]
```

> 注:`--backend` 多后端封装为独立计划,若尚未落地则 vision_repair 仅 claude 后端。

## 大文件 / 无人值守

单本大部头(700+ 页)转换耗时以小时计(本机 ~50s/页@DPI150),断点续跑 + 磁盘有界:

- 逐页流式:任一时刻临时目录仅 1 张 PNG,检查点为 `_work/page_NNNN_res.json`(每页)。
- 断点续跑:重跑同命令自动跳过已完成页;PDF 内容或 `--dpi` 变则自动清空重跑。
- 坏页隔离:单页异常记入 manifest `failed_pages`,不影响其它页。
- 毒页兜底:反复让进程硬崩的页超阈值标 `process-killed` 跳过。
- 无人值守:`watchdog.py` 反复拉起 convert,进程级崩溃(CUDA/驱动/OOM)自动续跑。
- 节流休息:默认每连续 OCR 6 小时，在已完成页的边界暂停 40 分钟后自动继续；这段时间 GPU 空闲、系统保持唤醒。三个入口均可用 `--work-hours` / `--rest-minutes` 调整。
- 强制 OCR:`--force-ocr` 仅影响本次命令；即使 PDF 有干净文本层也会先栅格化、完全忽略其文本层，并在 manifest 中记为路线 `F`。默认分诊行为不变。

几万页规模:把 `--work-dir` 指向本地大盘,交付根(小)留同步/打包位置。

## 测试

```bash
.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/
```

## 模块速览

- **paths.py** — `DocLayout`/`resolve_layout`:双根布局路径的单一真相源。
- triage.py — 文本层可信度判 A/B/C。
- preprocess.py — PDF→PNG 栅格化。
- engine.py — PaddleOCR-VL 封装(惰性单例,GPU)。
- reconstruct.py — `parsing_res_list` → md(按 order 重组、公式编号 `\tag` 绑定、页眉页脚剔除、着重号还原)。
- selfcheck.py — Tier0 block 覆盖 + KaTeX 不兼容扫描 + 公式疑似汇总。
- corrections.py — 公式修正叠加层(pending/accepted 门控)。
- source_audit.py — 路线 B 文本层抽取/健康度/bbox 对齐 + 文档级 source audit 报告(独立只读 CLI)。
- prose_adoption.py — 路线 B `hybrid` 模式正文块级采信门(健康块用文本层替换 OCR)。
- checkpoint.py / watchdog.py / batch.py — 断点/毒页/无人值守/批量编排。
- katex_scan.py — KaTeX 硬报错扫描薄壳(调 `debug_assets/scan_katex_errors.mjs`)。
- debug_repair.py / vision_repair.py — 公式视觉修复两步(裁图工作单 / AI 读图出修正)。
- debug_view.py — 调试可视化 + 人工确认门(浏览器)。
