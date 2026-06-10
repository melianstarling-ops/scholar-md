# debug_view 可视化调试工具工程经验（HTML/webview 篇）

本文记录 `debug_view.py`（自包含单 HTML 调试视图）v1→v7 迭代中踩到的前端/交互坑及根治思路。
与 `lessons_layout_quirks.md`（版面引擎篇，L1–L7）并行，编号 D1–D7。
适用范围：本仓所有"Python 生成单文件 HTML 工具"的后续开发（含未来 general/textbooks 管线的调试视图）。

---

## 总则（先读这四条再看个案）

1. **症状像"逻辑错"，先查"样式/环境"。** 僵尸气泡(D1)的两个症状（不消失、点不动）看似事件逻辑 bug，
   根因是一行 CSS 优先级；拖拽失灵(D2)看似代码没写，根因是 webview 事件链路。改 JS 前先排除 CSS/宿主。
2. **宿主是 VS Code webview，不是标准浏览器。** 事件委托链、File System Access、下载行为都可能降级。
   交互绑定尽量逐元素直挂 + pointer capture；权限敏感功能必须有确定性退路(D2、D7)。
3. **判定数据与主管线同源现算，绝不在工具里复刻引擎逻辑。** 宁可重构主引擎透出富结构，
   再用产物哈希逐字节比对证明无副作用(D6)。复刻=必然漂移=调试视图说谎。
4. **视觉微调按"数值+参照物"沟通，每轮可回退。** "上移/下移"这类相对描述参照物歧义，
   Dock 位置 4 轮返工最终回滚(D5)。按轮提交 git 让"恢复"只是一次 revert。

---

## D1 — `hidden` 属性被 ID 选择器的 display 覆盖（僵尸气泡）

### 现象
语义气泡选定后不消失；残留的气泡再也点不动（"不消失"与"没法选择"两轮反馈，同一根因）。

### 根因
CSS 写了 `#pop{display:flex}`。`hidden` 属性靠 UA 样式 `[hidden]{display:none}` 生效，
其优先级是属性选择器 (0,1,0)，**低于 ID 选择器 (1,0,0)** → `hidden=true` 形同虚设，
气泡从未真正隐藏。关闭逻辑其实一直在跑：状态已清空（`pop.key=null`），但元素留在屏幕上，
点击色点走到 `applyCat` 后因 key/pending 皆空而无事发生——"僵尸气泡"。

### 对策
一行显式覆盖：`#pop[hidden]{display:none}`。凡是"自定义 display + hidden 属性"的元素一律加这行
（或改用 class 控制显隐，不混用两套机制）。

### 教训
两个看似无关的症状（视觉不消失、交互失效）共享一个根因时，优先怀疑"元素根本没被隐藏/移除"，
用 DevTools 看元素是否还在，比读事件代码快得多。

---

## D2 — webview 里拖拽别走"委托链"，用逐元素 pointer capture

### 现象
标记框拖动完全无反应——但点击同一元素弹气泡正常（"click 可用 ≠ drag 可用"）。

### 根因
拖动最初的实现是委托链：`stage` 监听 pointerdown、`document` 监听 pointermove/pointerup。
在 VS Code webview 里这条跨元素链路受文本选择、事件竞争干扰，不可靠。

### 对策
改为**逐元素直挂 + 指针捕获**：在 `drawAnn()` 给每个标记框 `pointerdown` 里
`setPointerCapture(ev.pointerId)` + `ev.preventDefault()`（杀文本选择），move/up 挂在框自身，
配 CSS `touch-action:none`、容器 `user-select:none`。
另两个配套细节：
- **4px 移动阈值**区分"点击(弹气泡)"与"拖拽(移动)"；
- 拖后提交会重建元素，随后的 click 事件可能因目标已脱离 DOM 而**不触发**——
  `suppressClick` 旗标必须**定时清除**（250ms），否则吞掉下一次正常点击。

### 教训
单文件工具跑在 webview 时，把"浏览器标准行为"当不可信默认值：交互绑定越短路越稳
（元素自身 > 容器委托 > document 委托）。

---

## D3 — 弹层锚定"目标元素 rect"，不锚"鼠标点击点"

### 现象
气泡压在标记框上，遮挡正要观察的内容。

### 根因
气泡定位用的是 `e.clientX/Y`（点击点就在框内，弹层自然盖住框）。

### 对策
`openPop(anchorRect, …)`：锚定目标框的 `getBoundingClientRect()`，悬于框上方 10px，
顶部放不下自动翻到框下方；水平居中并向视口内夹取。
**时序坑**：`drawAnn()` 会重建元素，取 rect 必须在重建**之前**（先存 `r0` 再重渲染）。

---

## D4 — 弹层开/关竞态：打开它的那次点击会顺手把它关掉

### 现象
弹层行为偶发错乱：刚弹出即消失，或重复打开。

### 根因
"点外部关闭"监听挂在 document 上，而**打开弹层的那次 click 自己会冒泡到 document**。
`stopPropagation` 只能挡住同源路径，拖拽结束的合成 click 等旁路挡不住。

### 对策
双保险：打开路径 `stopPropagation()` + 弹层 `justOpened` 守卫（开后 50ms 内忽略关闭）。
选定后的执行顺序定为"**先 closePop 再落数据**"——后续 `drawAnn()` 重渲染时弹层引用已失效，
先关再改杜绝读到僵尸状态。

---

## D5 — 视觉微调的沟通协议（Dock 位置 4 轮返工的教训）

### 复盘
"Dock 上移 10px"：agent 以 CSS `bottom` 值为参照（正值=抬高），所有者以视觉感受为参照，
两边对"方向"的理解相反；中间又叠加了"静止态把手是否可见"这一隐藏变量（完全滑出 → 所有者
视为"沉下去了"）。来回 4 轮（悬浮卡/上移 24px/下沉 -10px）后回滚到原始形态。

### 协议（落为习惯）
1. 视觉位置调整，需求方给"**X 的哪条边，离哪条参考线，N px**"或**截图+箭头**；
2. agent 每轮必须说清"改完后**静止态/展开态各自长什么样**"（隐藏变量显式化）；
3. 每轮独立 commit——"恢复"只是一次 revert，不靠记忆重拼（本次回滚即受益于此）；
4. 连续两轮方向不收敛 → 停止猜测，要求数值/截图。

---

## D6 — 单文件 HTML 工具的架构基线

v1 起就定对、后续全程受益的四条：

1. **叠加层全部百分比定位**（坐标/页宽高 ×100%）→ 缩放、适宽、指针锚点缩放全部零成本，
   框永不漂移。
2. **DATA(JSON) 与渲染(JS) 分离**，页面图 base64 内嵌（16 页 ≈7.7MB 可接受）。
   嵌入时必须 `json.dumps(...).replace("</","<\\/")`，防文本中 `</script>` 截断脚本。
3. **判定数据与主管线同一批函数现算**（所见即引擎所判）。需要更细粒度时重构主引擎透出
   富结构（如 `_column_paragraphs` → `_column_paragraph_infos`），旧接口语义不变，
   并用**产物 md 哈希逐字节比对**证明重构零副作用——绝不在工具里复刻判定逻辑。
4. **localStorage 以文档名为 key** 存标记/主题/偏好 → HTML 重新生成后用户状态存活。

另两条交互细节：auto-hide 面板加 **160ms hover-intent 延迟**防路过误触；
弹出动画用长缓动 `0.7s cubic-bezier(.22,1,.36,1)`（ease-out，快入缓停），默认 ease 会显得突兀。

---

## D7 — 浏览器写文件的安全边界：下载 + CLI 归位，不赌 FSA

### 现象/根因
"标记自动落盘到工作区"做不到：静态 HTML 无法写任意路径；File System Access API
在 webview 不可靠、每会话需用户手势授权一次，被所有者以权限顾虑否决。

### 对策（落定的模式）
- **导出 = 浏览器下载（落"下载"目录）+ 复制剪贴板**，双通道；
- **CLI 归位**：`debug_view.py --collect` 把下载目录的 `<stem>_annotations*.json`
  （同名取最新）移到 `03_Output/patents/<stem>/`，与 md/selfcheck/crosscheck 同处，agent 直接读；
- localStorage 实时兜底，标记不怕忘导出。

### 教训
权限敏感能力（写盘、剪贴板、通知）一律按"**优雅降级 + 确定性退路**"设计：
浏览器端做它确定能做的（下载/剪贴板/localStorage），路径语义交给 Python 端收口。

---

## 关联代码与产物

- 工具：`scripts/pipelines/patents/debug_view.py`（生成 `<stem>_debug.html`，已全局 gitignore）
- 引擎重构：`reading_order.py:_column_paragraph_infos`（D6.3，产物哈希比对通过）
- 标记导出：`<stem>_annotations.json`（schema 含 `legend` 四类语义 + `kind: word|region`）
- 迭代轨迹：commits bff1e74(v1) → e0f40c9(v2) → e555ea4(v3) → 8c51957(v4) → 45b3a9a(v5)
  → 745aa54/cd4346f/ebcd2e1/3687275(位置实验) → f216794(回滚收尾)
