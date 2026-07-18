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
                                          ├─ <stem>_source_audit.json  路线 B source audit 报告
                                          └─ <stem>_debug.html
```

删掉整个过程根,交付根仍是一套完整可分发成果(md 靠相对路径引用 `.assets/`)。
`--work-dir` 省略时默认 `<out>/_work_root`,"一条命令就能跑"不变。

## 完整工作流

| # | 阶段 | 执行者 | 输入 → 产出 | 碰交付/过程 |
|---|------|--------|------------|:-:|
| 1 | 分诊 triage | 确定性 Python | PDF → 路线 A/B/C（B=born-digital，默认 `--born-digital-mode hybrid` 块级混合采信；`defer`/`ocr` 为回退开关；`--force-ocr` 时改走 F=强制 OCR） | — |
| 2 | 逐页 OCR | GPU 引擎 PaddleOCR-VL | PDF 页 → `_work/page_NNNN_res.json` + `.assets/` 裁图 | 双 |
| 3 | 重组 + 自检 | 确定性 Python | `res.json` → `<stem>.md` + `_selfcheck.json` | 双 |
| 4 | KaTeX 报错扫描 | Node + KaTeX(经 katex_scan 薄壳) | `md` → `_render_errors.json` | 双 |
| 5 | 建修复工作单 | 确定性 Python(重栅格化源 PDF) | `res.json`+`render_errors` → `_repair/` + `worklist.json` | 过程 |
| 6 | 公式视觉修复 | AI(claude/agy/codex/kimi CLI) | `worklist.json` 裁图 → `_corrections.json`(status=pending) | 过程 |
| 7 | 人工确认门 | 人(debug_view 浏览器) | 页图 + 待审修正 → 采纳/驳回 | 过程 |
| 8 | 落 md 对账 | 确定性 Python | `res.json` + 已采纳修正 → 覆写 `<stem>.md` | 双 |

第 1–3 步是转换主链(GPU + 确定性),第 4–8 步是后处理(可离线、可延后、可只跑其中某一步)。
人工确认门是红线:第 6 步生成的修正一律 `status=pending`,`accepted` 前不进 md。

## `--formula-repair`:转换收尾自动接公式修复环

三个入口(`convert.py`/`watchdog.py`/`batch.py`,`batch.py` 默认值有意不同——见下)
都接受 `--formula-repair {deterministic,agents,agents-apply,off}`,决定单本
转换成功(md/`_selfcheck.json` 已落盘)后自动往下接哪一段公式修复环:

- **`deterministic`(`convert.py`/`watchdog.py` 默认,零成本零网络)** ——
  第 4 步 KaTeX 报错扫描(node 缺失优雅跳过)→ 有硬错才跑第 4b 步
  katex_triage 分桶 + 视觉工单(镜像 batch.py 早先就有的收尾自动化)→
  formula candidates 漏斗(确定性聚合 `worklist.json`/`render_errors.json`
  成候选清单,不调用任何模型)。
- **`agents`** —— `deterministic` 全部 + 第 9 步公式 Agent 五道门(冻结模型链,
  外部 LLM 调用),以 `propose` 模式调用 `run_agents`——corrections 只落
  `status=pending`,人工审阅档,md 不改。adapters 全不可用(未装 CLI/未登录)
  时优雅降级为 `deterministic` 行为(不调用任何 agent,只留明确记录),不崩。
- **`agents-apply`** —— `deterministic` 全部 + 第 9 步公式 Agent 五道门,以
  `apply` 模式全自动应用(所有者 2026-07-18 裁决:撤销旧版"人工 accept 红线",
  安全兜底改由 orchestrator 内建机制承担——五道准入闸/置信阈值 0.8/熔断阈值
  0.6/应用后自动回滚/`.pre_agent.bak` 快照,编排层不再额外拦截)。额外只对
  这一档传 `collect_fn=crops_only_collect`,过滤掉无裁图的纯 KaTeX 警告(大书
  实战教训:数百条无害警告会灌爆候选,只留"有裁图 OR 硬错"再交给 agent)。
  adapters 全不可用时同样优雅降级为 `deterministic` 行为,不崩。
- **`off`(`batch.py` 默认)** —— 现状,只转换不后处理,零调用。

后处理失败隔离(硬要求):转换本体成功、md/`_selfcheck.json` 已落盘之后才会
进入这段编排;其中任一步骤异常都只记进返回字典的 `formula_repair` 字段(状态/
产物路径/错误摘要),绝不影响已产出的 md/selfcheck、也绝不让本次转换整体失败。
`agents-apply` 同样受此隔离保护——即便应用/熔断/回滚过程本身抛异常,已写出的
md/selfcheck/audit 也不受影响。

`batch.py` 默认 `off` 而非 `deterministic`,是刻意取舍而非疏漏:batch 收尾早
就有自己一套 katex_scan + katex_triage 自动化(见"各 stage CLI"一节),默认
打开、已有回归测试覆盖。若默认也转发 `deterministic` 给每本书的 `convert.py`
子进程,同一本书的 katex_scan/katex_triage 会被跑两遍(子进程一遍、batch 收尾
一遍)。因此:batch 显式选 `--formula-repair {deterministic,agents,agents-apply}`
时才转发给子进程,并自动让路(跳过)batch 自己对应的收尾步骤,不管
`--no-katex-scan` 传的是什么——两边加起来仍是"跑一遍",不双跑。

```bash
# 默认(deterministic):KaTeX 扫描/分桶/候选漏斗全自动,零网络零成本
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out

# agents:额外接公式 Agent 终检链,corrections 只落 pending,人工审阅档
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --formula-repair agents

# agents-apply:全自动应用(安全由 orchestrator 内建熔断/回滚/快照承担)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --formula-repair agents-apply

# batch 选择性接入(默认 off,沿用 batch 自己已有的收尾自动化)
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --formula-repair deterministic

# 批量全自动:hybrid 采信 + agents-apply 公式全自动修复,分段跑(所有者确认后
# 在自己终端执行)
.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.batch `
    --src .\03_Output\textbooks\_src_staging `
    --out .\03_Output\textbooks `
    --born-digital-mode hybrid --formula-repair agents-apply `
    --work-hours 8 --rest-minutes 15
```

## 路线 B(born-digital):`--born-digital-mode` 与 source audit

分诊判路线 B(PDF 已有可信文本层)后,三个入口(`convert.py`/`watchdog.py`/
`batch.py`)都接受 `--born-digital-mode {defer,ocr,hybrid}`(默认 `hybrid`),
决定这份文本层怎么用:

- **`defer`(回退开关,完全回退)** —— 不转换,只登记到
  `<out>/_deferred_born_digital/<stem>.txt`;PDF 原文本层完全不被信任、也
  不被消费。
- **`ocr`(回退开关)** —— 忽略文本层,逐页栅格化走完整 OCR 主链(manifest
  路由仍记 `"B"`)。这是块级采信门判定不可靠/失灵时的**内容级回退开关**——
  怀疑文本层质量或采信逻辑本身可疑时,应该退到 `ocr` 而不是 `hybrid`。
- **`hybrid`(默认)** —— 块级混合采信:**结构/公式/表格/图片永远走 OCR**
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

报告写到**过程根(work dir)**下的 `<stem>_source_audit.json`(即
`DocLayout.source_audit_path`,`--work-dir` 省略时默认
`<out>/_work_root/<stem>/<stem>_source_audit.json`,显式给 `--work-dir`
时在 `<work>/<stem>/` 下;是过程产物,不进交付根 `<out>/<stem>/`)——与
convert.py 主链复用同一文件;独立重跑该 CLI 时报告里 `adoption_source`
恒为 `"dry_run"`——采信只是现场推演用于审计分派,不代表任何内容已被改写。

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

> 注:上面这段"自动应用"描述的是**独立直接**跑 `formula_agents.cli` 时的
> 默认行为。经 `convert.py --formula-repair {agents,agents-apply}`(或
> `watchdog.py`/`batch.py` 转发)自动接入时,编排层按所选档位透传调用模式:
> `agents` 等价 `--propose`(corrections 只落 `pending`,人工审阅档,md 不改);
> `agents-apply` 等价全自动应用(所有者 2026-07-18 裁决撤销旧版"人工 accept
> 红线"后新增,见上文"--formula-repair"一节),安全由五道准入闸 + 熔断 + 自动
> 回滚 + 快照承担,不再依赖人工逐条确认。

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

# 单本转换(可续跑;指纹或 --dpi 变则自动清空重跑;转换成功后默认自动接
# deterministic 公式修复环,见上文"--formula-repair"一节)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out [--work-dir ./scratch] [--dpi 150]

# 无人值守单本(进程崩溃自动续跑,直到跑完或超上限;--formula-repair 原样透传)
$V -m scripts.pipelines.textbooks.watchdog --src book.pdf --out ./out [--work-dir ./scratch]

# 批量(目录/多文件;--resume 跳过已跑完;每本转完默认自动跑 KaTeX 扫描)
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out [--work-dir ./scratch] [--resume] [--no-katex-scan]
# 对含干净文本层的教材也逐页栅格化并 OCR；默认每连续运行 6h，GPU 休息 40min 后自动续跑
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --force-ocr --work-hours 6 --rest-minutes 40
$V -m scripts.pipelines.textbooks.batch --list          # 只列出待处理 PDF

# 路线 B(born-digital)采信模式:三入口均支持,watchdog/batch 原样透传给
# convert.py 子进程(默认 hybrid,块级混合采信;defer/ocr 为回退开关)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --born-digital-mode defer
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --born-digital-mode ocr

# 公式修复环档位(三入口均支持,见上文"--formula-repair"一节;convert/watchdog
# 默认 deterministic,batch 默认 off——避免与 batch 自己已有的收尾自动化双跑)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --formula-repair agents
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --formula-repair deterministic

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
