# HANDOFF —— 教科书/扫描书转换引擎（book 引擎）开发，转到家用机接手

> 撰写：2026-07-01，于"公司机"。所有者接下来要换到"家用机"继续开发，本文档**例外地进 git 提交并 push**
> （本项目常规交接文档走私有 OneDrive `04_Docs/plans/`，这次因为要跨机器且家用机可能没配那个 OneDrive
> 账号，所有者明确要求这份走 git）。读完即可在家用机 `git clone` / `git pull` 之后接着干。

---

## §0 TL;DR

- **目标**：仓库里现在只有两条管线——`patents`（美国专利专用几何规则引擎，已完成不动）和 `general`
  （Marker + ML 版面，给普通 born-digital 文档用，代码建成但**依赖未装、无冒烟测试、不支持大文件切分**）。
  **两者都不适合"大部头扫描教科书"**。所有者决定：接下来的开发重心是**从零建一条新管线**，专门处理这类内容，
  仓库自己的 `TODO.md` 里已经把它预留了名字：`scripts/pipelines/textbooks/`。
- **为什么现在做这个、而不是先跑 general 引擎批量转换**：今天的调研发现，真正的"大头"内容
  （`D:\无线充电\参考文献\Project 博士论文\1 Books`，131 个 PDF / 5.8GB）里 **63%（82 本，约 3.6 万页）
  是纯扫描件、没有文本层**，包括 Pozar《微波工程》中英文版、《高等数学》、Khalil《Nonlinear Systems》等。
  这部分内容不装文本层就没法用 `general` 引擎处理，是当前唯一的硬瓶颈。
- **这台机器（公司机）今天做完的事**：写了一份《文献批量转换编排方案设计》
  （`docs/superpowers/specs/2026-07-01-literature-batch-conversion-design.md`），覆盖 Zotero + 三处外部文件夹
  的资料收集、分类规范、文本层实测结果。**该文档里的"收集分类"工作本身跟本次 book 引擎开发解耦**——
  book 引擎不需要先等那份工作做完，两条线可以并行；但那份文档里的实测数据（尤其 §3 的文本层统计）
  是本次 book 引擎立项的直接依据，值得先读一遍建立背景。
- **不要做的事**（继承自本项目已有红线，详见 §4）：不要为了图省事直接吃 MinerU/Marker/Docling 的端到端
  markdown 输出；不要改动 `patents` 管线；不要在没有样本实测支撑的情况下臆断引擎选型或算法参数。

---

## §1 背景：为什么是"book 引擎"，不是先装 general 跑批量

### 1.1 今天的实测链路

1. 用户最初的诉求是"把 Zotero 里剩下的文献（AIMD_MRI_Research、Deep_Research 集合）+ 系统各处散落的书籍/
   标准都转成 md"，让我先敲定转换策略。
2. 探索仓库发现两条现成管线（见 §0），`general` 是"新引擎"（Marker），但**依赖没装**
   （`marker-pdf`、`torch==2.11.0+cu128`、`surya-ocr` 等在 `.venv` 里都没有），且仓库里**没有任何冒烟测试/
   样例夹具**，只在 TODO.md 里记了"用 4 份 Apple HIG 文档验证过"。
3. 读 `E:\Zotero Data\zotero.sqlite`（只读拷贝探测，方法见下方 §1.2）拿到真实集合结构和条目统计；
   扫了三处外部文件夹拿到文件数/体积；用 PyMuPDF 对 **253 个候选 PDF** 逐个采样 5 页判断是否有可提取文本层。
4. 结果：Zotero 里的论文/报告（82 个）**100% 有文本层**；但外部 Books 文件夹（真正的体量大头）
   **63%（82 本）是纯扫描件**。完整数据见 `docs/superpowers/specs/2026-07-01-literature-batch-conversion-design.md` §3。
5. 所有者据此判断：与其先装一个还没验证过的 `general` 引擎去处理占比例较小的"有文本层"内容，
   不如直接啃这块硬骨头——**教科书/扫描书引擎**，因为它是唯一在仓库现有能力矩阵里完全空白、
   又是内容体量真正大头的部分。同时对 `general` 引擎本身是否成熟也持保留态度（原话："`general` 引擎也是没有开发完善的"）。

### 1.2 Zotero 库探测方法（如果家用机也要接同一个 Zotero 库）

Zotero 正在运行时其真实数据目录不一定是默认路径，要从 profile 的 `prefs.js` 里读：

```powershell
# 找 Zotero profile 里的 prefs.js（通常在 %APPDATA%\Zotero\Zotero\Profiles\<hash>.default\prefs.js）
Select-String -Path "$env:APPDATA\Zotero\Zotero\Profiles\*\prefs.js" -Pattern "extensions.zotero.dataDir"
```

拿到 `dataDir` 后，真实库文件是 `<dataDir>\zotero.sqlite`。**这个文件建议先拷贝一份到临时目录再读**
（Zotero 跑着的时候可能有 `.sqlite-journal`，直接读原文件有极小概率读到写入中的中间态；拷贝后读没这个风险，
patents/PDF 附件在 `<dataDir>\storage\<itemKey>\<filename>`）。今天用的探测脚本逻辑（集合树遍历、按
collectionID 递归找子集合、附件路径解析）保留在会话记录里，家用机需要时可以照着重写，不复杂（~80 行 Python +
`sqlite3` 标准库，无额外依赖）。

**注意**：家用机上 Zotero 的 `dataDir`、盘符、甚至登录的 OneDrive 账号大概率跟公司机不一样，
不要硬编码 `E:\Zotero Data`，一律走上面"读 prefs.js"的方式动态探测。

---

## §2 现有能力矩阵（今天调研确认，供设计新管线时参考）

| 管线 | 方法 | 适用范围 | 关键限制 |
|---|---|---|---|
| `scripts/pipelines/patents/` | PyMuPDF `get_text("words")` + 手写几何规则 | 美国专利（双栏+中央行号+`Sheet N of M` 页眉信号） | 法域/文档类一换信号就失效，**明确不做迁移**（F1 0.97、确定性可审计是其优势，见 TODO 2026-06-12） |
| `scripts/pipelines/general/` | Marker（ML 版面识别，从文本层取字）+ Typora 排版 | 有文本层的普通 born-digital 文档 | **不分块**（README 原文），**依赖未装**，**无冒烟测试**，**无 resume** |
| `scripts/pipelines/textbooks/`（**待建，本次任务**） | 待定，TODO.md 里候选 **MinerU**（Apache 2.0，非 AGPL，本地可跑，CPU≥16G 或 GPU，输出 md + 阅读顺序 JSON 含 bbox） | 教科书/扫描书（无中央行号、可能无文本层、常有 CJK） | 从零开发 |

`patents` 页型判定（COVER/FRONT_MATTER/FIGURE/SPEC_BODY）完全靠美国专利专属规则（`Sheet N of M` 页眉、
中央行号阶梯、词数阈值），这套规则对教科书**完全不适用**——教科书没有这些版面信号。TODO.md 2026-06-12 条目
已经明确了方向：**新管线要用 ML 版面分析模型做"结构判断"（页型/栏/阅读顺序），输出 bbox+阅读顺序 JSON**，
不是靠手写规则。

---

## §3 建议的开发起点（未拍板，供参考，具体设计由家用机 session 展开）

以下不是最终方案，是今天调研过程中收集到的、值得带到设计阶段的线索：

1. **MinerU 是仓库自己路线图里的首选候选**（TODO.md 2026-06-09 & 2026-06-12 两次提到）：
   Apache 2.0 系许可（v3.1.0 起，非 AGPL）、本地可跑、Python SDK + 本地 REST API、输出 md + 阅读顺序 JSON（含
   bbox）。教科书没有中央行号，正是它的主场。**但选型本身没有实测验证过，属于本次要做的第一步工作**。
2. **本项目已有的架构哲学**（从 `patents` 管线的成功经验 + 2026-06-11 的 OCR 夹层交接文档提炼）：
   - 倾向"确定性产物 + Tier0 可审计底座"，ML/OCR 模型负责"判断"（页型、阅读顺序、bbox），
     **不负责直接吃进去吐 markdown 就完事**——2026-06-11 那份交接文档里明确写过"不用 Marker/Docling/MinerU
     的端到端 markdown 输出，会绕过我们管线的结构重建、丢可审计性"，这条红线是针对**专利 OCR 场景**定的，
     但精神上值得在教科书管线设计时重新审视：**新管线到底要不要照搬这条红线，还是教科书场景下"结构信任 ML 输出"
     更划算，是本次要做的判断题，不是照抄结论**。
   - 每条管线都配一份 Tier0 自检（字符覆盖率、结构 lint）+ README + `requirements.txt`，CLI 遵循
     `--src`（吃文件/目录/多个）/`--out`（默认就地）的统一约定（AGENTS H.5）。
3. **大文件切分是绕不开的问题**：外部 Books 文件夹里最长的扫描书有 700-800 页，任何引擎啃这种体量
   大概率要分块处理（内存、耗时、GPU 显存都可能扛不住整本一次性跑）。分块粒度、跨块拼接（页码续接、
   跨块段落续写判定——`patents` 管线的 `reading_order._assemble_paragraphs` 里已经有一套"跨栏+跨页续接"
   的三信号判据可以参考思路）留给新管线设计。
4. **样本素材**（公司机路径，家用机开发不依赖它，仅供后续在公司机验证时用）：外部 Books 文件夹里已经有
   "OCR" 子文件夹（如 `1 Books\WPT\OCR\`、`1 Books\控制理论\OCR\`），说明所有者之前已经手动/半自动给部分
   扫描书加过文本层，这些"已加层"样本可以作为对照组，评估新引擎产出质量时用。

---

## §4 必须遵守的红线（继承自本项目既有约定）

- **不改 `patents` 管线**——它是确定性规则引擎，F1 0.97，是仓库的"金标准"，教科书管线独立建，零耦合。
- **`general` 引擎的依赖安装、冒烟测试、批量转换执行**——这部分维持"待所有者另行确认"的状态，
  今天的会话里所有者明确说了"暂时不安装"，book 引擎开发不应该顺带把这个也装了。
- **确定性优先、ML 只做判断不擅自改字符**这条本项目一贯的原则（`patents` 管线的核心哲学），
  在教科书管线设计阶段要重新审视是否适用、以及适用到什么程度，不要跳过这个讨论直接抄 MinerU 端到端输出。
- **对外操作（push、装大依赖）前跟所有者确认**——尤其是 MinerU/深度学习依赖体积可能不小，装之前跟今天
  `general` 引擎依赖一样，先确认。
- **技术债 / 需详述的延后事项按本项目惯例登记**：常规是记 `TODO.md`（私有，OneDrive 同步，`## YYYY-MM-DD`
  倒序分节）；如果家用机没配那个 OneDrive 账号看不到 `TODO.md`，进度可以先记在这份 handoff 或新开一份
  `docs/handoff/` 下的文档里，等能同步了再合并回 `TODO.md`。

---

## §5 关键指针

- 本次触发这个决策的调研数据 & 分类设计：`docs/superpowers/specs/2026-07-01-literature-batch-conversion-design.md`
  （§3 文本层实测表格、§4 分类规范、§7 backlog 条目"纯扫描大部头教科书"）
- 现有 `general` 管线代码（架构参考，不是要复用其 Marker 依赖）：`scripts/pipelines/general/`
  （`batch.py`、`README.md`、`requirements.txt`）
- 现有 `patents` 管线（架构参考，Tier0/README/CLI 约定的范本）：`scripts/pipelines/patents/`
- `TODO.md`（私有，需登录 TODO.md 里记录的那个 OneDrive 账号同步后可见——账号信息在私有文档里，不在此公开文档中列出）：
  - `## 2026-06-12` "架构方向" 条——ML 版面判定 vs 规则判页的取舍
  - `## 2026-06-09` "MinerU 作 CJK/教科书管线备选引擎" 条
  - 文末"工作文档双机同步"章节——如果家用机要接 `06_Docs`（TODO + 私有交接文档）的 OneDrive junction，
    照着那段 PowerShell 脚本走一遍即可（账号信息见 `TODO.md` 原文，此处不重复）
- 2026-06-11 的 OCR 夹层 handoff（私有，`04_Docs/plans/2026-06-11-HANDOFF-ocr-sandwich-tesseract.md`）——
  "ML 只判断不改字符"这条哲学的完整论证过程，教科书管线设计时值得重读一遍再决定是否照搬。

---

## §6 下一步（家用机 session 建议顺序）

1. 读完本文档 + `docs/superpowers/specs/2026-07-01-literature-batch-conversion-design.md`。
2. 如果能同步到 OneDrive 私有 `TODO.md`，把上面 §5 提到的几条历史决策通读一遍，尤其是"ML 只判断不改字符"
   这条哲学在 `patents` 管线里的具体实现方式（`reading_order.py`），作为教科书管线设计的参照系。
3. 找 2-3 本有代表性的扫描教科书样本（可以用今天探测出的"scan_or_empty"清单里的任意几本，或自己现找），
   小规模验证 MinerU（或其他候选引擎）的真实输出质量、速度、资源占用——**先拿数据，再谈架构**。
4. 基于验证结果，产出教科书管线的设计文档（可以照着 `docs/superpowers/specs/` 这个目录的风格另开一份），
   核心要回答：MinerU 输出直接采用到什么程度 vs 自己再加一层确定性重建；分块策略；Tier0 自检怎么定义
   （教科书没有专利那种"claims 必须完整"的硬指标，覆盖率阈值可能要重新定）。
5. 完成后按本项目惯例：每个子任务独立 commit，push 前跟所有者确认。
