# 文献批量转换编排方案设计

- 日期：2026-07-01
- 状态：草案（待用户确认）
- 范围：Zotero 库 + 系统各处散落书籍/标准 → md 转换的收集、分类、编排策略
- 不涉及：patents 管线本身的修改（已完成、不动）；general 引擎的具体代码改造（另立任务）

> **⚠️ 环境说明**：本文档中出现的所有绝对路径（`D:\无线充电\...`、`G:\Projects\Project_MRI_Safety\...`、
> `E:\Zotero Data\...` 等）均为**当前这台"公司机"上的路径**，是这次会话实测调研时的实际取数位置。
> 另一台"家用机"上的盘符、目录结构、Zotero 数据目录（甚至 OneDrive 账号）大概率不同，**不能直接照抄这些路径**。
> 引擎开发本身（尤其是本文档触发的教科书/扫描书引擎）不依赖这些路径能否访问——用任意样本扫描 PDF 就能开发验证；
> 真正对着这些路径跑全量收集/转换，需回到本机（或在家用机上重新探测出对应路径）执行。
> 涉及 Zotero 时优先复用"从 `prefs.js` 读 `extensions.zotero.dataDir`"的探测方式（本文档 §3 的调研方法），
> 不要硬编码 `E:\Zotero Data`。

## 1. 背景

`patents` 管线已经把 Zotero 库里的专利文献转成了 md。现在要把剩下的内容也转成 md，来源分散：

1. Zotero 库中 `AIMD_MRI_Research` 集合（除已完成的 `Patents` 子集合外）
2. Zotero 库中 `Deep_Research` 集合
3. 系统各处散落的书籍：
   - `D:\无线充电\参考文献\Project 博士论文\1 Books`
   - `D:\无线充电\参考文献\EMC`
4. 标准文献：`G:\Projects\Project_MRI_Safety\01_Knowledge\Sources\Standards\_PDF`

仓库里已有两条转换管线：

| 管线 | 方法 | 适用 | 现状 |
|---|---|---|---|
| `scripts/pipelines/patents/` | 确定性几何解析（PyMuPDF words + 版面规则） | 美国专利 | 已建成并跑过 |
| `scripts/pipelines/general/` | Marker（ML 版面识别）+ Typora 排版 | 有文本层的普通 born-digital 文档 | 代码已建成，**依赖未安装**、**无冒烟测试**、**无 resume**、**不做大文件切分** |

`general` 引擎目前只用 4 份 Apple HIG 文档验证过，成熟度不明确；本方案因此把"收集整理"和"引擎转换执行"拆成两个独立、解耦的阶段，前者现在就能做，后者留到引擎验证/加固之后。

## 2. 范围分期

- **本阶段（现在做）**：把四处来源的文献扫描收集、探测、去重、按分类规范落地成一份可恢复的清单（manifest），并生成对应的分类目录骨架。**不跑任何 PDF→MD 转换**。
- **下一阶段（引擎验证后）**：装 `general` 引擎依赖，跑冒烟测试，通过后才对 manifest 中标记为"可转换"的条目批量执行。
- **暂缓（backlog）**：纯扫描/无文本层的大部头书籍——需要仓库自己路线图里还没开工的教科书管线（候选 MinerU），本方案只负责把它们识别出来、登记、不处理。

## 3. 资料来源实测结果

已用只读方式探测（复制 `E:\Zotero Data\zotero.sqlite` 到暂存区读取，PyMuPDF 采样每份 PDF 的首/中/尾等 5 页判断是否有可提取文本）：

| 来源 | 文献条目数 | 有 PDF 附件 | 有文本层(born-digital) | 纯扫描/无文本层 |
|---|---|---|---|---|
| AIMD_MRI_Research（除 Patents） | 41 | — | — | — |
| Deep_Research | 66 | — | — | — |
| **Zotero 合计（去重后目标条目）** | **107** | 81 有 PDF / 26 无 PDF | **82/82 = 100%** | 0 |
| 外部 Books 文件夹 | 131 个 PDF / 5.8GB | — | 49 (37%)，约 2.3 万页 | **82 (63%)，约 3.6 万页** |
| EMC 文件夹 | 34 个 PDF / 1.3GB | — | 24 (71%) | 10 (29%) |
| Standards 文件夹 | 6 个 PDF / 29MB | — | 6 (100%) | 0 |

关键结论：

- Zotero 里的论文/报告 **100% 有文本层**，是 `general` 引擎最合适、风险最低的验证对象。
- 外部 Books 文件夹是真正的"大头"（5.8GB），但其中 **63% 是纯扫描件**（含《微波工程》Pozar 中英文版、《高等数学》、Khalil《Nonlinear Systems》等大部头），这部分不是装个引擎就能扛的，需要教科书管线，本方案先标记为 backlog。
- Zotero 中有 26 个条目没有 PDF 附件（多为网页快照/新闻链接），本方案登记但不处理。

探测明细已保存：`_convert_workspace/00_manifest/textlayer_probe_results.json`（首次运行编排脚本时会重新生成，当前副本在会话暂存区）。

## 4. 分类规范（沿用 AIMD_MRI_Research 的分类风格）

原则：**能复用 Zotero 已有的集合/子集合结构就直接复用**，只有外部来源（无 Zotero 归属）才新增顶层分类。不强行把书籍塞进 Vendor/Regulatory 这种明显不合适的类目里，但保持"顶层=文献角色分类，二级=主题"这个统一模式。

顶层分类：

- `Patents`（已完成，本次不涉及）
- `Vendor` / `Papers` / `Regulatory` / `News` —— 直接对应 AIMD_MRI_Research 现有子集合，条目按其实际所属子集合分流
- `Deep_Research/<子集合名>` —— 保留 14-A / 14-B / 14-C / 13 / RF heating / 17-A / 17-微波工程 / 17-1-PUTL适用性验证 / 20-ML反向设计 原始子集合名
- `Books`（新增）—— 二级沿用文件夹已有主题划分：WPT / 光学 / 微波工程 / 控制理论 / 数学 / 朗道理论 / 电磁学 / 电路 / 耦合振子-动力学行为 / 费恩曼物理学讲义 / EMC（EMC 文件夹并入 Books 下的二级分类）；Zotero 里的 3 本 book 条目也落在 `Books/Deep_Research`
- `Standards`（新增）—— 二级按标准号前缀分流：ASTM / ISO / IEC / Other

输出目录骨架（在 `_convert_workspace/` 下）：

```
_convert_workspace/
  00_manifest/                 # 清单数据库、探测缓存、运行日志
  Vendor/<doc_id>/...
  Papers/<doc_id>/...
  Regulatory/<doc_id>/...
  News/<doc_id>/...
  Deep_Research/
    14-A/<doc_id>/...
    RF_heating/<doc_id>/...
    ...
  Books/
    WPT/<doc_id>/<doc_id>.md + <doc_id>_artifacts/
    数学/<doc_id>/...
    EMC/<doc_id>/...
    Deep_Research/<doc_id>/...   # Zotero 里的 3 本书
  Standards/
    ASTM/<doc_id>/...
    ISO/<doc_id>/...
  _deferred_scanned/            # 纯扫描大部头，只登记不转换
  _no_pdf/                      # Zotero 里没有 PDF 附件的条目，只登记
```

`doc_id` 命名规则：Zotero 来源用 `zotero_<itemKey>_<清洗后标题前缀>`；外部文件用清洗后的原文件名（去掉 Z-Library 等下载站后缀噪音）。

## 5. 转换策略

### 5.1 大文件切分

`general` 引擎当前不做分块（README 原文："不分块，区别于 books 的大型扫描教材流水线"）。对于页数较多、但**有文本层**的 born-digital 书（外部 Books 里 49 本，最长可能几百页），编排脚本会：

1. 先按原文件整份尝试（这是默认路径，成本最低）；
2. 若冒烟测试证明整份处理在大页数下会内存溢出/耗时过长，再启用页范围切分（如每 80-120 页一片，用 PyMuPDF 拆分出临时子 PDF，分别喂给 Marker，再按页码把各片 md 拼接、去重叠页脚注/页眉）。

**这一步的具体切分/拼接算法目前不预先写死**——先用冒烟测试摸清 `general` 引擎在整份大文件上的真实表现（内存、耗时、输出质量），再决定是否需要、以及怎么切。方案里先把"支持切分"作为编排脚本的一个可插拔的预处理步骤占位，具体实现留到验证阶段。

### 5.2 并行度

- **收集/分类/探测阶段**（读 Zotero DB、扫文件夹、PyMuPDF 探测文本层）是 I/O/CPU 密集型，可以安全地 3 路并行，互不影响。
- **转换执行阶段**用到 GPU（Marker + surya-ocr，torch cu128），你的显卡是 RTX 5060 / 8GB 显存。3 路并行跑 GPU 推理大概率会挤爆显存——具体并发数以冒烟测试实测的显存占用为准，默认先按 1 路跑，观察显存余量后再决定是否提到 2 路。不建议在没实测前就按"3 批并行"跑转换阶段。

### 5.3 断点续跑 / 增量新增

用一份持久化清单（`00_manifest/manifest.sqlite`，而不是内存态列表）记录每个文档的状态机：

```
discovered → probed(born_digital|scan_or_empty|error) → queued → converting → done|failed|deferred
```

- 编排脚本每次运行先重新扫描四处来源，按内容哈希（文件大小+mtime，或 sha1 前 1MB）比对已有清单，**新增文件**只追加新记录，不动已有状态；**已删除/移走的源文件**标记 `missing`，不删除已转换产物。
- 转换阶段严格按清单里 `queued` 状态取任务，处理完立即落盘更新状态，中断后重跑只会继续处理未完成的条目（等价于 patents 管线的 `--resume`，但这里做成默认行为而不是可选 flag）。

## 6. 冒烟测试计划（引擎验证阶段，装依赖之后再跑）

装依赖前的准备工作现在就能做——从清单里的"有文本层"条目里，按来源类型各选 1-2 个**页数最少**的样本，固定成一组冒烟测试样例（例如：1 篇 Zotero 期刊论文、1 份 Standards PDF、1 本 EMC 书里最短的一本），登记在 `00_manifest/smoke_test_set.json` 里。

验证阶段的执行步骤（需要你另行确认才会执行 `pip install`）：

1. 装 `scripts/pipelines/general/requirements.txt`（含 torch cu128、marker-pdf、surya-ocr，多 GB 下载）
2. 对固定样例跑 `general` 引擎，检查：能否正常出 md、图片资源是否完整、Tier0 selfcheck 是否通过、显存/耗时是否在可接受范围
3. 你人工抽查 md 质量（尤其是公式、表格、双栏排版）
4. 通过后才把 manifest 里其余"有文本层"条目（合计 161 个）批量放行

## 7. 未决事项（backlog，本方案不处理）

- 纯扫描大部头教科书（82 本，约 3.6 万页）：等教科书管线（MinerU 候选，仓库 TODO.md 已有规划）
- `general` 引擎大文件切分的具体实现：等冒烟测试摸清引擎真实表现后再定
- 26 个无 PDF 附件的 Zotero 条目：需要人工补充原文或从库中移除
- Standards `_PDF` 目录里的 `[ref]_..._preview_ITEH.pdf` 这类预览版文件是否算正式来源，需要你确认

## 8. 下一步

1. 你确认本方案（分类规则、目录骨架、分期策略）没问题
2. 我编写编排脚本：`collect`（扫描四处来源+探测文本层+写入 manifest）与 `status`（查看清单进度）两个子命令，先跑一遍 `collect`，产出 `_convert_workspace/` 目录骨架和清单，供你过目
3. 你决定何时执行 `pip install` 装引擎依赖，再进入冒烟测试
