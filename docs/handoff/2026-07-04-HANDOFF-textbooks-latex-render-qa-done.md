# HANDOFF —— textbooks LaTeX 渲染排查 + debug 可视化工具,自主执行完成

> 撰写:2026-07-04(自主执行会话,通宵)。承前:[2026-07-03 交接](2026-07-03-HANDOFF-textbooks-latex-render-qa.md)(所有者发现 1.3a 公式红色渲染报错,交调研任务)。
> 所有者在会话中途授权自主执行("自行判断分叉点,明早看成果"),故本轮无逐步审批,分叉点由执行者判断,证据见下。

## §0 TL;DR(先看这个)

1. **红的原因查清了,而且比交接文档以为的多**:全书 100 页真实语料共 **4 处 KaTeX 硬报错(红)**,交接只提了 1.3a/1.3b。用确定性清洗**修掉了 3 处**,剩 **1 处是内容级 OCR 损坏(p48),不能瞎猜,留给你对照原书人工修**。另有 2 处 KaTeX 警告(非红)。
2. **debug 可视化工具做好了**:自包含 HTML,左页面图+叠框、右 KaTeX 渲染(红直接复现),报错索引一键跳红页,5 类人工标注。**打开方式见 §3**。
3. 全程 TDD,167 单测绿,工具经 headless + jsdom 双重无人值守验证。红线全守。

## §1 全书渲染异常清单(核心交付 —— 回答"还有没有别的红")

用新写的 headless KaTeX 扫描器(node+vendored katex@0.16.11,`throwOnError`)扫全书 676 个公式。**修复前 4 红,修复后 1 红**:

| # | 页 | 现象 | KaTeX 报错 | 处置 | 状态 |
|---|---|---|---|---|---|
| 1 | p31 | **1.3a** underbrace 下多行标注被拆成同节点相邻双下标 `_{A}_{B}` | Double subscript | `sanitize_latex` 合并 `_{A\ B}` | ✅ 已修 |
| 2 | p32 | **1.3b** 同标注被拆成下标内链式 `A\atop B\atop plane` | only one infix operator per group | 链式 atop 归一 `\substack{A\\B\\C}` | ✅ 已修 |
| 3 | p48 | `\cdot d`(点积+微分d)被 OCR 粘成 `\cdotd` | Undefined control sequence | `\cdotd`→`\cdot d` | ✅ 已修 |
| 4 | **p48** | **1.53b** `\frac` 分子闭合`}`丢失 + `\cdot\vec{a}_n` 被误读成上标 + 分母缺括号 | Unexpected end of input, expected '}' | **内容级损坏,不可确定性修** | ⚠️ **待你人工修** |

> ⚠️ **交接文档说 1.3b"不一定硬报错"是错的**——KaTeX 明确拒绝一个 group 里链式多个 `\atop`,它同样是硬红。已修。

**另有 2 处 KaTeX 警告(渲染成功、不红,但 LaTeX 不规范,可选修)**:

| 页 | 警告 | 说明 |
|---|---|---|
| p44 | `\\ does nothing in display mode` | 某 display 公式里有 `\\` 换行(display 模式无效);可能原书是 aligned 多行,值得对照 |
| p49 | `Unrecognized Unicode character "↳" (8627)` | 公式里混入 `↳` 字符,KaTeX 用兜底字形;OCR 杂质 |

完整机读清单:`03_Output/.../Paul_p1-100_scan_render_errors.json`(含页号/模式/latex_head/错误信息)。

### §1.1 p48 的 1.53b 该怎么修(给你的线索,别我瞎猜)

坏块(p48 block_id=6):
```
$$ l=-\frac{\mu\int\limits_{c}^{\overrightarrow{\mathcal{H}}_{t}\cdot\overrightarrow{a}_{n}dl}I(z,t) $$
```
**同页 block_id=3 给出了正确形式**(结构完好,可参照):
```
$$ l\Delta z=-\frac{\Delta z\mu\int\limits_{c}\vec{\mathcal{H}}_{t}\cdot\vec{a}_{n}d l}{I(z,t)} $$
```
可见 block 6 是 1.53b 定义的**畸变重复**:把 `\cdot\vec{a}_n` 错读成了 `^{...}` 上标、丢了分子闭合 brace、分母 `I(z,t)` 没加 `{}`。正确应类似 `l=-\frac{\mu\int\limits_{c}\vec{\mathcal{H}}_{t}\cdot\vec{a}_{n}dl}{I(z,t)}`。**建议用可视化工具翻到 p48,对照左栏原书页面确认后手改**(改 md,或若要根治改引擎输出——但这是单页 OCR 幻觉,不值得写通用规则)。

## §2 修复了什么(确定性清洗,红线内)

`scripts/pipelines/textbooks/reconstruct.py` 的 `sanitize_latex()` 新增 3 条确定性规则(与既有 `\displaylimits` 清洗同源——engine 输出已知坏写法,规则确定性改写,不靠 ML):

- `_collapse_double_subscript`:括号配对扫描(**非正则**——交接 §3 已证正则区分不出真双下标与多层嵌套后接单下标),相邻 `_{A}_{B}` 合并 `_{A\ B}`。
- `_collapse_chained_atop`:一个 group 里 2+ `\atop` 归一 `\substack`(单 `\atop` 合法不动,`\atopwithdelims` 不误伤)。
- `\cdotd`→`\cdot d`:负向边界只改 `\cdotd` 本身,不误伤 `\cdot`。

**共同原则**:只消红、零丢失内容、**不猜原书断行/排版**(漂亮的三行 `\text{}` 堆叠属排版精修,需对照原书,留人工)。1.3b 的裸文字斜体("inthe"挤在一起)也是软问题,没在消红规则里处理。

⚠️ **磁盘上的 md 已用修复重生成**(`convert_pdf` 走现有 checkpoint,无 GPU)。旧版备份在本会话 scratchpad(会随会话清理,不重要,git 里有 before/after 证据在 commit message)。

## §3 debug 可视化工具 —— 怎么用

产物(gitignore,在盘上):`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan_debug.html`(20MB 自包含,**直接双击/VS Code 打开即可**,离线,无需 serve)。

- **左栏**:每页原书图(JPEG内嵌)+ 各 block 的 `block_bbox` 叠框,按 `block_label` 分色(顶栏可逐层显隐);hover 看 block_id/label/order/内容首行。噪声块(header/number)虚线弱化。
- **右栏**:该页 `reconstruct` 的 md 经 markdown-it + KaTeX **渲染后**显示——**红色报错在这里直接复现**(翻到 p48 能看到那处红)。顶部信号徽章(双栏嫌疑/未知label/KaTeX报错)。
- **报错索引**(顶栏下拉):离屏渲染全书,列出所有有红的页,点击跳转。当前语料只列 p48。
- **标注模式**(按 `M` 或点"✎ 标注"):在左栏页面拖框选问题,选 5 类之一(渲染报错/排版错/漏内容/错归类/图片位置)+备注,"⭳ 导出"落 `<stem>_annotations.json`。

命令行(重建/服务/回归):
```bash
# 重新生成静态 HTML(改了 reconstruct 代码后重跑)
.venv-textbooks\Scripts\python.exe -m scripts.pipelines.textbooks.debug_view --doc <stem-dir>
    可选:--serve(http://127.0.0.1:8078,改代码刷新即见) --no-images(快而小) --img-dpi 110
# headless 扫全书硬报错(无人值守,落 render_errors.json)
node scripts\pipelines\textbooks\debug_assets\scan_katex_errors.mjs --md <md> --out <json>
# 标注转回归断言(cat1/3/4 确定性判据;有回归退出码1)
.venv-textbooks\Scripts\python.exe -m scripts.pipelines.textbooks.check_annotations --doc <stem-dir>
```

## §4 组件与验证

- `debug_payload.py`(纯函数:叠框/逐页md/信号,TDD 8 测)、`debug_view.py`(CLI 静态/serve/collect)、`check_annotations.py`(TDD 7 测)、`debug_assets/`(template+app.js+app.css+vendored katex/markdown-it/@vscode-markdown-it-katex+扫描器)。
- **关键工程判断**:数学渲染必须用 `@vscode/markdown-it-katex` 插件,**不能**用裸 `renderMathInElement`。裸方案下 markdown-it 会先把公式里的 `_`/`*` 当强调解析,污染 LaTeX,产生**假红**(实测误报 p62 等 4 页)。插件在 inline 阶段 tokenize `$…$` 保护数学,判红与 headless 扫描器一致(仅 p48)。这也是 VS Code 预览的做法,最忠实。
- **无人值守验证**(无法交互开浏览器):① headless 扫描器复算 p48 出红、p31/p32 已清;② jsdom 冒烟真在 DOM 跑 init——渲染/判红(p48 出红)/图层(18)/翻页/报错索引(恰 p48)全生效,浏览器判红==扫描器;③ 结构验证(100页/内嵌图100/无残留占位符)。167 python 单测绿。

## §5 待你决定 / 遗留

1. **p48 1.53b**(§1.1):唯一没修的红,内容级损坏,请用工具对照原书手改。
2. **p44 `\\`、p49 `↳`** 两警告:非红,可选修;建议翻工具看看是不是有信息丢失。
3. **标注回归入 CI?**`check_annotations` 已就绪(cat1/3/4 有判据,cat2/5 记录),但目前无标注数据。你用工具标注后可挂进测试。
4. **源 PDF 脆弱性**:本语料源只以上会话 scratchpad 临时文件存在(`67cdc87b-.../Paul_p1-100_scan.pdf`,现存但随时可能被清)。静态 HTML 已内嵌页图故不受影响;但若要重新 `--serve` 或重栅格化,建议把该 PDF 拷到稳定位置并 `--src` 指定,或重新从原书切片。
5. **vendor 依赖**:`debug_assets/vendor/` 已提交离线库(katex/markdown-it/@vscode-katex,~1.4MB),运行期零外部请求。升级见 `vendor/README.md`。

## §6 红线(全部遵守)

不改 patents/general/engine;确定性优先、ML 只判断不改字符(本轮全是确定性规则);`02_Source/` 只读;textbooks 独立 `.venv-textbooks`;**未做对外操作**(无 merge/push;装了 node 侧 dev 依赖仅在 scratchpad,vendored dist 已拷入仓库);产物路径未动他人文件。

## §7 提交(本会话,分支 feature/textbooks-engine)

- `2772a98` fix: 双下标合并
- `14c31cd` docs: spec
- `30107be` feat: 扫描器 + 链atop/cdotd + vendor
- `86e4bc4` feat: 可视化工具
- (本文档 + TODO 一并提交)

## §8 恢复方法

- 进度:`TODO.md` 2026-07-03 节下"进行中"已改记完成。
- spec:`docs/superpowers/specs/2026-07-03-textbooks-debug-view-design.md`。
- 未合并:分支仍领先 main(承前 49 commit + 本轮 5),**合并前需所有者确认**(红线)。
