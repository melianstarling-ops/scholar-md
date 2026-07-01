# HANDOFF — textbooks 首版完成，接续任务交接

> 撰写：2026-07-01，会话即将压缩上下文。本文档走 git（跨会话/跨机可见）。
> 读完 + `git log` + `.superpowers/sdd/progress.md` 即可接续。

## §0 当前状态（一句话）

textbooks 教科书管线**首版已建成并全部 review 通过**，在分支 `feature/textbooks-engine`（领先 main 19 commit），
**所有者决定：等引擎开发完毕后再合并 main**，现分支保留原地不动。

## §0.1 本次会话增补（2026-07-01 第二会话）

- **KaTeX 渲染兼容清洗（L-T16 落地）**：引擎把 display 积分输出成 `\int\displaylimits_{下}^{上}`，`\displaylimits` 是
  plain TeX 命令、KaTeX（Typora 默认渲染器）不认 → 公式红字。reconstruct 新增 `sanitize_latex()` + 单一清单
  `KATEX_INCOMPAT_COMMANDS`（当前仅 `\displaylimits`），只作用于 `display_formula`；selfcheck 新增 `katex_incompat_scan()`
  Tier0 lint（与清洗层**同源清单**，防漂移）；convert 把结果并入 `selfcheck["katex_incompat"]` 并在 CLI 打印。
- **验收**：`Paul_p200_真实端到端重跑.md` 重跑确认 `\displaylimits` 残留 0、3 条 `\int` 公式保留。**测试 22→27 全绿**。
- **命令普查（本会话新证据）**：扫三关全部产物共 30 个不同 LaTeX 命令，逐一比对 KaTeX 支持表，
  **`\displaylimits` 是唯一不兼容项**（`\underset`/`\mathrm`/`\tag`/`\displaystyle`/`\boldsymbol`/`\begin..end` 均受支持）
  → 当前单命令清单对**已测语料**完整；但语料仅电磁/电动力学（Pozar/Paul/Jackson），换学科可能冒新命令，lint 能检出但不自动清洗。
- **已知盲区（代码注释已标 pending）**：text 块内联 `$...$` 公式暂不接 `sanitize_latex`（语料无实例、对纯文字影响未验证）；
  待出现内联公式红字实例再评估接入。

## §1 已完成（首版本轮）

- **6 模块**（`scripts/pipelines/textbooks/`）：triage 分诊 A/B/C · preprocess PDF→PNG · engine PaddleOCR-VL 1.6 惰性单例 ·
  reconstruct（json→md：公式 `\tag` 绑定、着重号还原、页眉 order=None 剔除）· selfcheck Tier0 block 覆盖 · convert 编排+CLI。
- **22 单元测试全绿** + engine/convert 真实 GPU 端到端 smoke（route=A、公式 `\tag{5.30}~{5.33}` 正确、无页眉页码）。
- **三关实测质量优秀**：英文有层 Pozar / 英文扫描 Paul / 中文扫描 Jackson《经典电动力学》。产物存 `03_Output/textbooks/_firstrun_samples/`。
- **文档**：设计 `docs/superpowers/specs/2026-07-01-textbooks-pipeline-design.md`、计划 `docs/superpowers/plans/2026-07-01-textbooks-pipeline-v1.md`、
  踩坑 `04_Docs/lessons/lessons_textbooks_dev.md`（L-T1~L-T15）、工作流 SOP `01_System/SOP-08_Complex_Dev_Workflow.md`。
- 最终 opus 全分支 review：**Critical/Important 均 None**。

## §2 环境（家用机，已就绪）

- GPU：**RTX 4060 / 8GB**（非文档旧假设的 5060）；实际显存占用 alloc 3–4GB，8GB 足够。
- 独立环境 **`.venv-textbooks`**（已装 paddlepaddle-gpu 3.2.1 cu126 + paddleocr[doc-parser] 3.7.0 + PyMuPDF + pytest；模型 v1.6 已缓存）。
- 跑测试：`.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v`（从仓库根，须 `-m pytest`）。
- ⚠️ 传给 PyMuPDF 的路径用 `D:/` 不用 Git Bash 的 `/d/`（见 lessons L-T1）。

## §3 接续任务（按优先级；标注适合的会话）

### 适合"下会话开头快速做"（轻，代码位置明确，见 `.superpowers/sdd/progress.md` Minor 清单）
1. **review Minor cleanup（原 6 个，commit `77b88ad` 已清 3 个，实剩 3 个）**：
   - ✅ 已清：① triage `sample` 死参数 · ② 着重号正则 hex→raw-string · ⑤ preprocess try/finally
   - ⏳ 实剩：③ text_badness 与 sample_text_coverage 重复 open PDF（progress.md 标"等 batch"，与重构耦合，单独动不划算）
     · ④ selfcheck 12-char probe 近重复短块（scale 才暴露）· ⑥ convert resumability（实为功能，归 batch/大文件）
   - **注：§3.1"轻活"基本清空**，剩 3 个要么绑 batch、要么等规模；下一步应直接进 §3.2。

### 重任务（需较多新上下文，独立开工）
2. **batch.py 外部工作区调用**（对齐 patents/AGENTS H.5）：当前 convert.py **只吃单文件**；需 `--src` 吃文件/目录/多个、
   `--out`、`--flat`、`--no-selfcheck-json`、`--resume`、env 回退（`SCHOLARMD_*`）、`--list`。参考 `scripts/pipelines/patents/batch_patents.py`。
3. **大文件稳健化（原名"大文件分块 ≤50 页"，本会话读代码后重新定义）**：
   ⚠️ **交接原描述两处不准，先纠正**：
   - "convert 现整份处理"**不成立**——[convert.py:31-34](../../scripts/pipelines/textbooks/convert.py#L31) 已是**逐页 predict**
     （`for png in pngs: predict_page(单页)`），引擎每次只吃一张 PNG，"分块 predict"引擎侧本来就做到了。
   - "按页拼接（借 `_assemble_paragraphs`）"**是质量功能不是大文件功能**——patents [reading_order.py:256](../../scripts/pipelines/patents/reading_order.py#L256)
     解决**跨栏/跨页被劈开的段落重接**（当前 convert 用 `"\n\n".join` 硬断页，跨页段落被拆）。与文件大小无关，应单列为"跨页段落重接"质量项。
   - "旧引擎超 50 页报错"是**继承自旧引擎（MD_Book/Marker）的经验，未在 PaddleOCR-VL 逐页管线上验证**。
   **700 页真正的隐患在别处**：① 磁盘暴涨——[preprocess.py:9-19](../../scripts/pipelines/textbooks/preprocess.py#L9) 一次栅格化**全部页** PNG 堆在单一 temp
     （700 页@200DPI≈GB 级，全程占着）；② 无断点续跑——第 690 页崩则前 689 页全废；③ 一坏页毁全书（单页 predict 异常掀翻整份）；④ 全程静默无进度。
   **Task 3 真正内核 = 让大部头转换"稳/可续/磁盘有界"**（每 N 页一块→落盘检查点→丢该块 PNG→下一块），不在引擎调用。
   **⚠️ 开工前置门槛**：先拿一份**真实 300+ 页教科书跑一次现管线**，确认瓶颈到底是磁盘/显存累积/还是根本能跑通——
   避免为一个"继承自旧引擎、未经验证的崩溃"过度设计。所有者尚未确认手上有 300+ 页样本；brainstorming 已起头、设计待明日。
4. **系统量化质量基准**：现质量靠人工对照原图 + golden 断言 + Tier0；未做报告 §6 的量化（中文 CER 逐字、公式→LaTeX 渲染比对、
   双栏阅读序、表格 TEDS）。需定基准样本 + 指标。
5. **debug_view textbooks 版 HTML**（所有者要的"转换前后对照"）：移植 patents `debug_view.py`，左原图+parsing_res_list 识别块/
   右 md 渲染（KaTeX）。设计 §8.1。
6. **Tier1 Opus AI 审查**：页图 vs md 对照批量标错。设计 §8。
7. **B 路文本层直取**（优质 born-digital 首版只登记 `_deferred_born_digital` 不转）；**triage 阈值标定**（COVERAGE_MIN=50/BADNESS_MAX=0.25 为初值）；
   **vllm 加速**（Windows 无原生，需 WSL2/Docker，量产前搭）。

## §4 恢复方法

- 进度 ledger：`.superpowers/sdd/progress.md`（各任务 commit + review 结论 + Minor 清单）。
- 各任务 brief/report/diff：`.superpowers/sdd/task-*.md`、`review-*.diff`。
- 全部待办：`TODO.md` 2026-07-01 节（私有 OneDrive）。
- 复杂开发照 **SOP-08** 走（AGENTS F 表已登记）。

## §5 红线（继承）

不改 patents/general；确定性优先、ML 只判断不改字符；对外操作（merge/push/装大依赖）前所有者确认；每管线独立 venv。
