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
| 1 | 分诊 triage | 确定性 Python | PDF → 路线 A/B/C（B=born-digital 默认登记不转；`--force-ocr` 时改走 F=强制 OCR） | — |
| 2 | 逐页 OCR | GPU 引擎 PaddleOCR-VL | PDF 页 → `_work/page_NNNN_res.json` + `.assets/` 裁图 | 双 |
| 3 | 重组 + 自检 | 确定性 Python | `res.json` → `<stem>.md` + `_selfcheck.json` | 双 |
| 4 | KaTeX 报错扫描 | Node + KaTeX(经 katex_scan 薄壳) | `md` → `_render_errors.json` | 双 |
| 5 | 建修复工作单 | 确定性 Python(重栅格化源 PDF) | `res.json`+`render_errors` → `_repair/` + `worklist.json` | 过程 |
| 6 | 公式视觉修复 | AI(claude/agy/codex/kimi CLI) | `worklist.json` 裁图 → `_corrections.json`(status=pending) | 过程 |
| 7 | 人工确认门 | 人(debug_view 浏览器) | 页图 + 待审修正 → 采纳/驳回 | 过程 |
| 8 | 落 md 对账 | 确定性 Python | `res.json` + 已采纳修正 → 覆写 `<stem>.md` | 双 |

第 1–3 步是转换主链(GPU + 确定性),第 4–8 步是后处理(可离线、可延后、可只跑其中某一步)。
人工确认门是红线:第 6 步生成的修正一律 `status=pending`,`accepted` 前不进 md。

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
- checkpoint.py / watchdog.py / batch.py — 断点/毒页/无人值守/批量编排。
- katex_scan.py — KaTeX 硬报错扫描薄壳(调 `debug_assets/scan_katex_errors.mjs`)。
- debug_repair.py / vision_repair.py — 公式视觉修复两步(裁图工作单 / AI 读图出修正)。
- debug_view.py — 调试可视化 + 人工确认门(浏览器)。
