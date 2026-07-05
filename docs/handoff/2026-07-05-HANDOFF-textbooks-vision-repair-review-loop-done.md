# HANDOFF —— textbooks 公式视觉修复流水线:Phase A + 人工确认门 + 审核 UI 已建成,交接新会话执行

> 撰写:2026-07-05。承前:[2026-07-04 视觉修复方案交接](2026-07-04-HANDOFF-textbooks-formula-vision-repair.md)定的 Phase A/B/C 顺序与叠加层设计,本会话把 Phase A 整条链路(检测→裁图→视觉修→corrections→应用)建成并接了人工确认门 + 审核 UI,在真实 100 页书上全流程跑通、逐条人工审过。**方案已验证,新会话直接执行下面的开放项,勿推翻已定决策。**

> **【2026-07-05 续会话更新】** 又一轮会话补齐了"采纳→落 md"闭环并订正了两处过时/错误描述:
> - **已建成**:debug 审核点"采纳"后,修正现在会**自动进入最终 `.md`**——新增 `convert.reassemble_md` 复用唯一的 `assemble()` 幂等重组;`debug_view` serve 加 `/reassemble` 路由 + dirty 门控 + 线程锁 + **启动即对账**;前端**翻页/`S` 键**触发、`debug_view --reassemble` **CLI** 入口。257 测试全绿。设计与计划见 `docs/superpowers/{specs,plans}/2026-07-05-textbooks-accept-reassemble-md-*`。
> - **订正 §2**:Paul 的 corrections 现为 **13 条全部 accepted 且已全部落盘 `.md`**(原表"9 accepted + 4 待审"已过时;那 4 条已采纳并回填)。
> - **订正 §1.2 / §3.4 打包措辞**:见下方各节的〔订正〕——"禁一切打包"是错的,只该禁 L-T31 合成图打包。
> - **仍未修**:§3.1 滚动 bug、§3.2 p48 检测、§3.3 Phase C 生成侧调度、§3.4 多后端封装 —— 均保留为开放项。

## §0 一句话结论

Phase A 闭环已建成、已用真实数据验证、AI 视觉识别效果很好(9/9 已采纳,0 驳回)。下一步:**修 2 个已知 bug**(滚动异常、p48 硬报错未纳入检测)+ **把它折进主转换管线(Phase C)** + **给视觉调用加并发与多后端切换**。

## §1 本会话已完成(全部 TDD,247 测试全绿,真实数据验证过)

1. **Module 1** `scripts/pipelines/textbooks/debug_repair.py`:`find_suspicious_blocks`(扫 display_formula 块的疑似)→ `crop_at_scale`(按 DPI 比例换算 bbox + padding)→ `build_repair_worklist`(编排,只渲染真正有疑似的页,产 `<stem>_repair/{crops/,worklist.json}`)。
2. **Module 2** `scripts/pipelines/textbooks/vision_repair.py`:无头 `claude -p` 读裁图(`_resolve_claude_bin` 绕过 Windows npm shim 坑,见 L-T28)→ `run_vision_repair` 产 `<stem>_corrections.json`。跨调用并发 `ThreadPoolExecutor(max_workers=parallel)`(默认 `parallel=3`)。
   > **〔2026-07-05 订正〕** 原文"不做任何形式的图片打包(别重新实验)"措辞有误——把两种机制不同的打包混为一谈:
   > - **L-T31 合成图打包**(N 图拼成一张、一次 Read,分辨率被压):实测 10/10 下标读错,**确实该禁**。
   > - **L-T30 路径列表打包**(N 个独立文件、各自原分辨率 Read、结果按 key 归并):只测过更慢更省钱,**从未测过正确性**,无降质证据。
   >
   > 当前 `call_claude_vision_batch`(`run_vision_repair` 默认 `batch_size=5`)走的正是 **L-T30 路径列表打包**,真实跑 13/13 采纳是其不降质的正面证据。**结论收窄:仅禁 L-T31 合成图打包;L-T30 路径列表打包可用**,代码无需改。
3. **Module 3** `scripts/pipelines/textbooks/corrections.py`:`load_corrections`/`apply_corrections`(按 page+block_id+content_fingerprint 匹配,**只应用 `status=="accepted"`**)/`set_correction_status`。已接入 `convert.py assemble()` 与 `debug_view.py build_payloads`——**这就是 Phase C 想要的"应用"半条腿,已经在生产路径里了,缺的是"生成"那半条腿的调度**(见 §3.3)。
4. **人工确认门**(所有者反馈"生成即自动应用没有关卡"不符合红线后补的):`vision_repair.py` 新产出的修正一律 `status:"pending"`;`apply_corrections` 只应用 `accepted`。
5. **debug 视图审核 UI**(`debug_assets/{app.js,app.css,template.html}` + `debug_payload.py`/`debug_view.py`):
   - `R` 键筛选"待审修正"(与既有 `E` 疑似筛选互斥,待审页排在问题索引最前)。
   - 每条待审公式,右栏内嵌审核卡片:**原图裁切(真实源图,非引擎渲染)与 AI 修正左右并排**,一眼看差异;置信度标签。
   - **采纳/驳回可随时改判**(不是一锤定音),按钮即时写盘但**不立即刷新整页**,只在翻页/跳页时(`gotoIndex`)刷新一次——一页多处要改不必点一次等一次。
   - `serve()` 新增 `/corrections` POST 路由(`handle_post`,已抽成纯函数单测)。
6. **真实 100 页书全流程验证**(`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/`,非临时数据):13 处疑似公式全部走完 detect→crop→vision→corrections→人工审→apply 闭环,详见 §2。

## §2 当前真实数据状态(可直接核对)

`Paul_p1-100_scan_corrections.json`(13 条):

| page/block | 状态 |
|---|---|
| 44/13, 46/11, 48/12, 49/3, 49/6, 49/9, 49/12, 49/15, 49/18, 50/3, 53/20, 54/6, 54/15 | **accepted**(13 条,全部已落盘 `Paul_p1-100_scan.md`) |

> **〔2026-07-05 订正〕** 原表写"9 accepted + 4 待审(还没人看)"已过时:那 4 条(50/3、53/20、54/6、54/15)后来也已采纳,共 13 条全部 accepted。且续会话建成"采纳→落 md"闭环后,对本文档跑了一次 `--reassemble` 回填,**13 条修正现已全部写进 `.md`**(此前只有前 9 条落盘)。

驳回 0 条——曾演示时误点驳回 49/6(1.56b),已改回 accepted 并重新生效,非遗留问题。**AI 视觉修复质量结论:13/13 人工核对通过,无一驳回,含"曲面 s' 而非围道 c'"这种最容易被同化的 case 也读对**——这是 §3.3 建议接回主管线的实证依据。

## §3 已知问题 / 开放项(新会话按序处理)

### §3.1 滚动异常 bug——尝试修过,没修好,别重复已试的方案

现象:页面内容较长(审核卡片多时更容易触发)→ 顶部工具栏跟着滚出视口、底部露出大片空白、鼠标滚轮回不到顶部。

**已排除的假设**:`body` 只有自己 `overflow:hidden`、`html` 没锁高度,导致外层 `<html>` 滚动——已加 `html{height:100%;overflow:hidden}` + `body{height:100%}`(见 `debug_assets/app.css`),**所有者验证后反馈问题依旧**,说明根因不止这层,或完全是另一层。

**下一步排查建议**(别只读代码猜,用真浏览器 devtools 复现后再动手):
- 打开 devtools,复现时看 `document.scrollingElement` 到底是谁在滚、它的 `scrollHeight`/`clientHeight` 是多少。
- 怀疑对象升级到本会话新加的元素:`.corrcard` 里的 `<img class="corrphoto">` 有没有在某些裁图尺寸下撑爆 `#rightpane` 内部宽度导致横向溢出连带纵向布局错乱;`#film`/`#filmzone`(`position:absolute`,悬停出的缩略图条)是否在特定页面高度下跳出预期位置。
- 复现步骤要写清楚给下一个会话:大概是"某页审核卡片较多/较长时,往下滚动到某处后触发",具体触发点需要所有者补充或亲自用 devtools 抓。

见 lessons L-T33。

### §3.2 p48 的 1.53b KaTeX 硬报错——现有检测漏了它,不是本会话改动引入的

`Paul_p1-100_scan_render_errors.json` 早就记录了这条(`l=-\frac{\mu\int\limits_{c}^{\overrightarrow{\mathcal{H}}_{t}\cdot\overrightarrow{a}_{n}dl`,缺右花括号截断),但 `debug_repair.py::find_suspicious_blocks` 只认"裸大算符"和"`\frac` 撇号分母"两种启发式,**没有覆盖"花括号不配对/截断"这类硬报错**,所以这处从没进过裁图/视觉修复流程。

**建议做法(别造新正则)**:`render_errors.json` 已经是确定性更强的信号源(KaTeX 真报错,零假阳性),`find_suspicious_blocks`/`build_repair_worklist` 应该把它也纳入候选池——按 page 反查该页哪个 display_formula 块对应这条报错(可用 bbox 邻近或该页唯一报错块兜底),跟启发式疑似合并去重后一起喂给裁图/视觉修复,而不是为每种新报错模式单独写检测正则。

见 lessons L-T34。

### §3.3 折回主管线(Phase C)——审核结果证明质量够,可以做

§2 的 9/9 采纳、0 驳回证明视觉修复质量可信。**应用侧已经在 `convert.py` 里了**(§1.3),缺的是"生成"侧的调度策略:
- 什么时候自动跑 `debug_repair.py` + `vision_repair.py`?每次转换后自动跑,还是留一个 `convert.py --repair` 开关手动触发?
- 人工确认门**必须保留**,不管调度策略怎么定——生成 corrections 可以自动,但 `status: accepted` 之前不能生效,这是红线,不是讨论项。
- 建议:先做成 `convert.py` 的显式可选阶段(命令行开关,不默认跑),等这条路径也经过几轮真实使用验证后,再考虑要不要默认接入。

### §3.4 视觉调用的并发 + 模型切换——参考主仓经验,别重复踩坑

`vision_repair.py` 现在只有 claude 一个后端;`run_vision_repair` 已有 `batch_fn`/`parallel`/`vision_fn` 参数骨架(为单元测试留的注入点),但**没有做成"多后端可配置切换"这层封装**。实现时:

- **调用方式全部参照 Project_MRI_Safety 的既有经验,不要重新踩坑**:
  - `00_System/scripts/kb_core.py` 的 `resolve_backend_argv`/`call_backend` 模式(后端解析、subprocess-via-stdin、`--strict-mcp-config`/空 MCP 配置禁工具)。
  - `00_System/SOPs/Engineering/SOP-Batch_Agent_Run.md`(禁 MCP、`run_batches` 进度、适度并发 `parallel 3`、何时上后台+Monitor)。
  - `00_System/Lessons/lessons_kb_ingest.md`(K1~K16,尤其 K7 npm shim、K11 kimi 独立 exe + UTF-8 locale 两前提、K13/K15 MCP 污染、K16 进度+并发)。
  - 本仓 L-T28(Windows subprocess 调 claude 的 npm shim 坑,已复现过一次,`_resolve_claude_bin` 已经是解法)。
- **Kimi 是否接入待定**:此前决定"暂缓,等实际调试阶段再评估"(见 `docs/handoff/2026-07-04-HANDOFF-textbooks-formula-vision-repair.md` §1.2)。现在 claude 单后端质量已验证很好,是否还需要 Kimi(省钱/限流备份)是所有者要拍板的产品决策,不是技术阻塞——新会话开工前建议先问一句,别默认就去接。
- **"并发"这个词在视觉调用场景下的含义已经踩过一次坑**(L-T30):多图塞进一次 prompt 打包 ≠ 并发,真正的并发是"同时跑几个独立子进程"。新会话设计并发时,直接照抄 `run_vision_repair` 现有的 `ThreadPoolExecutor(max_workers=parallel)` 那层。
  > **〔2026-07-05 订正〕** "不要退回到打包思路"专指别用 **L-T31 合成图打包**;**L-T30 路径列表打包**(当前默认 `batch_size=5`)与并发不冲突、可并存,是省启动开销的正当手段,无降质证据。详见 §1.2 订正。

## §4 给新会话的第一步

按 §3 顺序:先用真浏览器 devtools 定位滚动 bug 根因(§3.1)→ 把 `render_errors.json` 接进疑似候选池修 p48(§3.2)→ 跟所有者确认 Phase C 调度策略与 Kimi 是否接入(§3.3/§3.4)→ 参照 Project_MRI_Safety 经验实现多后端 + 并发封装。**红线不变**:人工确认门保留、res.json 不动、`02_Source/` 只读、对外操作(装依赖/merge/push)前所有者确认。
