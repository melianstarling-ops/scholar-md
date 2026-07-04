# HANDOFF —— textbooks 公式视觉修复流水线（后处理），交接新会话执行

> 撰写：2026-07-04。承前：本会话已完成"LaTeX 渲染排查 + debug 可视化工具 + 引擎结构错检测"（见 [同日 QA 交接](2026-07-04-HANDOFF-textbooks-latex-render-qa-done.md) 与 commit `2772a98`→`2ec74ba`）。本文档交接**下一步**：把"检测→视觉修复→验证"做成流水线。**方案已与所有者讨论拍板，新会话直接执行，勿推翻已定决策。**

## §0 一句话目标

debug 工具已能**抓出**引擎识别错的公式（渲染红 + 结构疑似）。下一步：对被抓出的公式，**用视觉模型（Claude 的 Read 直接读图 / Kimi）读原图裁切、生成修正 LaTeX**，经人工确认后**以"叠加层"方式**修正，最终把这套做成**管线的后处理阶段**。

## §1 已拍板的决策（勿重议）

1. **修正用"叠加层"(方案 B)，不改 res.json**。res.json 永远是引擎原始输出的铁证（可追溯/回滚/换引擎重比）。修正写进独立的 `<stem>_corrections.json`，在拼 md 时按 (page, block_id) 覆盖对应块。大白话="原稿不动，另贴勘误表，排版照表来"。
2. **修复引擎 = 前沿视觉模型**。本会话实测 p49 六个结构错公式，**Claude(Read 工具直接读图) 与 Kimi 2.7 均 6/6 读对**（见 §3 golden）。Read 工具能直接读 PNG → 修复即本 agent 的视觉，**无需外部 API、不装依赖、数据不外流**。
3. **采纳门槛：初期全部人工确认**（debug 视图三栏并排：原图裁切 + 旧渲染 + 新渲染，人一键采纳/驳回）。跑顺后再对"高置信 + 检测器通过 + 双模型一致"开自动采纳。**别一上来就自动改。**
4. **节奏：先 debug 通，再加回管线**。当前是 debug 阶段，**先不碰原 OCR 内容**；p49 端到端闭环验证成功后，再把后处理阶段（检测→裁图→视觉修→corrections→应用）折进 `convert.py`。
5. **红线不变**：确定性优先、ML 只判断不改字符（视觉修复属"重新识别"不属"确定性改写"，走人工确认门）；`02_Source/` 只读；不改 patents/general/engine；对外操作（装依赖/merge/push）前所有者确认。

## §2 叠加层设计（新会话按此建）

- **文件**：`03_Output/textbooks/<...>/<stem>/<stem>_corrections.json`（产物目录，随 03_Output 一起 gitignore）。
- **结构**（建议）：
  ```json
  {"stem": "...", "corrections": [
    {"page": 49, "block_id": 3, "kind": "frac_primed_denom",
     "engine_latex": "$$ c\\Delta z=\\frac{...}{c^{\\prime}} $$",
     "corrected_latex": "$$ c\\Delta z=\\frac{...\\oint_{c'}...}{V(z,t)} $$",
     "source": "claude-vision", "confidence": "high",
     "content_fingerprint": "<engine block_content 的哈希,防 res.json 漂移误配>",
     "ts": "2026-07-04"}
  ]}
  ```
- **应用点**：`convert.py assemble()` 里、`reconstruct_fragments/markdown` **之前**，对每个块：若 corrections 有匹配 (page, block_id) 且 `content_fingerprint` 与当前 res.json 块内容一致 → 用 `corrected_latex` 替换 `block_content`。然后 reconstruct/sanitize 照常在**修正后**内容上跑。
  - fingerprint 兜底：万一重跑引擎导致 block_id 变了，用内容哈希/前缀匹配防止修正贴错块（宁可不应用也不错配）。
- **优点**：原始可追溯、可回滚、可审计、重跑不丢、来源分层清晰。**缺点**：多一层、要维护块对应（fingerprint 兜底）、最终 md 是合成品（需工具看来源）。

## §3 p49 golden（六个公式的正确 LaTeX，已由 Claude+Kimi 双验）

p49 是第一个端到端目标，也是回归 golden。块 id → 编号 → 正确 LaTeX（引擎那版全部把围道/曲面当分母、丢真分母，见 §4 QA 交接）：

| block_id | 编号 | 正确 LaTeX（$$ 包裹） |
|---|---|---|
| 3 | 1.56a | `c\Delta z = \frac{\Delta z\,\varepsilon\oint_{c'}\vec{\mathcal{E}}_{\mathrm{t}}\cdot\vec{a}_{\mathrm{n}}'\,dl'}{V(z,t)}` |
| 6 | 1.56b | `c = \frac{\varepsilon\oint_{c'}\vec{\mathcal{E}}_{\mathrm{t}}\cdot\vec{a}_{\mathrm{n}}'\,dl'}{V(z,t)}` |
| 9 | 1.57 | `c = \varepsilon\,\frac{\oint_{c'}\vec{\mathcal{E}}_{\mathrm{t}}\cdot\vec{a}_{\mathrm{n}}'\,dl'}{-\int_{c}\vec{\mathcal{E}}_{\mathrm{t}}\cdot d\vec{l}}` |
| 12 | 1.58 | `g\Delta z = \frac{\sigma\oint_{s'}\vec{\mathcal{E}}_{\mathrm{t}}\cdot\vec{a}_{\mathrm{n}}'\,ds'}{V(z,t)}` |
| 15 | 1.59a | `g\Delta z = \frac{\Delta z\,\sigma\oint_{c'}\vec{\mathcal{E}}_{\mathrm{t}}\cdot\vec{a}_{\mathrm{n}}'\,dl'}{V(z,t)}` |
| 18 | 1.59b | `g = \frac{\sigma\oint_{c'}\vec{\mathcal{E}}_{\mathrm{t}}\cdot\vec{a}_{\mathrm{n}}'\,dl'}{V(z,t)}` |

注意 1.58 是**曲面 s'**（`\oint_{s'}…ds'`），其余是**围道 c'**（`\oint_{c'}…dl'`），别homogenize。Kimi 完整识别结果（含正文）：`03_Output/textbooks/Paul_Analysis_MTL_scan_page49_visual/Paul_Analysis_MTL_scan_page49_visual.md`。本会话裁图：scratchpad `p49crops/eq_*.png`（会随会话清理，用 §5 脚本重裁）。

## §4 建议的建造顺序

**Phase A —— 在 p49 上跑通端到端闭环（先做，证明可行）**
1. **裁图工具**：新增 `scripts/pipelines/textbooks/debug_repair.py`——输入 doc 目录，读 selfcheck/payload 的 suspicions，把每个被标记的公式块按 `block_bbox` 高 DPI（如 300）裁成 PNG，落 `<stem>_repair/`；同时导出待修工作单 JSON（page, block_id, bbox, engine_latex, crop 路径）。裁图逻辑可参考本会话 scratchpad `crop_p49.py`（§5）。TDD。
2. **视觉修复**：agent 逐个 Read crop → 写 corrected_latex + confidence → `<stem>_corrections.json`。p49 可直接用 §3 golden 播种，验证闭环。
3. **叠加层应用**：按 §2 在 assemble/reconstruct 前应用 corrections，重生成 md。TDD（含 fingerprint 防漂移、缺失/不匹配时不应用）。
4. **前后验证**：对修正后重跑 `scan_katex_errors.mjs` + `scan_formula_suspicions` → p49 疑似应清零、能渲染；debug 视图三栏对比。

**Phase B —— debug 视图加"修复/验证"模式**
- 标记公式点开 → 原图裁切 + 旧 LaTeX 渲染 + 新 LaTeX 渲染三栏并排；人一键采纳/驳回 → 写 corrections.json（serve 直写 / 静态下载 + collect）。复用现有 debug_view/app.js 的 serve/collect/气泡骨架。

**Phase C —— 折进管线做后处理阶段（debug 通过后）**
- 把"检测→裁图→视觉修→corrections→应用"作为 `convert.py` 的**可选后处理阶段**（OCR 之后、最终 md 组装时应用 corrections）。门控：默认人工确认；成熟后对高置信自动采纳。这一步**等 Phase A/B 验证成功再做**。

## §5 复现裁图（scratchpad 脚本已随会话，附核心逻辑）

```python
import fitz, json, io
from PIL import Image
PDF='<源 PDF>'; res=json.load(open('_work/page_0049_res.json',encoding='utf-8'))
want={3:'1.56a',6:'1.56b',9:'1.57',12:'1.58',15:'1.59a',18:'1.59b'}
blocks={b['block_id']:b for b in res['parsing_res_list']}
scale=300/150  # bbox 在 150DPI 空间;裁图渲 300DPI
img=Image.open(io.BytesIO(fitz.open(PDF)[48].get_pixmap(dpi=300).tobytes('png')))
for bid,tag in want.items():
    x0,y0,x1,y1=blocks[bid]['block_bbox']
    img.crop((int(x0*scale)-10,int(y0*scale)-10,int(x1*scale)+10,int(y1*scale)+10)).save(f'eq_{tag}_id{bid}.png')
```
⚠ 源 PDF 只以上会话 scratchpad 临时文件存在（`67cdc87b-.../Paul_p1-100_scan.pdf`，现存但随时可能被清）；若丢失需从原书重新切片或让所有者提供。

## §6 当前状态 / 已提交 / 怎么跑

- **检测器已就绪**：`selfcheck.scan_formula_suspicions`（bare_op + frac_primed_denom）、`summarize_suspicions`（落 selfcheck.json `formula_suspicions`）。debug 视图已标出（琥珀 mdblk/box + 徽章 + 问题索引 + 缩略图 ◆ + E 筛选）。
- **全书疑似**：`\oint×13, frac÷c'×7, \int×6, \lim×1, frac÷s'×1`（28 处，聚在 44/46/48/49/50/53/54 共 7 页）。
- **产物**：`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/Paul_p1-100_scan_debug.html`（20.6MB，双击浏览器打开）、`_render_errors.json`、重生成的 `.md`（均 gitignore）。
- **测试**：`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/ -q`（184 绿）。headless 扫描：`node scripts/pipelines/textbooks/debug_assets/scan_katex_errors.mjs --md <md> --out <json>`。jsdom 冒烟见 scratchpad `smoke_jsdom.mjs`（注意 `url:'http://localhost/'`）。
- **本会话 commits（分支 feature/textbooks-engine）**：`6d9ff8d`(UI 复刻) `4fe8aee`(双向联动) `7c75d83`(疑似漏识别) `2ec74ba`(结构错检测) + 本文档/lessons/TODO。
- **lessons**：`04_Docs/lessons/lessons_textbooks_dev.md` L-T23~L-T27（本会话踩坑）。

## §7 给新会话的第一步

按 §4 Phase A 起步：先写 `debug_repair.py` 裁图 + 工作单导出（TDD），再用 §3 golden 播种 corrections、建叠加层应用、跑 p49 前后验证闭环。跑通后回报所有者，再推进 Phase B/C。**始终：res.json 不动、人工确认门、先 debug 后入管线。**
