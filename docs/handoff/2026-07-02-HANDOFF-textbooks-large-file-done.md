# HANDOFF — textbooks 大文件稳健化完成，接续交接

> 撰写：2026-07-02。走 git（跨会话/跨机可见）。读完本文 + `git log` + `.superpowers/sdd/progress.md` 即可接续。
> 承前：[首版完成交接](2026-07-01-HANDOFF-textbooks-v1-done.md)、[book 引擎交接](2026-07-01-HANDOFF-book-engine-dev.md)。

## §0 当前状态（一句话）

textbooks 管线**大文件稳健化已建成并全 review 通过**（分支 `feature/textbooks-engine`，领先 main ~40 commit）；
**所有者决定：分支保持现状、等引擎开发完毕再合并 main**。下一步（另开会话）：**batch.py 外部工作区调用**。

## §1 本次会话完成

- **大文件稳健化（首版交接 §3 的重任务 3）** — 让单本 700+ 页大部头转换稳/可续/磁盘有界/可无人值守：
  - **逐页流式**：栅格化第 i 页→predict→立即删该 PNG→下一页。磁盘峰值 = 1 张 PNG（砍掉"分块"参数，YAGNI）。
  - **断点续跑**：每页 `_work/page_{i:04d}_res.json` 作检查点；PDF 指纹（page_count+size_bytes）**+ DPI** 失配 → 清空重跑（防混合精度）。
  - **坏页隔离**：单页栅格化/predict 异常 → 记 `failed_pages`(kind=page-exception)，继续下一页。
  - **进程级崩溃恢复**：`watchdog.py` 子进程反复拉起 convert（进程被 CUDA/驱动/OOM 杀 → 自动续跑）；
    **毒页检测**——`in_progress` 面包屑 + `attempts_by_page` 计数（仅启动 `resolve_poison` 自增，与页序解耦），
    某页硬崩进程达 `MAX_HARD_ATTEMPTS`(2) → 标 process-killed 跳过，看门狗兜底 `MAX_RESTARTS`(50)。
  - **收尾**：每次运行从检查点重组 md + Tier0 自检（部分完成也产出部分 md + manifest）。
- **代码**：新建 `checkpoint.py`(139 行,纯确定性/无 GPU)、`watchdog.py`(49 行)；改 `preprocess.py`(单页栅格化+默认 DPI 150)、`convert.py`(重写为可续跑编排)。engine/reconstruct/selfcheck/triage **未动**。
- **测试**：67 单测全绿（checkpoint 21 / convert 13 / watchdog 4 / preprocess 3 + 既有 reconstruct 11/selfcheck 5/triage 10）。
  **全程打桩 `predict_page`、零 GPU**，任何机器 0.78s 跑完。命令：`.venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v`（本机权限坑需加 `--basetemp=./.pytest_tmp`，见 lessons L-T22）。
- **流程**：brainstorming（设计 spec）→ writing-plans（8 任务 TDD 计划）→ subagent-driven-development（每任务实现→两阶段 review→修复；全分支 opus review）。
  全分支 review 抓出 1 个单任务 review 看不到的**跨任务 Important bug**（毒页计数被前置失败页重置），已修复+复评（见 lessons L-T19）。
- **环境**：家用机 A（RTX 5060/Blackwell）重建 `.venv-textbooks`——**cu126 wheel 起不来，须 cu129**（lessons L-T21）。
- **文档**：设计 `docs/superpowers/specs/2026-07-02-textbooks-large-file-robustness-design.md`、计划 `docs/superpowers/plans/2026-07-02-textbooks-large-file-robustness.md`、
  调研提示词 `docs/research-prompts/2026-07-02-*`、经验 `04_Docs/lessons/lessons_textbooks_dev.md`(L-T17~L-T22)。

## §2 关键性能事实（2026-07-02 实测，决定后续方向）

- **本机原生路径地板 ≈ 50s/页**（DPI 150）。便宜杠杆已耗尽：`PADDLEOCR_VL_LOCAL_BATCH_SIZE=1` 硬锁批处理、精度已 bf16、DPI 已优化。
  病根是默认动态图后端逐块串行自回归解码（GPU 利用率 96% 但功耗仅 45W/145W）。**非 GPU 架构问题**（RTX 4060 跑同页也 ~78s）。
- **要 ~2s/页（30x）只能上 vLLM 服务化**，Windows 需 WSL2/Docker（Blackwell 上 SGLang/FastDeploy 不支持 CC≥12.0，两卡统一只能 vLLM）。
- 完整调研 `04_Docs/PaddleOCR-VL 1.6 文档 OCRVLM 推理性能优化可执行调研报告.md`（私有）。所有者决定：**先接受 50s/页做稳健化，暂缓 vLLM**。

## §3 接续任务（按优先级）

### 下一个（所有者已定，另开会话）
1. **batch.py 外部工作区调用**（首版交接 Task 2）：convert 现只吃单文件；需 `--src` 吃文件/目录/多个、`--out`、`--flat`、
   `--no-selfcheck-json`、`--resume`、env 回退（`SCHOLARMD_*`）、`--list`。参考 `scripts/pipelines/patents/batch_patents.py`。
   **建议开场**：说"brainstorm batch.py 设计"或"用 subagent 驱动开发实现 batch.py"，走 brainstorming→writing-plans→subagent-driven 链。

### 验收 / 落地（本次新产生）
2. **真实 GPU 端到端分档实测（100→300→800 页）**：大文件稳健化写好但**未在真实引擎上验收**。
   样本 `02_Source/textbooks_samples/Paul_Analysis_MTL_scan.pdf`（803 页）。用 `watchdog.py --src ... --out ...` 起真跑，
   观察：磁盘峰值、显存、单页耗时、断点续跑与坏页是否如期工作、md 质量。
3. **图像预处理 + 推理加速调研落地**：报告已回。结论：VLM 别做激进 CV 预处理、DPI 150 已定、要提速须 vLLM。
   待小样本验证有无必要并入 `preprocess`/`engine`，vLLM 与下方 §3.7 合并。

### 重任务（承首版交接 §3，未做）
4. **系统量化质量基准**：现有 KaTeX lint + block 覆盖 + golden 属"质量护栏(Tier0)"，**非量化指标**。
   报告 §6 要的（中文 CER 逐字、公式→LaTeX 渲染正确率、双栏阅读序、表格 TEDS）**一个都没做**，需定标注基准样本 + 指标。
5. **debug_view textbooks 版 HTML**：移植 patents `debug_view.py`，左原图+parsing_res_list 识别块/右 md 渲染(KaTeX)。设计 §8.1。
   （本次保留了每页 res.json 检查点，为其铺路。）
6. **Tier1 Opus AI 审查**：页图 vs md 对照批量标错。设计 §8。
7. **B 路文本层直取**（优质 born-digital 首版只登记 `_deferred_born_digital`）；**triage 阈值标定**（COVERAGE_MIN=50/BADNESS_MAX=0.25 初值）；
   **vLLM 加速**（WSL2/Docker，量产前搭）。

### 轻 Minor（首版交接 §3.1 剩余）
8. review Minor 实剩 2 个：③ `text_badness` 与 `sample_text_coverage` 重复 open PDF（等 batch）；④ `selfcheck` 12-char probe 近重复短块（scale 才暴露）。
9. **watchdog `restarts` 回写 manifest**（audit-only，spec §4 已标延后）。

## §4 需所有者确认的悬项

- **lessons L-T1~L-T15 去向**：本 handoff 的前身与 `TODO.md` 均记"已写入 `04_Docs/lessons/lessons_textbooks_dev.md`(L-T1~L-T15)"，
  但 2026-07-02 本机 OneDrive 镜像**创建前无此文件**（同目录 6 月的 lessons 正常）。本次新经验已从 **L-T17** 起编号避让。
  请确认 L-T1~L-T15 是否在另一台机器的 OneDrive 副本、或已丢失需重写。

## §5 恢复方法 / 红线（继承）

- 进度 ledger：`.superpowers/sdd/progress.md`（各任务 commit + review 结论 + Minor + 最终 review 结果）。
- 全部待办：`TODO.md` 2026-07-02 节（私有 OneDrive）。复杂开发照 SOP-08、经验落档照 SOP-06。
- 红线：不改 patents/general/engine；确定性优先、ML 只判断不改字符；`02_Source/` 只读；每管线独立 venv；对外操作（merge/push/装大依赖）前所有者确认。
