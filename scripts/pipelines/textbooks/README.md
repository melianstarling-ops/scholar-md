# textbooks 管线

扫描/教科书 PDF → Markdown（PaddleOCR-VL + 确定性页级重组 + 可选 AI 质量修复）。
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
  └─ <stem>.assets/     ← 成果图片        ├─ _work/_derived_v1/        页级 adopted/fragments/MD 缓存
                                          ├─ <stem>_repair/            修复裁图 + worklist.json
                                          ├─ <stem>_render_errors.json
                                          ├─ <stem>_corrections.json
                                          ├─ <stem>_selfcheck.json
                                          ├─ <stem>_source_audit.json  路线 B source audit 报告
                                          ├─ <stem>_quality_repair/    events/ledger/snapshot/validation
                                          └─ <stem>_debug.html
```

删掉整个过程根,交付根仍是一套完整可分发成果(md 靠相对路径引用 `.assets/`)。
`--work-dir` 省略时默认 `<out>/_work_root`,"一条命令就能跑"不变。

## 完整工作流

三个正式入口（`convert.py` / `watchdog.py` / `batch.py`）默认执行同一条
`--repair auto` 闭环：

| # | 阶段 | 输入 → 产出 |
|---|------|-------------|
| 1 | 分诊与页结果 | PDF → 路线；复用已有 `_work/page_NNNN_res.json`，只对缺失/失效页 OCR |
| 2 | 页级派生产物 | 每页 adopted text、block fragments、最终页片段 → `_derived_v1/page_NNNN.json` |
| 3 | 源文档与候选审计 | source audit + formula + quality detectors → 详细 `events.jsonl` 与有界摘要 |
| 4 | 确定性修复 | 可证明的候选直接形成 proposal；不写正式 Markdown |
| 5 | Agent 路由 | 仅把仍有歧义的页/块送给用户显式指定的 Agent；source-grounded repair 先形成暂存 block correction |
| 6 | 页级重建 | 只重建 correction/proposal 命中的 affected pages；其余页直接复用页缓存，不重跑 OCR |
| 7 | 最终门禁 | 页面完整性、source audit、定界符、资产、KaTeX、指纹与冲突检查 |
| 8 | 单次发布 | 全部通过后一次性原子写回 Markdown/corrections/页缓存；失败保持正本不变 |

同一本书的转换、修复和独立 quality CLI 共用文档锁，避免两个进程同时修改正本。
旧产物首次进入新版本时会从现有 OCR 页 JSON 与 Markdown 精确迁移页缓存；迁移后
再运行只读取命中页，不再整书 hybrid 重放。

source audit 报告当前为 schema 6。freshness 同时绑定 PDF、完整 OCR result-set、
corrections 内容哈希和算法 schema；报告中的块定位使用 page JSON 的真实
`block_id`，循环位置另存 `source_index`。旧 schema 或任一输入漂移都会触发重审，
`batch --resume` 不会误信旧 `latest.json`。报告还为每页保存强 input fingerprint；
单页 OCR/correction 变化只重审该页，其余 page report 直接复用，
`summary.audit_reuse` 记录 reused/recomputed 页数。

## `--repair`:默认自动修复闭环

```powershell
# 默认 auto：确定性检查/修复一路执行到底；没有歧义时零外部模型调用
.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.convert `
  --src book.pdf --out ./out

# 在命令开始时显式指定模型与 fallback 顺序；候选可并行，最多两轮
... --repair auto `
    --repair-agent codex:gpt-5.6-sol:high `
    --repair-agent claude:claude-sonnet-4-6:medium `
    --repair-workers 4 --repair-max-rounds 2

# 完全关闭自动修复
... --repair off
```

未给 `--repair-agent` 时绝不会偷偷调用外部 provider：管线仍完成确定性公式修复、
开放式质量审计和安全 proposal；无法证明的候选保留为 unresolved，并以状态码 `2`
结束，避免把可疑产物宣称为完成。统一退出码为 `0=OK`、`2=SUSPECT`、`1=FAILED`。

`--repair-agent` 格式固定为 `provider:model:effort`，可重复；顺序是单个候选的
fallback 顺序，`--repair-workers` 控制不同候选的并发数。Agent 只读最小证据包，
不能直接写 Markdown。`--repair-max-rounds` 防止新修复暴露后无限循环。

旧参数 `--formula-repair`、`--quality-repair` 和 `--quality-agent` 仍保留为显式
兼容覆盖，但不再代表默认策略。新命令应优先只使用统一的 `--repair` 参数。

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
`decimal_shift`/`exponent_change`/`severe_prose_degradation`)与
**mild**(其余 issue code)两级,便于
快速判断哪些书需要优先人工复核。

**审计能证明什么、不能证明什么**:source audit 检测的是文本层与 OCR 输出
之间的**结构与数值保真信号**(字符/token 召回、块级 NED、数字 token 召回、
页面顺序一致性等)——它不宣称、也不能证明公式或表格的**语义**是正确的
(公式块只记录源文本层字符统计,不做 LaTeX 内容对比;表格审计只给结构/
数值层面的信号,不判断表格语义是否正确)。

### Stage 9: 公式 Agent 终检(formula_agents)

把候选漏斗里的可疑公式送给调用方配置的模型链，通过五道确定性准入闸。
独立 CLI 可直接应用；统一 `--repair auto` 中只写隔离候选和暂存 corrections，
与后续 quality 修复一起通过最终门禁后单次发布。

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

### Stage 10:开放式质量审计与受控修复(quality_repair)

`quality_repair` 以**最终组装 Markdown + fresh source audit**为质量输入，独立于
OCR 页循环运行。默认
由三个正式转换入口的 `--repair auto` 挂接；单独运行时仍通过 `--mode`
明确选择 audit/propose/apply。首批 detector 覆盖页面完整性、最终 `$`
定界符、未排序块、公式信号、图片资产和 source-grounded severe 事件，另有
`novel_discovery` 通过页级统计异常发现尚未注册的新问题。

```powershell
# 独立只读审计(默认模式,不调 Agent、不改 Markdown)
.\.venv-textbooks\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.quality_repair `
  --stem <STEM> --deliverables-root <DELIV> --work-root <WORK> --mode audit

# 允许 Agent 生成 proposal,仍不改 Markdown;模型与 fallback 顺序完全由调用方指定
... --mode propose --agent codex:gpt-5.6-sol:high `
    --agent gemini:Gemini-3.1-Pro:high --agent-workers 4 `
    --max-rounds 2 --learn package

# 事务应用:重叠 proposal/P0/指纹漂移/任一门禁失败均不写最终 Markdown
... --mode apply --agent codex:gpt-5.6-sol:high
```

`--agent` 格式固定为 `provider:model:effort`,可重复;工具不会补入未声明的
provider。无 Agent 时仍完成全部确定性审计和安全修复。`--learn package` 只对
Agent 已确认的 `novel` 或可泛化 `repair` 生成开发交接包,不会在转换进程中修改
生产代码、commit、merge 或 push。

每次运行写入
`<work-root>/<stem>/<stem>_quality_repair/<run-id>/`。`events.jsonl` 保存逐页/
逐块的完整机器路由事件；`summary.md` 只保留有界的人读汇总，二者不互相替代。
detector/Agent 只能产出 finding/proposal；source-grounded Agent repair 产出绑定
真实 page/block/fingerprint 的暂存 accepted correction，而不是直接改 Markdown。
唯一正式写入者是 transaction。apply 先在临时副本上执行，按受影响页重建并刷新
暂存 source audit，再通过非空、定界符回归、资产回归和 KaTeX 回归门。批量入口会
把 P0/P1、冲突、回滚或运行错误汇总为 `QR_FLAG`。`02_Source/` PDF 始终只读。

### Stage 11:学习包蒸馏与受控升级(quality_learn)

`quality_learn` 与转换进程分开运行。它消费一个或多个 Stage 10 learning package，
把首次出现的新问题按 `issue_family` 稳定聚类，再依次执行 `plan → develop → review`。
转换时使用哪个审阅模型，仍在最初命令里通过 `--quality-agent` 指定；代码升级使用
哪个 coding/review Agent，则在本 CLI 启动时显式指定，不存在隐藏 provider。

```powershell
# 1. 只聚类和生成实施计划；不调 Agent、不改代码
.\.venv\Scripts\python.exe -X utf8 -m scripts.pipelines.textbooks.quality_learn `
  --run <quality-repair-run> --mode plan --learn-run-id <LEARN_RUN>

# 2. clean worktree + 基线全绿后才调用 coding Agent
# Agent 只能返回两个受限 diff：先加红测，再加实现；任一步失败自动恢复原文件
... --run <quality-repair-run> --mode develop --learn-run-id <LEARN_RUN> `
  --agent codex:gpt-5.6-sol:high

# 3. 独立模型复跑全套回归并 review；不得与实际 develop Agent 相同
... --run <quality-repair-run> --mode review --learn-run-id <LEARN_RUN> `
  --review-agent claude:claude-sonnet-4-6:medium
```

`develop` 的硬门包括：learning package 八件套完整、工作树 clean、改前测试全绿、
red-test patch 只能写 tests、受限路径检查、红测确实失败、实现后目标测试与完整
textbooks 测试全绿。成功只留下待审 diff 和 run 报告；失败自动按文件快照回滚。
`review` 再核对 develop 后文件指纹，使用只读 Agent 输出 `approve|revise|reject`。
两个模式都不会 commit、merge 或 push，也永不允许补丁触碰 `02_Source/`。

## 各 stage CLI(积木,可单独跑)

所有涉及产物的 CLI 都接 `--out`(交付根)与 `--work-dir`(过程根,默认 `<out>/_work_root`)。

```bash
V=.venv-textbooks/Scripts/python.exe

# 单本转换(可续跑；默认 --repair auto，页缓存复用 + 只重建受影响页)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out [--work-dir ./scratch] [--dpi 150]

# 无人值守单本(进程崩溃自动续跑；0/2 终止，只有 1 才按策略重试)
$V -m scripts.pipelines.textbooks.watchdog --src book.pdf --out ./out [--work-dir ./scratch]

# 批量(目录/多文件；--resume 会继续未闭环的 repair，不把 SUSPECT 当完成)
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out [--work-dir ./scratch] [--resume] [--no-katex-scan]
# 对含干净文本层的教材也逐页栅格化并 OCR；默认每连续运行 6h，GPU 休息 40min 后自动续跑
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --force-ocr --work-hours 6 --rest-minutes 40
$V -m scripts.pipelines.textbooks.batch --list          # 只列出待处理 PDF

# 路线 B(born-digital)采信模式:三入口均支持,watchdog/batch 原样透传给
# convert.py 子进程(默认 hybrid,块级混合采信;defer/ocr 为回退开关)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --born-digital-mode defer
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --born-digital-mode ocr

# 统一自动修复(三入口同义；未给 Agent 时只跑确定性能力)
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --repair auto
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --repair auto \
  --repair-agent codex:gpt-5.6-sol:high --repair-workers 4 --repair-max-rounds 2

# 旧版分阶段覆盖仍兼容
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --formula-repair agents
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --formula-repair deterministic

# 独立覆盖通用质量阶段
$V -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --quality-repair audit
$V -m scripts.pipelines.textbooks.batch --src ./pdfs --out ./out --quality-repair propose \
  --quality-agent codex:gpt-5.6-sol:high --quality-learn package

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
- quality_repair/ — 最终 MD detectors、开放式发现、显式 Agent 路由、proposal
  仲裁、事务门禁、learning package 与独立 CLI。
- quality_learn/ — learning package 聚类、受限双 patch 红→绿开发、失败恢复、
  独立 Agent review 与审计报告。
