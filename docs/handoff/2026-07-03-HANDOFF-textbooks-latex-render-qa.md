# HANDOFF —— textbooks debug 可视化工具 + LaTeX 渲染异常排查,交接给新会话执行

> 撰写:2026-07-03。走 git(跨会话/跨机可见)。承前:[图片输出模块③完成交接](2026-07-03-HANDOFF-textbooks-conversion-quality.md)(模块①②③均已完成、140/140 单测绿、100 页真实语料端到端验证通过,详见该文档及 `docs/superpowers/plans/2026-07-03-textbooks-image-output.md` 的 progress ledger)。
>
> 本文档是所有者在 VS Code 里人工预览模块③产物 `Paul_p1-100_scan.md` 时,发现一段公式整体红色显示(渲染报错)后临时追加的调研任务,**尚未开始实现,只完成了调研,交给新会话动手**。

## §0 任务(所有者原话转述)

1. 产出一个 debug 用的脚本/工具,类似 `patents` 管线已有的那种 html 视图,能方便地对照"识别前后"的情况(原始页面 vs 转换结果并排,人工核对用)。
2. 用这个工具(或其他手段)查清:VS Code Markdown 预览里那段红色公式是什么原因导致的;当前转换出的 md 文档里,还有没有别的类似"红色/渲染异常"的内容。

## §1 触发现象

所有者截图:VS Code 内置 Markdown 预览(应该是走 KaTeX 或 MathJax 渲染)里,`Paul_p1-100_scan.md` 有一段公式(标号 `1.3a`/`1.3b` 所在的那段)整体显示为**红色**——这是渲染引擎报错的典型表现(渲染失败时很多前端会把原始 LaTeX 源码或错误提示整体标红)。

## §2 已查清的根因(不需要重新调研,直接用结论)

### 2.1 精确定位

文件:`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan.md`,第 590 行(1.3a)、第 593 行(1.3b)。

**1.3a(第 590 行,真·渲染报错源头)**:

```
$$ \left(\nabla_{\mathrm{t}}+\nabla_{z}\right)\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}=\underbrace{\nabla_{\mathrm{t}}\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}}_{z\text{ directed}}+\underbrace{\nabla_{z}\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}}_{in\text{the}}_{\substack{\text{transverse}\\ \text{plane}}}=-\mu\frac{\partial\overrightarrow{\mathcal{H}}_{\mathrm{t}}}{\partial t} \tag{1.3a} $$
```

问题在中间那个 `\underbrace{...}_{in\text{the}}_{\substack{...}}`——同一个 `\underbrace{}` 节点被连续下标了两次(`}_{...}_{...}`),这是非法的 **double subscript**,LaTeX/KaTeX 会直接报错,导致整段公式(乃至同一渲染块)显示为红色。原书这里应该是"underbrace 下方写了两三行小字说明"("in the / transverse / plane"),OCR 把它错误拆成了两段独立下标,而不是合并进一个 `\substack{}`。`\substack{}` 自身语法(`\\` 换行、包在一个 `_{}` 里)没问题,问题是它前面多余插了一段 `_{in\text{the}}`。

**1.3b(第 593 行,同一类问题的另一种畸变,不一定硬报错但排版明显不对)**:

```
$$ \left(\nabla_{\mathrm{t}}+\nabla_{z}\right)\times\overrightarrow{\mathcal{H}}_{\mathrm{t}}=\underbrace{\nabla_{\mathrm{t}}\times\overrightarrow{\mathcal{H}}_{\mathrm{t}}}_{z\text{ directed}}+\underbrace{\nabla_{z}\times\overrightarrow{\mathcal{H}}_{\mathrm{t}}}_{in the\atop transverse\atop plane}=\sigma\overrightarrow{\mathcal{E}}_{\mathrm{t}}+\varepsilon\frac{\partial\overrightarrow{\mathcal{E}}_{\mathrm{t}}}{\partial t} \tag{1.3b} $$
```

这里只有一个下标,内部用 `\atop`(KaTeX 支持的 TeX 原语)做三行堆叠,理论上不会像 1.3a 那样硬报错,但 "in"/"the"/"transverse"/"plane" 都没包 `\text{}`,会被当数学斜体变量渲染、按乘法间距互相挤在一起(比如渲染成斜体的"inthe"/"transverseplane"),观感明显不对,只是不一定触发红色报错。

**两处本质是同一个原书排版元素(underbrace 下方的多行小字标注)被引擎用两种不同且都不理想的方式拆解成了 LaTeX**,应该合并成同一种规范写法处理,比如统一改成 `\substack{\text{in the}\\\text{transverse}\\\text{plane}}`(具体断行需要对照原书页面图确认,不要瞎猜)。

### 2.2 已确认:问题出在 OCR 引擎输出本身,不是 reconstruct.py 引入的

直接查了原始 `res.json`(未经 reconstruct.py 处理的 PaddleOCR-VL 原始输出):

- 1.3a 在 `_work/page_0031_res.json`,`block_order=15, block_id=18`,`block_content` 逐字节包含上面那段畸形 LaTeX(含双下标)。
- 1.3b 在 `_work/page_0032_res.json`,`block_order=1, block_id=2`,`block_content` 逐字节包含 `\atop` 那段。

**结论:这是 PaddleOCR-VL 引擎对"多行小字下标标注"这种排版元素的识别缺陷,不是本仓库 `reconstruct.py`/`convert.py` 的重组逻辑引入的 bug。** 这意味着修复路径大概率是**确定性后处理清洗**,和现有 `reconstruct.py` 里 `sanitize_latex()` + `KATEX_INCOMPAT_COMMANDS` 那套机制同源(参考 lessons L-T16:`\displaylimits` 也是同样处理方式——engine 输出已知有问题的写法,用一条清洗规则确定性删除/改写,不是靠 ML 判断)。**不要往 reconstruct.py 的分类/归并逻辑(模块①②③新写的代码)里找这个 bug,那部分是好的,问题在更早的 OCR 输出层。**

## §3 已知的同类问题范围(未穷尽,下一会话要扩大排查)

在 `Paul_p1-100_scan.md`(仅这一份 100 页产物,还没扩展到别的书/别的页数档)里粗查:

| 特征 | 命中 |
|---|---|
| `\substack` | 1(仅 1.3a) |
| `\underbrace` | 13(第 590/593/697/699/751/753/792/794/869/886/1318 等行,**这 13 处都是"同类问题的候选嫌疑犯"**,因为已确认的两个真实 bug 都出现在 `\underbrace` 附近,但不代表另外 11 处也有问题,需要逐个人工核对) |
| `\atop` | 1(仅 1.3b) |
| `\text{` 后紧跟中文 | 0(这本书是纯英文教材,不涉及) |

**重要提醒(用正则批量筛"双下标"基本不可行,不要重蹈覆辙)**:试过用正则 `\}_\{[^$]*?\}_\{` 粗筛"连续 `}_{`"模式,命中 80+ 行,但绝大多数是假阳性——因为像 `\overrightarrow{\mathcal{H}}_{\mathrm{t}}` 这种完全正常的写法,字符串层面本身就会出现"多个 `}` 挨着 `_{`"的模式(`\mathcal{H}` 收尾的 `}` + `\overrightarrow{...}` 收尾的 `}` + 后面正常的单个下标 `_{`),嵌套花括号会让 `[^{}]*` 这类非贪婪排除法失效,正则区分不出"同一节点被连续下标两次"和"多层嵌套后接一个正常下标"。**这次唯一确认的 1.3a 真实 bug 是靠通读原文定位的,不是正则筛出来的。**

下一会话如果要在全书范围排查同类问题,建议:
- 不要继续加正则,写一个真正做**括号配对/轻量 AST 解析**的小工具,检测"同一个 group(`\underbrace{...}`/`}` 收尾的顶层节点)后连续出现两个顶层 `_{}`"这种模式,才能可靠区分真假阳性。
- 或者更实际的思路(见 §4):既然已经要建可视化工具,不如直接靠**人工用可视化工具逐页核对**(反正 100 页量级不大),比自动化检测更可靠,也顺便验证了图片输出模块③的效果。
- 除了"双下标"这一种模式,`\atop` 类"未加 \text{} 的多词堆叠"、以及其他 KaTeX 不兼容命令(参考 `reconstruct.py` 里现有的 `KATEX_INCOMPAT_COMMANDS` 清单,目前只收了 `\displaylimits` 一条),都值得作为独立检查项,不要只盯着双下标一种模式。

## §4 任务1:仿 patents 的 debug html 视图工具

### 4.1 已有的参照实现(patents 管线,细节齐全,直接抄思路)

`scripts/pipelines/patents/debug_view.py`(1276 行)。已调研清楚,要点:

- **输入**:不读预落盘的中间产物 json,而是对每页现场调用 `page_classify.classify_document`/`reading_order` 等**管线核心函数**重算一遍(和真实转换同源函数跑的),做到"所见即引擎所判"。可选叠加 `crosscheck_words.py` 产出的 `<stem>_crosscheck.json`(红色"未解释删除"层、坏字形层)。
- **输出**:自包含单文件 HTML(页面图 base64 内嵌,零外部依赖,VS Code 内置预览或浏览器直接打开)。
- **两种运行模式**:
  ```
  # 服务模式(推荐):导出标记直接回写 03_Output/,改代码后浏览器刷新即见最新
  .venv\Scripts\python.exe scripts\pipelines\patents\debug_view.py --serve   # http://127.0.0.1:8077/
  # 静态模式:落一个 <stem>_debug.html(已 gitignore)
  .venv\Scripts\python.exe scripts\pipelines\patents\debug_view.py
  .venv\Scripts\python.exe scripts\pipelines\patents\debug_view.py --collect   # 把浏览器导出的标记 json 归位
  # 其他:--src <pdf|dir>  --zoom 2.0  --md-root ...
  ```
- **布局**:左右并排,仿 MinerU 风格。左栏 = PDF 页渲染图(PyMuPDF `get_pixmap`)+ 绝对定位判定叠加层(可逐层显隐/缩放/平移,区分剔除页眉页脚/保留词/段落区域/crosscheck 未解释删除等不同颜色图层)。右栏 = 该页 reading_order 重排后的中间产物(段落卡片、剔除词清单、页统计,封面页额外出 `bib_parse` 诊断面板)。左右 hover/点击互相定位高亮。内建"标记模式"(M 键)可直接在页面上圈人工核出的问题(误删/漏删/转换错/漏识别四类),标记导出后配合 `check_annotations.py` 做回归断言。

### 4.2 textbooks 管线要做的等价工具(未拍板,新会话先过一遍 brainstorming 再动手,不要直接抄代码)

textbooks 和 patents 的引擎、数据结构都不同(textbooks 是 PaddleOCR-VL 的 `parsing_res_list`,不是 patents 的词级坐标),不能照搬代码,但可以照搬**产品形态**:

- 左栏:该页栅格化 PNG(textbooks 的 `pdf_page_to_png`/`preprocess.py` 已有现成函数)+ 各 block 的 `block_bbox` 叠加框(可选:按 `block_label` 分色,方便一眼看出哪块被判成 noise/visual/passthrough)。
- 右栏:该页 `reconstruct_markdown()` 产出的对应 md 片段(**渲染后的**,不是源码——这样红色报错这种问题才会在视图里直接复现,而不是还要脑内渲染 LaTeX),外加该页 selfcheck 相关信息(命中的 `unhandled_labels`/`visual_warnings`/`column_layout_suspected`,如果这页有的话)。
- 需要决定:静态 md 片段怎么"渲染"出来给 HTML 看(意味着 HTML 里要嵌一个 markdown+KaTeX 渲染器,才能真实复现"red 渲染报错"这个问题本身——这是这次任务的核心诉求,如果只是把 markdown 源码原样摆在右栏,是看不出红色报错的,等于没解决所有者的实际需求)。建议 KaTeX(轻量、可完全离线内嵌,不用连网),不建议 MathJax(重)。
- 是否需要"标记模式"这种人工标注功能(patents 那套用于多轮调和/回归断言),还是先做一个纯只读的"翻页浏览+渲染核对"版本就够用——这个规模判断建议问所有者,不要自己拍板,可能第一版只需要能翻页看就够,标注功能可以是 v2。
- 输出形式建议follow patents 先例:自包含单 HTML,不新增外部依赖(如果决定内嵌 KaTeX,注意 KaTeX 本身是 JS+CSS 库,需要想清楚是 base64/内联嵌进 HTML 还是走 CDN——**走 CDN 违反"自包含离线可用"的既有设计目标,应该内嵌**,量级几百 KB,可接受)。

**这是一个新功能,不是小修复,按项目既有流程规矩,新会话应该先走 `superpowers:brainstorming` 过一遍设计(参考模块③当时的流程:brainstorming→spec→plan→subagent-driven-development),不要直接开始写代码。**

## §5 建议的执行顺序(未拍板,供新会话参考)

1. 先用当前手头信息(§2)判断:1.3a/1.3b 这类问题要不要先用一条 `sanitize_latex()` 清洗规则兜底(比如检测到"同一 underbrace 后连续两个下标"就合并/丢弃第二个,或者退化成不下标只保留公式主体),这个可以独立于 debug 工具先小修一下,成本低、见效快,不需要等 debug 工具做完。
2. debug 工具(§4)按 brainstorming 流程走一遍完整设计讨论,再动手实现。工具做出来后,用它把 100 页语料**逐页人工过一遍**,系统性找出所有类似"红色/渲染异常"的实例(不只是双下标这一种,§3 已经提醒过正则批量筛不可靠)。
3. 排查结果视情况决定:零星几例就手工列清单逐条清洗规则修;如果发现是某类排版元素(比如所有"underbrace 下方多行小字标注")系统性都识别错,再考虑要不要针对这一类写专门的清洗/归一化规则。

## §6 恢复方法 / 红线(继承)

- 进度记录:`TODO.md` 2026-07-03 节(模块①②③已记为完成,本文档对应的任务还没记入,新会话开始动手后再记)。
- 红线:不改 patents/general/engine;确定性优先、ML 只判断不改字符;`02_Source/` 只读;每管线独立 venv;对外操作(merge/push/装大依赖)前所有者确认。
- 涉及的产物路径:`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/`(md + `_work/` 原始 res.json + `.assets/` 图片,均可直接用,不需要重新跑 GPU 或补裁)。
