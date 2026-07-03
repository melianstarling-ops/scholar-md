# textbooks 图片输出与 order=None 块补齐设计文档(模块③)

- 日期:2026-07-03
- 状态:设计定稿(brainstorming + 独立审核两轮迭代,所有者已逐块批准,待实现)
- 分支:`feature/textbooks-engine`
- 范围:补齐 `block_order is None` 但携带真实内容的块——`image`/`chart` 裁图输出、`table`/`footnote`/`figure_title` 直通输出、按 y0 归并进正文流;配套 selfcheck 可观测性扩展
- 不涉及:模块①②(有序块 label 补齐 + selfcheck 空探针修复,已实现,见交接文档);patents/general 管线;OCR 引擎与性能

> 立项依据:[textbooks 转换质量交接](../../handoff/2026-07-03-HANDOFF-textbooks-conversion-quality.md) §3、
> 100 页真实语料实测(`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/_work/page_XXXX_res.json`,下 §1)。

## 1. 背景与证据基础(100 页真实语料实测,2026-07-03)

模块①②解决了"有序但未处理"的丢失(44→0);本设计解决"无序即丢弃"这一类。
[reconstruct.py:50](../../../scripts/pipelines/textbooks/reconstruct.py#L50) 现状对 `block_order is None` 一律剔除,
100 页语料里这一规则连坐误杀了以下真实内容:

| label | 实例数 | block_content | 处置 |
|---|---|---|---|
| `image` | 54 | 全空(已逐一复核) | 裁图 |
| `chart` | 12 | 全空(已逐一复核) | 裁图 |
| `figure_title` | 89 | 非空(图注/子图标签) | 直通 |
| `footnote` | 2 | 非空(注释文本) | 直通 |
| `table` | 1 | 非空(完整 `<table>` HTML,p76 已抽查) | 直通 |
| `header` | 89 | 非空(页眉文字) | 噪声,丢弃(维持现状) |
| `number` | 85 | 非空(页码) | 噪声,丢弃(维持现状) |
| `header_image` | 2 | 全空 | **噪声,丢弃**(见下) |

其余 10 种 label(`text`/`display_formula`/`formula_number`/`paragraph_title`/`reference_content`/
`content`/`doc_title`/`abstract`/`algorithm`/`seal`)在语料中 100% 有序,归模块①②管辖。

**header_image 归入噪声的证据**:仅有的 2 个实例,p24 bbox `[1156, 0, 1233, 16]` 是贴页面上边缘的
77×16px 页眉装饰碎片(`header` 的图像同族);p6 bbox `[540, 194, 702, 355]` 是版权页出版社 logo。
无任何实例支持它是正文内容。

**块字段**(res.json 实测):`block_bbox` / `block_content` / `block_id` / `block_label` /
`block_order` / `block_polygon_points` / `group_id`。**`block_id` 是引擎原生字段**,裁图文件命名直接用它,不自造索引。
100 页全部 875 个有序块 + 245 个无序块均带 bbox(经验观察,非引擎保证——缺 bbox 的行为仍须定义,见 §2/§3)。

## 2. label 三层分类(判定顺序即优先级)

```python
_VISUAL_LABELS = {"image", "chart"}                                    # 内容空,裁图
_PASSTHROUGH_UNORDERED_LABELS = {"table", "footnote", "figure_title"}  # 内容非空,直通
_KNOWN_NOISE_LABELS = {"header", "number", "header_image"}             # 页眉页脚页码及其图像变体,故意丢弃
```

`block_order is None` 的块按序判定:

1. 命中 `_VISUAL_LABELS` → 裁图 + 插入图片引用;**缺 bbox → 告警 + 丢弃**(裁图与 y0 归并都无从谈起);
   若 `block_content` 意外非空(当前语料 0 例)→ 图 + 文本都输出 + 告警,不静默吞文本。
2. 命中 `_PASSTHROUGH_UNORDERED_LABELS` → 内容为空静默跳过(与有序块空内容路径一致),否则直通插入。
3. 命中 `_KNOWN_NOISE_LABELS` → 静默丢弃(维持现状)。
4. 都不命中、内容非空 → **告警 + 丢弃**:stderr 实时一条 + 计入 selfcheck `unhandled_labels`(§5.6)。
   这是"未来第 7 种 order=None 真内容 label"的兜底——告警先冒出来,不再需要重走一遍人工排查。
5. 都不命中、内容为空 → 静默丢弃(无害)。

**与有序块 else 分支的不对称性(有意为之,勿"顺手统一")**:有序块的未知 label 直通输出,因为
`order≠None` 已是模型对"这是阅读流内容"的判定,直通风险低;无序块的先验是噪声(header 89 + number 85
占无序非空文本块的绝大多数),保守丢弃 + 告警才是正确默认。

**已否决:纯属性判定**("内容非空即输出")——会把 89 个页眉 + 85 个页码注入正文,
"真内容 vs 页面噪声"的区分绕不开 label 语义。

## 3. y0 归并算法(两阶段,不重排 ordered)

**已否决:把无序块硬塞进 ordered 列表重排**——会破坏 `display_formula`/`formula_number` 的相邻吸收逻辑
(依赖 `ordered[i+1]` 定位),且有把双栏页 ordered 内容整体打乱的风险。

采用两阶段:

1. **正常渲染 ordered**:逻辑完全不变(含公式吸收)。每渲染出一个片段,记录该片段**所属首块**的
   bbox y0,得到 `[(y0, fragment), ...]`。此序列的相对顺序就是 `block_order` 本身,**绝不重排**。
2. **单独渲染每个无序 extra**(裁图引用/直通产物),按各自 y0 **稳定排序**(Python `sorted()`,
   相同 y0 按原始列表顺序破 tie);缺 bbox 的 extra 归入"页尾组",按原始列表顺序排在全部有 y0 的 extra 之后。
   然后与第 1 步序列归并。

**归并的权威语义(spec 口径)**:对每个 extra,顺序线性扫描 ordered 片段序列,插在**第一个
y0 > extra.y0 的片段之前**;扫完没有命中 → 追加页尾。不假设 ordered 片段的 y0 单调,**不得用 bisect**。

**实现口径(共享指针归并,O(n+m))**:遍历 ordered 片段,在吐出当前片段前,先吐出 extras 中所有
**y0 严格小于(`<`)** 当前片段 y0 的;循环结束后吐出剩余全部 extras。

**等价性条款**:两种口径等价,**前提是实现用严格 `<` 而非 `≤`**。反例(y0 相等 tie):
extra y0=300、片段 y0 序列 `[100, 300, 500]`——spec 口径把 extra 放在片段 300 **之后**
(300 不满足 >300),`≤` 版共享指针会放在片段 300 **之前**。此反例必须写成单测锁死(§6.2)。

**行为定义**:

- **纯图页(零 ordered 片段)**:归并天然退化为"全部 extra 按 y0 输出"(循环体不执行,收尾吐出全部)。
  注意这是 [convert.py:25](../../../scripts/pipelines/textbooks/convert.py#L25) `if page_md.strip()` 的行为变化——
  以前纯图页贡献空串被跳过,现在会产出图片引用。单测锁死,不靠"顺带覆盖"。
- **非单调 y0(双栏页)**:ordered 内部顺序绝不受影响;extra 的插入点"确定但可能不精确"
  (不知道列信息,右栏 extra 可能插进左栏流)。这是**接受的残余风险**,可观测性由
  `column_layout_suspected`(§5.6)兜住——100 页语料两个独立口径(30px 容差版 / y 重叠比>0.5 版)
  交叉验证均只命中 p2/p6(版权页/出版社页,非正文),但 12.4% 抽样对全书 803 页无推断力,
  书末 index/附录出现双栏是现实可能,错位属安静失败,必须逐页告警而非沉默接受。

## 4. 裁图技术路线:Pillow(像素空间零换算)

**已否决:fitz 把 PNG 当单页文档打开 + clip 裁剪**。实测(2026-07-03,本机 `.venv-textbooks`):
`get_pixmap(dpi=150)` + `pix.save` 产出的 PNG 带 150 dpi 元数据(与
[preprocess.py:29-31](../../../scripts/pipelines/textbooks/preprocess.py#L29-L31) 同路径),`fitz.open` 打开后
`page.rect` 按元数据折算成 point:417×209 px → **200.16×100.32 pt**,即 **1 pixel ≠ 1 point**
(系数 72/150=0.48)。OCR 像素 bbox 直接当 clip 会裁错区域;正确做法需要"像素→point→像素"双重换算,
正确性挂在 PNG DPI 元数据上——元数据一变就安静裁歪,且测试 fixture 必须专门构造带 150dpi 元数据才能复现。
fitz 路线唯一优势"不新增依赖"已不成立:**Pillow 12.1.0 已在 `.venv-textbooks`**(PaddleOCR 栈传递依赖)。

**采用**:`PIL.Image.open(png).crop((x0, y0, x1, y1))`——与 OCR bbox 同一坐标系,零换算,
DPI 元数据无关,逐像素精确。配套动作:**把 Pillow 从传递依赖提升为 requirements 显式声明**(一行),
防上游升级静默丢失。

## 5. 架构落点

### 5.1 新模块 `images.py`

```python
crop_block_images(png_path, blocks, assets_dir, page) -> list[dict]   # 裁图存盘,返回告警列表
crop_filename(page, block_id) -> str                                   # 纯函数,命名唯一事实源
```

- `crop_block_images` **不返回文件名给调用方依赖**——续跑场景下 `reconstruct_markdown` 可能在一次
  完全不裁图的独立进程里运行(读上次落盘的检查点),两边必须**各自导入同一个 `crop_filename`**
  算出一致的名字,而不是靠传参。`block_id` 取 res.json 原生字段(§1)。
- **裁图异常 → 记告警,不判页失败**(文字内容仍有价值,一张图裁失败不掀翻整页)。

### 5.2 `convert.py` 钩子与 assets 生命周期

- 钩子点:`blocks = predict_page(...)` 成功之后、`finally: os.remove(png)` 之前,调用
  `images.crop_block_images(png, blocks, assets_dir, page)`。
- `assets_dir = <doc_out>/<stem>.assets`,与 `<stem>.md` 同级,照搬 `general/typora_layout.py` 约定,
  内部文件名不含空格。
- **指纹失配触发 `reset_work_dir` 时一并清空 `<stem>.assets/`**——它在 doc_out 不在 work_dir,
  现有 reset 逻辑碰不到,不清会变孤儿文件。

### 5.3 补裁循环(续跑缺口,必做)

钩子只覆盖本次运行处理的页;**已完成的页不进 todo 循环,其 PNG 早已删除**。两个真实场景:
(a) 现有 100 页测试 `_work` 是在图片功能之前跑完的,在它上面重跑 convert 一张图都不会裁,
md 却会引用不存在的文件;(b) `res.json 落盘 → 崩溃 → resume` 的窗口同理。

对策:assemble 前对每页核对"该页应产出的裁图文件是否都在盘上"(用 `crop_filename` 推算),
缺失的页执行**补裁**:用 manifest 里的 dpi 重新栅格化该页(`pdf_page_to_png`,convert 手里有
pdf_path 和 dpi,与 OCR 时同 DPI 保证 bbox 对齐)→ 裁图 → 删 PNG。顺手治愈现有 100 页测试目录。

### 5.4 `reconstruct_markdown` 签名变化

```python
reconstruct_markdown(blocks, stem=None, page=None) -> tuple[str, list[dict]]   # (md, warnings)
```

- `stem`/`page` 可选:不涉及图片的调用不用传;但**遇到 `_VISUAL_LABELS` 块且 stem/page 为 None
  → 报错**,失败要响,不能悄悄跳过。
- 返回值改 tuple:**现有全部 ~19 个调用点(tests + convert.py)都要做 `md, _ = ...` 的机械修改**
  ——"可选参数不逼测试改"只对参数成立,对返回类型不成立,如实记录,不假装无成本。
- `assemble()` 改为 `(work_dir, total, stem)`,把 stem 传下去,并逐页汇总 warnings。

### 5.5 告警传递链

reconstruct/images 的告警**作为返回值向上传**(不只 print):
`reconstruct_markdown`/`crop_block_images` → `assemble()` 汇总 → 计入 selfcheck JSON + batch 汇总报告。
stderr 实时输出保留,但 13 小时跑批滚过去就没了,**持久化报告才是"能被看到"的兑现处**。

**告警项统一 schema**(§2 三种告警场景共用一种结构,`reconstruct_markdown` 返回值里的每个元素):

```python
{"kind": "unhandled_label" | "visual_missing_bbox" | "visual_unexpected_content",
 "label": str, "page": int, "block_id": ..., "sample": str}   # sample = 内容前 40 字符,可能为空
```

`kind="unhandled_label"` 对应 §2 场景④(未知非空 label);`visual_missing_bbox`/`visual_unexpected_content`
分别对应 §2 场景①的两个子情形(`_VISUAL_LABELS` 缺 bbox / 意外带非空内容)。`assemble()` 按 `kind` 分流到
§5.6 两个不同字段,不混在一起——`unhandled_labels` 语义上专指"没见过的 label",另两种是"认识的 label
但行为超出预期",分开存放便于跑完 803 页后分别定位是"引擎出了新 label"还是"已知 label 的数据反常"。

### 5.6 selfcheck 扩展(四个新字段)

| 字段 | 内容 | 来源 |
|---|---|---|
| `unhandled_labels` | `{label: {count, sample}}`(sample=首个实例内容前 40 字符) | warnings 里 `kind=unhandled_label` 汇总 |
| `visual_warnings` | `[{kind, page, block_id, sample}, ...]`(缺 bbox / 意外带文本两种) | warnings 里另两种 `kind` 原样列出 |
| `column_layout_suspected` | `[page, ...]`,疑似双栏页,需人工核对图片插入位置 | 启发式逐页跑(下) |
| `missing_assets` | `[filename, ...]`,md 引用但盘上不存在的裁图文件 | assemble 后核对 |

**双栏启发式(产品化,含单测)**:对每页 ordered 的 `text`/`display_formula` 块两两比较,
存在一对满足"y 区间重叠比例 > 0.5(相对较矮块高度)且 x 区间完全分离(一块的 x1 < 另一块的 x0)"
→ 记该页。100 页语料基线:恰好命中 p2/p6。

## 6. 测试策略

1. **images.py**:测试内用 Pillow 现造已知像素内容的小 PNG,裁已知 bbox,断言存盘文件的像素尺寸与内容;
   裁图异常路径(坏 bbox)→ 返回告警不抛出。
2. **归并逻辑**(reconstruct):单栏正常插入 / 非单调(双栏模拟)按 §3 spec 口径精确断言插入位置 /
   **tie 反例(extra 300 vs 片段 [100,300,500],锁死严格 `<`)** / 纯图页退化 / `_VISUAL` 缺 bbox
   → 丢弃+告警 / `_PASSTHROUGH` 空内容 → 静默跳过 / `_VISUAL` 非空内容 → 图+文本都在且有告警 /
   未知 label 非空内容 → 告警进返回值 / 页尾组顺序(缺 bbox extra 按原始顺序垫尾)。
3. **golden(真实语料,非合成)**:p28(单栏,1 text + 1 image + 4 figure_title)断言插入正确性;
   p6(双栏嫌疑页)断言输出**确定性**(锁行为,不锁"正确"——该页本就是接受的不精确场景)。
4. **convert.py**:裁图钩子在 PNG 删除前被调用 / 指纹失配时 `.assets` 被清空 / 补裁循环对缺资产页生效。
5. **selfcheck**:四个新字段各一条正/反例(含 `visual_warnings` 的两种 `kind`);双栏启发式单测。
6. **最终验证**:100 页真实语料 `_work/*_res.json` 离线重跑——预期首轮 `missing_assets` 亮起
   (离线无 PNG,恰好验证该字段本身),补裁后清零;核对 `column_layout_suspected` 只命中 p2/p6;
   `unhandled_labels` 为空。

## 7. 遗留与不涉及

- 双栏页 extra 插入精度:接受的残余风险,由 `column_layout_suspected` 提供人工复核入口;
  803 页全量跑完后按该字段抽查,若命中页显著多于前言范围再评估列感知插入。
- 模块①②遗留:`doc_title` 判据(has_paragraph_title)基于 4 个语料样本,全量跑后人工抽查全部
  doc_title 页;`## {content}` 多行标题断裂、selfcheck 空探针不区分"预期空/识别失败"——
  见交接文档审核记录,不在本设计范围。
