# textbooks 调试可视化工具 + LaTeX 渲染异常排查 —— 设计

> 撰写:2026-07-03(自主执行会话)。承前:交接 `docs/handoff/2026-07-03-HANDOFF-textbooks-latex-render-qa.md`。
> 所有者已拍板:**完整版工具(含标注模式)**;标注用 **5 类渲染/转换质量**分类;自动 KaTeX 扫描**同时导出 `<stem>_render_errors.json`**。
> 会话中途所有者授权自主执行("自行判断分叉点,明早看成果"),故本 spec 由执行者定稿,不再逐节等审批。

## §0 目标与背景

所有者在 VS Code Markdown 预览里看 `Paul_p1-100_scan.md`,发现 1.3a 公式整段红色(KaTeX 硬报错)。根因已查清(交接 §2):PaddleOCR-VL 把"underbrace 下方多行小字标注"错拆成同一节点上相邻双下标 `_{A}_{B}`,KaTeX 报 double subscript。

两个诉求:
1. **debug 可视化工具**:仿 patents `debug_view.py` 的左右并排 HTML,右栏**实时渲染** markdown+KaTeX,使"红色报错"在视图里直接复现,供人工逐页核对。
2. **排查**:查清红的原因(已知)+ 全书还有没有别的渲染异常。

前置修复(已完成,commit 2772a98):`sanitize_latex` 新增 `_collapse_double_subscript`,括号配对扫描合并相邻双下标 `_{A}_{B}`→`_{A\ B}`,确定性消红、零丢失、不猜断行。本工具在此基础上做验证与扩大排查。

## §1 两条相互独立又互补的能力

| 能力 | 载体 | 无人值守可产? | 解决 |
|---|---|---|---|
| **确定性硬报错扫描** | node + KaTeX(headless) | ✅ 是 | §0.2 "还有没有别的红"——硬报错 100% 命中 |
| **视觉 before/after 核对** | 自包含 HTML(浏览器/VS Code 预览) | 需人看 | §0.1 工具 + 软问题(1.3b 型能渲染但排版丑)人工核对 |

硬报错(1.3a 型)不该靠肉眼盲翻 100 页——KaTeX `throwOnError` 能确定性列全。肉眼只处理软问题(渲染成功但排版/归类不对)。二者共用同一套 md 数据源与同一份 vendored KaTeX,保证"扫描判红"和"视图显红"一致。

## §2 数据源与坐标(关键约束)

- **块数据**:读预落盘的 `_work/page_NNNN_res.json` 的 `parsing_res_list`(引擎输出已冻结,无 GPU 无法重算;reconstruct 对它是确定性的)。**不**像 patents 那样现场重跑引擎——textbooks 的"引擎判定"就是 res.json,不可重算。
- **每块字段**:`block_label` / `block_content` / `block_bbox`([x0,y0,x1,y1]) / `block_id` / `block_order` / `group_id`。顶层含 `width`/`height`(DPI 下页尺寸)。
- **坐标空间**:`block_bbox` 就在 manifest DPI(150)的 PNG 像素空间(images.py 已按此裁图),左栏同 DPI 栅格化即可**直接对齐**叠框,零换算。
- **页图来源**:源 PDF 现场 `pdf_page_to_png()` 栅格化。PDF 路径取自 manifest `pdf_path`,允许 `--src` 覆盖;**缺失时优雅降级**(左栏显"源 PDF 不可用"占位,右栏仍可用)。⚠️ 本语料 manifest 的 pdf_path 指向上一会话 scratchpad 临时文件(现存但脆弱)——生成的**静态 HTML 把页图 base64 内嵌后自包含**,不再依赖 PDF,规避该脆弱性。
- **右栏 md**:逐页 `reconstruct_markdown(blocks, stem, page)` 实时产出(过修复后的 sanitize_latex),**不是**读整份 md 文件——这样右栏所见即当前代码所判。

## §3 组件划分(小而有界,可独立测试)

```
scripts/pipelines/textbooks/
  debug_payload.py     # 纯函数:逐页 payload(页图 base64 / bbox 叠框 / 逐页 md / selfcheck 信号)。可脱离浏览器单测。
  debug_view.py        # CLI:编排 + 静态落 HTML + --serve 服务模式。薄。
  check_annotations.py # 从导出的标注 json 生成回归断言,配合 5 类分类。
  debug_assets/
    template.html      # 应用骨架(左右栏布局 + JS 交互 + 标注模式)。
    app.js / app.css   # 交互逻辑与样式(内联进最终 HTML)。
    vendor/            # 离线内嵌:katex.min.js/css + fonts + auto-render + markdown-it。
tools/scan_katex_errors.mjs  # node headless:对 md 抽取每个公式跑 KaTeX,落 render_errors.json。
```

**边界**:`debug_payload.build_page_payload(res_json, pdf_path, dpi)` → dict(纯数据,JSON 可序列化);`debug_view` 只负责把 payload 列表塞进 template 并处理 serve/静态;渲染与判红全在浏览器 JS(KaTeX);headless 扫描器复用同版本 KaTeX 独立成一个 node 脚本。

## §4 渲染与自动扫描

- **markdown 渲染器**:markdown-it(VS Code 预览同源,最忠实复现所有者实际环境)。
- **数学**:KaTeX + auto-render,`throwOnError: true`——渲染失败时 KaTeX 输出红色错误节点(`.katex-error`),**直接复现所有者看到的红**。
- **浏览器内扫描**:页面加载后 JS 遍历所有 `.katex-error`,聚合成"报错页跳转索引"面板;serve 模式可一键 POST 回写 `<stem>_render_errors.json`,静态模式走浏览器下载。
- **headless 扫描(无人值守产出)**:`tools/scan_katex_errors.mjs` 用正则从 md 抽取 `$$...$$` / `$...$`,逐个 `katex.renderToString(..., {throwOnError:true, displayMode})`,捕获抛错者,落 `render_errors.json`(结构:`[{page?, formula_index, mode, latex, error_message, latex_head}]`)。这是本会话夜间产出的**确定性发现清单**,不依赖人点浏览器。
- **一致性**:两处用同一 vendored KaTeX 版本,判红结果一致。

## §5 视图形态

- **左栏**:该页 PNG(base64) + 各 block `block_bbox` 叠框,按 `block_label` 分色(text/display_formula/formula_number/image/chart/paragraph_title/... 各一色;order=None 的噪声块用虚线弱化)。hover 块高亮,点击滚动右栏对应片段。
- **右栏**:逐页 reconstruct 的 md,markdown-it+KaTeX **渲染后**显示;报错公式红色高亮。顶部每页显示 selfcheck 信号徽章(该页命中的 unhandled_labels / visual_warnings / column_layout_suspected)。
- **顶部**:页码翻页 + "报错索引"面板(跳到有 KaTeX 错误的页) + 标注模式开关(M)。
- **标注模式**:M 键进入,在左栏页图上框选并归类(§6 五类),导出 json;serve 直写,静态下载。

## §6 标注五类(渲染/转换质量)与回归契约

| # | 分类 | 含义 | 回归断言(check_annotations) |
|---|---|---|---|
| 1 | 渲染报错(红) | KaTeX 硬报错,如 1.3a | 该页 reconstruct md 经 headless KaTeX 扫描**不得再报错**(修复后应清零) |
| 2 | 公式排版错 | 能渲染但不对,如 1.3b(裸文字斜体/堆叠乱) | 断言标注 latex 片段经归一化后含预期修正(逐例定,较弱) |
| 3 | 漏内容/漏识别 | 页上有、md 里没有 | 断言该内容子串出现在该页 md(block_coverage 同源) |
| 4 | 错误归类 | block_label 判错(公式当 text / 标题层级错) | 断言该 block_id 的 label 为人工标注的期望值 |
| 5 | 图片位置/裁切错 | 图插错位置或裁切范围错 | 断言图片链接相对正文的插入顺序(golden 同源) |

标注 json 结构:`{stem, page, block_id?, bbox, category(1-5), note}`。`check_annotations.py` 逐条按上表转成 pytest 断言,回归时锁死"这些人工确认过的问题不复发"。v1 先落 1/3/4 的可自动断言部分(有确定性判据),2/5 记录为人工核对项(note 保留,断言留 TODO)。

## §7 CLI(仿 patents)

```
python -m scripts.pipelines.textbooks.debug_view --doc <stem-dir>            # 静态落 <stem>_debug.html(gitignore)
python -m scripts.pipelines.textbooks.debug_view --doc <stem-dir> --serve    # 服务模式 http://127.0.0.1:8078/
python -m scripts.pipelines.textbooks.debug_view --doc <stem-dir> --collect  # 归位浏览器导出的标注
  可选:--src <pdf 覆盖>  --dpi  --port
tools/scan_katex_errors.mjs <md-path> [--out render_errors.json]             # headless 扫描
```

端口用 8078(patents 占 8077,避冲突)。

## §8 错误处理与边界

- 源 PDF 缺失:左栏占位,不崩;静态 HTML 已内嵌 base64 故不受影响。
- 畸形/缺失 bbox:不叠框(与 reconstruct 同策略,已有降级),不崩。
- res.json 缺页/空白页:该页显"空白/缺失",跳过。
- 大页数:静态 HTML 内嵌 100 页 base64 图偏大(~数十 MB);可接受(patents 同量级)。必要时 `--pages a-b` 分段(v1 可选)。
- vendored 库:一次性 `npm install` 取 dist(katex+markdown-it),copy 进 `debug_assets/vendor/`;运行期零外部请求。

## §9 测试策略

- `debug_payload`:TDD 纯函数——给定 res.json 与桩 PDF,断言 payload 含正确页图 key、bbox 叠框数与坐标、逐页 md、selfcheck 徽章。用真实 page_0031(1.3a)res.json 做 golden。
- `scan_katex_errors.mjs`:对已知 1.3a(修复前)断言命中 double subscript;对修复后断言清零;对 1.3b 断言**不**报硬错(软问题不误报)。
- `check_annotations`:对构造的标注 json 断言生成的断言按 §6 表正确通过/失败。
- 不给浏览器 JS 交互写自动化测试(v1);靠人工 + headless 扫描双保险。

## §10 交付物(本会话夜间)

1. sanitize 修复(已提交)。
2. `render_errors.json`(修复后)+ before/after 对比 → 回答"红修好没 + 还有别的红没有"。
3. 重生成的干净 `Paul_p1-100_scan.md`(旧版备份留证)。
4. 可视化工具(payload/view/assets/check_annotations)+ 生成的静态 `Paul_p1-100_scan_debug.html`。
5. 本 spec + plan + handoff + TODO 更新。
