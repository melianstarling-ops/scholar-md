# HANDOFF — textbooks 首版完成，接续任务交接

> 撰写：2026-07-01，会话即将压缩上下文。本文档走 git（跨会话/跨机可见）。
> 读完 + `git log` + `.superpowers/sdd/progress.md` 即可接续。

## §0 当前状态（一句话）

textbooks 教科书管线**首版已建成并全部 review 通过**，在分支 `feature/textbooks-engine`（领先 main 19 commit），
**所有者决定：等引擎开发完毕后再合并 main**，现分支保留原地不动。

## §1 已完成（本轮）

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
1. **6 个 review Minor cleanup**：① triage `sample` 死参数（移除或 wire）② reconstruct 着重号正则 `\x5c` hex→plain raw-string
   ③ text_badness 与 sample_text_coverage 重复 open PDF（共享采样）④ selfcheck 12-char probe 近重复短块 ⑤ preprocess 加 try/finally
   ⑥ convert resumability（属功能，可归下面）。改后须保持测试全绿。

### 重任务（需较多新上下文，独立开工）
2. **batch.py 外部工作区调用**（对齐 patents/AGENTS H.5）：当前 convert.py **只吃单文件**；需 `--src` 吃文件/目录/多个、
   `--out`、`--flat`、`--no-selfcheck-json`、`--resume`、env 回退（`SCHOLARMD_*`）、`--list`。参考 `scripts/pipelines/patents/batch_patents.py`。
3. **大文件分块（≤50 页）**：convert 现整份处理，700+页大部头须分块 predict + 按页拼接（借 patents `reading_order._assemble_paragraphs`）。
   注：所有者经验旧引擎超 50 页直接报错。
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
