# HANDOFF —— textbooks 100 页真实端到端实测 + 转换质量问题排查,交接给新会话修复

> 撰写:2026-07-03。走 git(跨会话/跨机可见)。读完本文即可在新会话直接动手修复,无需重新调研。
> 承前:[大文件稳健化完成交接](2026-07-02-HANDOFF-textbooks-large-file-done.md)。

## §0 当前状态(一句话)

100 页真实 GPU 端到端测试**跑通**(大文件稳健化机制验证有效),但顺带发现 **`reconstruct.py` 有系统性内容遗漏 bug**(参考文献、摘要、目录、算法块整类型丢失)+ **图片完全没有输出**(架构缺口,从未实现)。本文档把两个问题的精确定位、根因、修复点、可参考的历史代码都交接清楚,**下一个会话可以直接开始修,不需要重新调研**。

## §1 100 页真实 GPU 端到端测试结果

- 机器:**家用机(RTX 4060 / 8GB)**。样本:`Paul_Analysis_MTL_scan.pdf`(803页原书)切前 100 页,存于本机 scratchpad,若需复测可重新用 PyMuPDF 切(`fitz.open(src); out.insert_pdf(doc, from_page=0, to_page=99)`)。
- 命令:`watchdog.py --src <100页PDF> --out 03_Output/textbooks/_realrun_100page_test --dpi 150`。
- 结果:**100/100 页完成,`failed_pages=0`,`restarts=0`**(全程零崩溃,看门狗续跑/坏页隔离机制**未被真实触发**,这两个机制目前仍只有 mock 测试覆盖,没有真实崩溃场景验证过)。
- 耗时:1:17:43,平均 **46.6s/页**(落在既有 45~78s/页区间)。`_work` 目录终态 PNG 残留=0(磁盘有界设计验证通过)。
- 产物:`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan.md`(158KB)+ `_selfcheck.json`(Tier0 覆盖 831/875 块,缺 44 —— 见 §2)。
- 显存:该进程独占显卡,`nvidia-smi` 显示专用显存 7.9/8.19GB(接近打满)。另外用 `Get-Counter '\GPU Process Memory(*)\Shared Usage'` 查到该进程还占用约 **7.68GB "共享显存"**(远高于其他任何进程)。**这条不阻塞当前工作**,详情见 §4,仅供好奇/未来深挖参考。

## §2 reconstruct.py 系统性内容遗漏(本次最重要的发现)

### 根因

`scripts/pipelines/textbooks/reconstruct.py` 第 42-77 行 `reconstruct_markdown()` 的 if/elif 分支**只处理 4 种 block_label**:`paragraph_title`/`text`/`display_formula`/`formula_number`。但这 100 页语料里 PaddleOCR-VL 实际产出了 **18 种 label**(text 370、display_formula 227、header 89、figure_title 89、number 85、image 54、paragraph_title 27、reference_content 24、chart 12、content 10、doc_title 4、header_image 2、footnote 2、algorithm 2、abstract 2、table 1、seal 1)。**没有 else 兜底分支**——任何未被显式处理的 label,即使 `block_order` 不是 `None`(即该出现在正文里),也会在循环里被 `i += 1` 静默跳过,不落地、不告警。

### 精确清单(44 条 selfcheck missing 逐条核实)

已用脚本精确复现(直接读 100 个 `page_XXXX_res.json`,按 assemble() 同样页序拼接、同样 probe 算法比对),完整机器可读版 + 复现脚本已存入本仓库:
- `docs/handoff/2026-07-03-textbooks-quality-evidence/missing_map.json`(44 条逐条明细:页码、block_id、label、原始 content 全文不截断、判定)
- `docs/handoff/2026-07-03-textbooks-quality-evidence/map_missing.py`(复现脚本,可直接在新会话重跑验证)

汇总:

| 分组 | 条数 | 页码 | label | 判定 |
|---|---|---|---|---|
| A | 6 | p2/p3/p6/p7 各若干 | text/seal,`block_content` 为空 | **selfcheck 假阳性**——`_probe("")` 恒为空字符串,falsy 导致必然误判 missing,与内容无关。reconstruct.py 本就正确跳过空块。 |
| B | 2 | p1(id1)、p23(id1) | doc_title | **真实遗漏**。p1 是封面信息;p23 是被 OCR 误标成 doc_title 的第1章标题(其余几十个"INTRODUCTION"是 `header`+`order=None`,已被正常过滤,不算遗漏)。 |
| C | 1 | p8(id1) | abstract | **真实遗漏**,全文约300词简介。 |
| D | 9 | p13/15/16/17/18/19/20/21/22 | content | **真实遗漏**,前言/目录页码列表整段丢失。 |
| E | 2 | p80(id5)、p81(id3) | algorithm | **真实遗漏**,SPICE 电路网表示例(`EXAMPLE\nVS 1 0 PULSE(...)...`)。 |
| F | 24 | p91(id8-19)、p92(id2-13) | reference_content | **真实遗漏**,参考文献 `[1]`~`[24]` 整段丢失,一条不剩。 |

**修复点**:
1. `selfcheck.py` 第 14-17 行 `_probe()`:探针为空时应直接 `continue`,不计入 missing(1 行小改)。
2. `reconstruct.py` 第 42-77 行 `reconstruct_markdown()`:补 `reference_content`/`content`/`abstract`/`algorithm` 的处理分支(参考文献建议保留编号原样输出;content/abstract 按段落文本处理;algorithm 建议用代码块 ` ``` ` 包裹,保留网表格式);**必须加一个兜底 `else` 分支**(至少 `parts.append(content)`),防止未来 PaddleOCR-VL 版本升级出现新 label 时重演同样的静默丢失。`doc_title` 需要甄别"封面元信息"与"被误标的正文标题",不能一刀切纳入正文(建议:只在页码较小、无对应 paragraph_title 兄弟块时按封面处理;否则当章节标题处理,可能需要人工规则或退化为按 paragraph_title 处理)。

## §3 图片完全没有输出(架构缺口,不是本次遗留 bug,是从未实现)

### 现象

这次 100 页测试产物只有 `.md` 文本,**没有任何图片文件**,也没有 `.assets/` 图片资源目录。所有者确认这不是预期效果——需要图片和文档都要有。

### 根因(两层问题叠加)

1. **设计文档早就规划了图片资源目录,但从未实现**:`docs/superpowers/specs/2026-07-01-textbooks-pipeline-design.md` 第113行明确写"沿用 general 的 Typora 结构:`<doc_id>.md` + `<doc_id>.assets/`(图片资源)",但 `reconstruct.py` 里从来没有写过图片裁剪落盘的代码。
2. **`image` 类型的块在到达"没写处理逻辑"这一步之前,就已经被另一条规则误杀**:核实了 `page_0002_res.json`,`image` 类型块**确实带 bbox 坐标**(如 `"block_bbox": [12, 1658, 251, 1732]`,技术上可以从对应页面 PNG 裁出来),但这些 image 块的 **`block_order` 也是 `null`**——而 `reconstruct.py` 第 44-47 行"剔除页眉页脚"的过滤规则是"`block_order is None` 就整体丢弃",image 块被这条规则连坐误杀,根本轮不到 label 分支判断这一步。**需要先确认:是不是所有 image 块的 order 都是 None(如果有真实排版位置的插图 order 不是 None,那问题只在没处理逻辑;如果确实都是 None,则需要把"image 特殊处理"从"剔除页眉页脚"的判断逻辑里摘出来单独走一条路径)。**这一点本次只核实了 p2 的 4 个 image 块,未扩展到全部 100 页,新会话需要先扩大样本确认这个规律是否普遍。

### 可参考的历史实现(旧项目 Project_MD_Book)

路径:`D:\Projects\Project_Archive\Project_MD_Book`(已归档,独立 git 仓库,不要往里面写东西,只读参考)。

- `02_Scripts/pipelines/books/pipeline.py`:有一套现成的图片资产组织逻辑——`copy_image_assets()`(把图片文件拷到目标 `assets/<label>/` 目录)、`rewrite_image_links()`(用正则改写 md 里的 `![alt](target)` 链接指向新路径)。**这套"组织/落盘/改链接"的模式思路可以直接借鉴**。
- **但注意工具链不同,不能照搬裁剪逻辑**:旧项目用的是 **Marker**(该项目自带独立 Marker 环境,见 [[scholar-md-succeeds-mdbook]] 记忆),Marker 自己就会在转换时把 PDF 嵌入的图片提取出来,`pipeline.py` 只负责"搬运整理"这些已提取好的图片文件。现在 textbooks 用的 **PaddleOCR-VL 不做图片提取**,只给 bbox 坐标——所以"从页面 PNG 按 bbox 裁剪出图片文件"这一步**需要新写**,不能复用旧代码,只能借鉴目录组织/链接改写这部分思路。
- 本仓库内 `scripts/pipelines/general/typora_layout.py` 也有同一套 Typora `.assets/` 惯例的实现(assets 目录名 `<name>.assets`,空格转 `%20`),是当前项目里更贴近的参照(同一套约定,同一个仓库,拿来主义成本更低)。
- 本仓库内 `scripts/pipelines/patents/figures.py` 存在(patents 管线处理图片的脚本),**未细读,新会话应该先看一眼这个文件**,因为它可能已经解决了"从图像里裁 bbox 区域存文件"这个具体问题(即使不是同一个 OCR 引擎,裁剪本身是通用图像操作,值得复用)。

### 建议实现方向(未拍板,供新会话参考)

1. 先扩大样本(不只 p2)确认 image 类块的 order 规律,决定是否需要把 image 特殊处理从"order=None 过滤"逻辑里摘出来。
2. 用 PyMuPDF 或 Pillow,依据 `block_bbox` 从 preprocess 阶段生成的页面 PNG(注意:convert.py 当前设计是"栅格化→predict→立即删 PNG",磁盘有界;如果要裁图,要么在删除前顺手裁剪,要么调整时序)裁出图片,存到 `<stem>.assets/page_{N}_block_{id}.png` 或类似命名。
3. reconstruct.py 遇到 image/figure/chart/table 类 label 时,插入 `![](相对路径)` 引用。
4. 参考 general/typora_layout.py 的路径约定与空格编码规则,保持全仓库风格一致。

## §4 GPU 显存"共享内存"疑点(次要,不阻塞,仅供参考)

已排查,**结论不确定,证据不完全支持"良性"这个初步猜测,不建议现在深挖**:

- 该进程专用显存 7.9/8.19GB(几乎打满)+ Windows 计数器另记该进程占用约 7.68GB"共享显存"(远高于其他进程的几十MB量级)。
- 一种解释是 PaddlePaddle 默认会预留"系统内存的50%"作为 pinned memory 池(`FLAGS_fraction_of_cuda_pinned_memory_to_use` 默认 0.5),Windows WDDM 把这类锁页内存也计入"共享显存"统计(已知有失真的计数器,微软有对应 KB 说明),不代表真的发生了性能拖累的显存换页。
- **但核实了这台机器实际物理内存是 31.93GB,不是 16GB**,如果套用"50%"这个假设,应该看到约16GB 共享显存,而不是实测的 7.68GB——**当初支持"良性"结论的关键数字佐证是错的**,所以这个解释目前只能算"机制上合理但数字对不上"的存疑状态,置信度应该下调。
- 如果新会话想彻底搞清楚:可以试着临时设环境变量 `FLAGS_fraction_of_cuda_pinned_memory_to_use=0.05` 重跑一次小样本(比如10页),看这个共享显存数字是否等比例下降——如果下降,坐实是 pinned pool;如果没变化,说明另有原因(真实溢出换页的可能性上升)。**这不是当前的优先级**,只有在怀疑显存问题拖慢了转换速度、需要精确定位时才值得做。

## §5 建议的修复优先级(未拍板,供新会话参考)

1. **selfcheck.py 空探针 bug**(§2 修复点1):最小改动,顺手就改。
2. **reconstruct.py label 覆盖不全**(§2 修复点2):这是本次发现的核心问题,直接影响转换准确性,建议优先修——particularly 加兜底 else 分支,防止未来再犯。
3. **图片输出**(§3):架构级新功能,工作量比 1/2 大,建议走 brainstorming → 写计划 → subagent 开发 这套流程,不要直接改。
4. 修完 1/2/3 后,重新跑一次 100 页(或更小样本)验证,确认 selfcheck 覆盖率提升、md 里能看到参考文献/摘要/图片。
5. 再往后按原计划推进 300→800 页分档实测(见 [大文件稳健化交接](2026-07-02-HANDOFF-textbooks-large-file-done.md) §3)。
6. §4 的显存疑点视精力决定是否深挖,不阻塞主线。

## §6 恢复方法 / 红线(继承)

- 进度记录:`TODO.md` 2026-07-03 节。
- 红线:不改 patents/general/engine;确定性优先、ML 只判断不改字符;`02_Source/` 只读;每管线独立 venv;对外操作(merge/push/装大依赖)前所有者确认。
- 本次 100 页测试产物(含 md/selfcheck/manifest)保留在 `03_Output/textbooks/_realrun_100page_test/`,可直接用于对照验证修复效果,不需要重新跑一遍 100 页才能开始改代码。
