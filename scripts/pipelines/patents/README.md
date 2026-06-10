# 专利 PDF → Markdown 转换（patents/）

针对**美国授权专利**（born-digital、双栏排版、中央行号）的确定性几何转换管线。
内容准确优先：主转换零成本、确定性、无幻觉；AI 仅做只读审查。

## 为什么不用现成方案

Docling/Marker 对专利双栏版式会把**左右栏按行横向拼接**、**把行号当正文**、
**过度拆分标点**（"and / or"、"2 , 433"）。本管线用 PyMuPDF 文字层坐标，按
几何规律精确重排，已在 5 份 staging 专利上做到 Tier0 内容覆盖 100%、零结构问题。

## 架构

```
PDF → page_classify(封面/前置/附图/正文) →
   封面  → bib_parse  → YAML 元数据 + 摘要
   正文  → reading_order(分栏+剔行号+剔页眉+空格重建) → 分节 + claims 切分
   附图  → figures(整页渲染 PNG + FIG 标题)
   前置  → reading_order.reconstruct_linear(线性) → 引用/分类附录
→ 组装(YAML+分节+claims列表+附图) → selfcheck(Tier0) → [ai_review(Tier1,可选)]
```

**核心解耦**：`reading_order.py` 只吃"带坐标的词框"，与取词来源无关。第二期接
OCR（中/欧/日扫描件）只需换取词器 + 加国家 profile，引擎复用。

## 文件

| 文件 | 职责 |
|---|---|
| `profiles.py` | 布局常量（gutter/行号带/页眉正则/章节关键词）。`US_GRANT`；预留 CN/EP |
| `reading_order.py` | 核心引擎：分栏、剔行号、剔页眉页脚、几何+标点空格重建、段落 |
| `page_classify.py` | 页型分类 + PyMuPDF 取词（第二期可替换为 OCR） |
| `bib_parse.py` | 封面 INID → 专利号/标题/发明人/受让人/日期 + 摘要 |
| `claims.py` | 按递增权项号切分（对行内编号鲁棒）→ 带从属关系的列表 |
| `figures.py` | 附图页整页渲染 + 抓 "FIG. N" |
| `convert_patent.py` | 单篇编排 → 结构化 Markdown |
| `selfcheck.py` | Tier0：字符级覆盖校验 + 结构 lint（零成本） |
| `crosscheck_words.py` | 交叉校验（离线，不进主管线）：pymupdf4llm `extract_words` 第二取词器全词集审计，缺失词独立归因（行号/页眉/claims 标记），堵 Tier0"已删除词"盲区 |
| `debug_view.py` | 可视化调试（自包含单 HTML，默认输出到本目录、已 gitignore）：左=页面图+判定叠加层（行号/页眉/保留词/段落/crosscheck 告警，逐层显隐+缩放），右=该页重排中间产物；双向联动（hover/点击互相定位）；标记模式（误删/漏删/转换错 → 导出 `*_annotations.json` 供修引擎）；暗色默认（claude-dark）可切换；←/→ 翻页 |
| `ai_review.py` | Tier1：云端 AI 仅出报告（模型可切换），不改写 |
| `batch_patents.py` | 批驱动 |

## 用法

```powershell
# 列出 / 转换 staging 全部专利
.venv\Scripts\python.exe scripts\pipelines\patents\batch_patents.py --list
.venv\Scripts\python.exe scripts\pipelines\patents\batch_patents.py
.venv\Scripts\python.exe scripts\pipelines\patents\batch_patents.py --resume

# 单篇
.venv\Scripts\python.exe -c "from pathlib import Path; import sys; sys.path.insert(0,'scripts/pipelines/patents'); from convert_patent import convert; convert(Path('<pdf>'), Path('03_Output/patents/<name>'))"

# 交叉校验（转换后离线审计;第二取词器 vs 产物,误删告警带坐标）
.venv\Scripts\python.exe scripts\pipelines\patents\crosscheck_words.py
.venv\Scripts\python.exe scripts\pipelines\patents\crosscheck_words.py --src <pdf|dir> --md-root 03_Output\patents

# 可视化调试视图（生成 <stem>_debug.html 到脚本同目录,VS Code 预览/浏览器打开;
# 先跑 crosscheck 可叠加红色告警层;标记导出 <stem>_annotations.json 交 agent 修引擎）
.venv\Scripts\python.exe scripts\pipelines\patents\debug_view.py

# Tier1 云端 AI 审查（仅出报告；模型实测后定）
$env:ANTHROPIC_API_KEY="..."   # 或 OPENAI_API_KEY / GEMINI_API_KEY
.venv\Scripts\python.exe scripts\pipelines\patents\batch_patents.py --review --review-model anthropic:claude-haiku-4-5
.venv\Scripts\python.exe scripts\pipelines\patents\ai_review.py <pdf> <out_dir> --model gemini:gemini-2.5-flash --all
```

输出：`03_Output/patents/{name}/{name}.md` + `{name}_artifacts/`（附图 PNG）
+ `{name}_selfcheck.json`（Tier0 报告）+ `{name}_review.{md,json}`（如跑了 Tier1）。

## 输入假设与边界

- **仅美国授权专利**（带文字层）。无文字层的扫描件需第二期 OCR。
- 非标准 claims 标记（如部分 PCT 国家阶段件）可能不单独分节，但**内容不丢**
  （Tier0 覆盖校验保证），会折叠进说明书并提示。
- OCR 文字层固有的偶发碎词（"elec trode"、"implant able"）属源缺陷，未强行合并。

## 第二期（OCR / 多国）规划

1. `page_classify` 前加"文字层探测"，无字层 → 路由 OCR（PaddleOCR/PP-StructureV3）
   → 输出词框喂同一 `reading_order`。
2. 新增 `CN`/`EP`/`JP` profile（页面几何 + 页眉正则 + 章节关键词）。
3. `ai_review` 复用，中文页换中文强模型。
