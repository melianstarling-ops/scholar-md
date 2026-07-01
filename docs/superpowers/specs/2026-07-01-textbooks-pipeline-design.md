# textbooks 管线设计文档 —— 教科书/扫描书 → Markdown

- 日期：2026-07-01
- 状态：设计草案（`superpowers:brainstorming` 产出，待所有者 review）
- 分支：`feature/textbooks-engine`
- 范围：新建 `scripts/pipelines/textbooks/` 管线的引擎选型、架构、流程、自检设计
- 不涉及：patents/general 管线改动；本设计不含任何执行（安装/批量转换留到实现阶段另行确认）

> 立项依据见 [引擎选型综述报告](../../../04_Docs/引擎选型综述报告_v2_2026-07-01.md)（私有）与
> [book 引擎开发交接](../handoff/2026-07-01-HANDOFF-book-engine-dev.md)；本设计的所有质量结论
> 均来自 2026-07-01 家用机三页实测（下 §2），非空谈。

## 1. 背景与目标

`patents`（美国专利几何规则）和 `general`（Marker，born-digital）两条现有管线都**不适合大部头扫描教科书**。
按 [文献批量转换方案](2026-07-01-literature-batch-conversion-design.md) §3 实测：外部书籍 63%（约 82 本、3.6 万页）
是**纯扫描件、无文本层**，含大量中文（CJK）、公式密集、双栏、单本常 700+ 页。这是仓库能力矩阵里
完全空白、又是内容体量大头的部分。

**目标**：本地把这批扫描教科书转成高质量 Markdown（供阅读 + 进知识库），公式转 LaTeX、版面阅读顺序正确、
中文准确，产物可审计、错误可定位。

## 2. 引擎选型结论（三页实测支撑）

**选定 PaddleOCR-VL 1.6**（`paddleocr==3.7.0`，`pipeline_version="v1.6"`，OmniDocBench v1.6 96.33%）。
2026-07-01 家用机（RTX 4060 / 8GB / Windows / Python 3.12）三关实测：

| 样本 | 类型 | 转换质量 | predict | 显存(alloc/reserved) |
|---|---|---|---|---|
| Pozar《Microwave Engineering》p100 | 英文·有文本层 | 公式 LaTeX 惊艳 | 52s | 3.1 / 7.1 GB |
| Paul《MTL》p200 | 英文·纯扫描 | 优秀 + json 保留公式编号 | 78s | 3.5 / 8.2 GB |
| Jackson《经典电动力学》中文版 p200 | **中文·纯扫描** | 优秀·中文 CER 极高 | 48s | 4.0 / 8.1 GB |

结论：中文/英文/公式/扫描件四维**全面过关**，质量足以支撑本管线。显存 reserved 偏高是 paddle 池化 +
测试时 GPU 上并存语音识别引擎所致，**实际 alloc 仅 3–4GB，8GB 足够**（运行前清空其他进程占用）。

后端选择：Windows 上 vLLM/SGLang/FastDeploy **均不支持原生运行**（官方明确），故用 **PaddlePaddle 原生后端**；
transformers 只支持 element-level、发挥不出 page-level 版面能力，不选。

## 3. 架构决策（核心红线）

**走"引擎输出 `parsing_res_list` 结构化 JSON 中间件 → 自建确定性逻辑重组 Markdown"，而非盲信端到端 `.md`。**

实证依据：Paul p200 测试中，端到端 md **丢失了公式编号 (5.30)–(5.33)**，但 json 的 `parsing_res_list`
（18 个块，每块含 `block_label`/`block_content`/`block_bbox`/`block_order`/`block_polygon_points`）
**完整保留了全部编号**。即：md 渲染丢的信息，json 层都在。

这与 `patents` 管线"ML 只做判断、确定性产物作可审计底座"的哲学一致；[book 引擎交接](../handoff/2026-07-01-HANDOFF-book-engine-dev.md) §4
把"是否照搬这条红线"列为待判断题——本设计的判断是：**教科书场景同样采用中间件路线**，因为它带来错误可定位、
公式/表格块可单独重跑、阅读顺序可确定性校验。

## 4. 目录与环境

```
scripts/pipelines/textbooks/          # 新建,与 patents/general 平级、零耦合
  convert.py        # 单文档: PDF → PNG → predict → 自建重组 → Typora md
  preprocess.py     # PDF→PNG 预处理(见 §5.1)
  reconstruct.py    # 从 parsing_res_list JSON 自建重组(阅读顺序/编号/着重号/标题)
  selfcheck.py      # Tier0 确定性自检(见 §7)
  debug_view.py     # 人工复核 HTML(见 §7,移植改造自 patents 版)
  batch.py          # 批量 + 分块 + 断点续跑(见 §6)
  README.md
  requirements.txt  # paddlepaddle-gpu==3.2.1(cu126) + paddleocr[doc-parser]==3.7.0
```

- 独立虚拟环境 **`.venv-textbooks`**（本项目"每管线独立 Python 环境、不混用"规矩；已建、已 gitignore）
- CLI 遵循 AGENTS H.5：`--src`（吃文件/目录/多个）/`--out`（默认就地）

## 5. 管线流程

### 5.1 PDF → PNG 预处理（实证必需）

实测：把纯扫描单页 PDF 直接喂 `PaddleOCRVL.predict()`，input worker 抛异常（图像解码环节）；
改渲染成 PNG（PyMuPDF `get_pixmap(dpi=200)`）喂入则正常。故预处理固化为流程第一步：
**PDF 各页 → 200dpi PNG**（分辨率待实现阶段微调：过高糊、过低丢细节；报告建议 4K 以上降到 1080p–2K 最优）。

### 5.2 引擎识别

`PaddleOCRVL(pipeline_version="v1.6").predict(png)` → 每页产出 `parsing_res_list` JSON（结构化块 + bbox +
阅读顺序）+ 端到端 md（仅作预览参照，不作最终产物）。

> 注：`parsing_res_list` 已实测确认含 `block_label`/`block_content`/`block_bbox`/`block_order`/`block_polygon_points`
> 字段；本文档中出现的具体 `block_label` 取值（如 `formula`/`formula_number`/`table`/`figure`/`title`）为**推测**，
> 实现阶段以实际 json 输出的标签集为准。

### 5.3 自建重组（`reconstruct.py`）

从 JSON 的 `parsing_res_list` 按 `block_order` 重组 Markdown，确定性逻辑处理 §6 的已知问题：
公式编号绑定、着重号还原、章节层级、图文/表格块归位。**这是本管线的核心自研部分**，也是"可审计"的落点。

### 5.4 输出

沿用 general 的 Typora 结构：`<doc_id>.md` + `<doc_id>.assets/`（图片资源）。`doc_id` 命名沿用
[批量转换方案](2026-07-01-literature-batch-conversion-design.md) §4 规则。

## 6. 实测发现与对策

| 发现 | 现象 | 对策（reconstruct.py 实现） |
|---|---|---|
| 公式编号 | 端到端 md 丢 (5.30) 等；json 保留 | 从 `parsing_res_list` 中 `formula_number` 类块，按 bbox 右对齐 + 同行 y 坐标绑回对应公式 |
| 中文着重号 | "没有自由磁荷"字下着重点被转成 `\underset{\cdot}{没}...` 塞进 `$$` | 后处理识别该模式 → 还原为正文（或 `<u>`/去除），不留公式块 |
| 显存 | reserved 顶 8GB | 非问题：实际 alloc 3–4GB；运行前清空其他进程；必要时限 paddle 显存池 fraction |
| 速度 | 48–78s/页（paddle native 无加速） | **入 backlog**：vLLM/SGLang 加速需 WSL2 或 Docker（Windows 不支持原生）；现阶段先用 paddle native 把一条路跑通 |

## 7. 大文件分块

- **硬约束：每块 ≤ 50 页**（所有者经验：旧引擎超 50 页直接报错）。
- 实现：PyMuPDF 按 ≤50 页拆临时子文件 → 逐块 PDF→PNG→predict → 按页码拼接。
- 跨块续接：借鉴 patents `reading_order._assemble_paragraphs` 的三信号续接判据处理块边界段落。
- 断点续跑：借 [批量转换方案](2026-07-01-literature-batch-conversion-design.md) §5.3 的 manifest 状态机，
  按块粒度记录 `done/failed`，中断后只续未完成块。

## 8. 三层自检

扫描件**无源文本层**，patents/general 的"源 PDF 文本 ↔ md 字符多重集对账"（Tier0）在此**失效**。重设为三层：

| 层 | 手段 | 说明 |
|---|---|---|
| **Tier0 确定性** | `parsing_res_list` **block 全覆盖 lint** + 图链完整 + 页/块数一致 | 不靠字符对账，靠"每个识别块都进了 md、无丢块、无孤链"。可自动化、确定性 |
| **Tier1 AI 审查** | **Claude Opus 多模态**：页图 vs 渲染 md 对照，标 OCR错/公式错/漏内容/顺序错 | 取代人工抽检（所有者明确：抽检只能靠强 AI，不靠人肉）。批量、机器过一遍标疑点 |
| **人工复核** | **`debug_view.py` textbooks 版 HTML**（移植改造 patents 版） | 人可视化抽查、复核 Tier1 标出的疑难页、下最终判断 |

### 8.1 debug_view textbooks 版（移植改造）

复用 [patents debug_view](../../../scripts/pipelines/patents/debug_view.py) 的自包含单 HTML 骨架
（左原图 / 右产物 / 翻页 / 缩略图条 / 缩放平移 / 人工标记导出），改三处适配：

| 部件 | patents 版 | textbooks 版 |
|---|---|---|
| 左叠加层 | 剔除行号/页眉/gutter/段落框 | `parsing_res_list` 块：按 `block_label`（text/formula/formula_number/table/figure/title）着色 + 标 `block_order` 阅读顺序号 |
| 右侧内容 | 段落卡片 + 剔除词 | **该页 md 的渲染视图（公式经 KaTeX/MathJax 渲染）** —— 左看扫描原页、右看排版好的公式与正文，逐页对照 |
| 标记语义 | 误删/漏删/转换错/漏识别 | OCR错 / 公式错 / 漏内容 / 顺序错 / 着重号问题 |

Tier1（Opus 自动审）与人工复核 HTML 互补：AI 批量标疑点 → 人在 HTML 里翻检疑难页下终判。

## 9. Backlog（本设计登记、不在首版实现）

- **vLLM/SGLang 加速**（WSL2 或 Docker）：解决 48–78s/页 量产瓶颈，量产前搭。
- 分辨率/显存池 fraction 调优。
- 第二 OCR 引擎交叉校验（MinerU 2.5 / PP-OCRv5）：作为高风险页可选增强，非默认。

## 10. 边界与红线

- **不改 patents/general**：textbooks 独立建、零耦合。
- **确定性优先**：ML/OCR 只做识别判断，重组走确定性逻辑；产物可审计、错误可定位。
- **对外操作/装大依赖前确认**：vLLM 等重依赖安装、批量执行，均需所有者点头。
- 设计阶段不执行：本文档只定架构与思路。

## 11. 下一步

按 `superpowers:brainstorming` 收尾 → 所有者 review 本设计 → `superpowers:writing-plans` 出分步实现计划
（首版目标：单文档 PDF→PNG→predict→reconstruct→Typora 跑通 + Tier0 lint，暂不含 Opus 审查/HTML/分块的完整实现，
按计划分步落地）。
