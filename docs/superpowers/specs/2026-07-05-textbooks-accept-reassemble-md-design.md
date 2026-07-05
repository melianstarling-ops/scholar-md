# 设计 —— textbooks 公式修正:采纳后落进 .md(翻页时重跑 assemble)

> 撰写:2026-07-05。承前:[2026-07-05 视觉修复审核闭环交接](../../handoff/2026-07-05-HANDOFF-textbooks-vision-repair-review-loop-done.md)。
> 本 spec 只解决一个 bug:debug 审核里点"采纳"后,修正不会进入最终生成的 .md。

## 1. 背景与问题

公式视觉修复的"应用"半条腿已在转换管线里:`convert.py::assemble()` 读检查点 blocks →
`apply_corrections()`(只应用 `status=="accepted"`)→ `reconstruct_markdown()` → 拼出 md,
`convert_pdf()` 再把 md 写到 `doc_dir/stem.md`。

但**采纳动作发生在另一个入口、另一个进程**:debug 审核脚本 `debug_view.py::serve()` 的
采纳/驳回按钮 → `POST /corrections` → `handle_post` → `set_correction_status`,**只改
`<stem>_corrections.json` 里那条记录的 status,改完就停,从不触发 .md 重新生成**。

两个入口互不触发 = 缺口:点采纳只动了 json 的状态位,.md 不会更新,要等下一次完整
`convert_pdf` 才落盘。

**现存实证**(`03_Output/textbooks/_realrun_100page_test/Paul_p1-100_scan/`):
`corrections.json` 13 条全部 accepted,但 `.md`(生成于该 json 被改前)只含前 9 条的
修正;后 4 条(50/3、53/20、54/6、54/15)是"已采纳但未落盘"。逐条比对确认 4 条 MISSING。

## 2. 目标 / 非目标

**目标**:在 debug 审核过程中采纳修正后,该修正能进入最终的 `doc_dir/stem.md`,
无需手动重跑整条 `convert_pdf`。

**非目标**:
- 不改视觉调用/打包逻辑(`vision_repair.py` 不动,见 §7)。
- 不改正式转换管线 `convert_pdf` 现有流程(取舍 B)。
- 不同步刷新 `stem_selfcheck.json`(取舍 A)。
- 人工确认门红线不变:仍然只有 `status=="accepted"` 才落进 md。

## 3. 方案:闭环数据流

```
点采纳/驳回
  → POST /corrections(已有)→ set_correction_status 写 json.status
  → 后端置 dirty = True                         [新增]
(继续审本页其它条,可多次改判,都只写 json + 置脏)
  → 翻页 gotoIndex
  → 前端 POST /reassemble                         [新增:翻页就 ping]
  → 后端:dirty 才真跑,否则秒回                   [dirty 门控]
      → reassemble_md():assemble() 读所有页 blocks
        → apply_corrections 只应用 accepted → reconstruct → 拼 md
      → 写 doc_dir/stem.md
      → dirty = False
```

**两个机制要点:**

1. **前端翻页无脑 ping `/reassemble`,"跑不跑"由后端 dirty 决定。** 前端逻辑最简
   (翻页发一个请求),后端保证没有新采纳就不空跑整本 assemble;无改动翻页 = 秒回、零成本。
2. **`/reassemble` 是后台异步 fetch,不阻塞翻页 UI。** 界面立即切换,md 后台重生成;
   assemble 在 assets 已齐时只做 blocks→md 重组(不重 OCR、不重栅格化),很快。

复用的是转换管线里**唯一的** `assemble()`,所以 debug 采纳出的 .md 与将来正式 convert
出的 .md 逐字一致,不会分叉。

## 4. 组件改动(3 个文件)

### 4.1 `convert.py` — 新增 `reassemble_md(doc_dir, pdf_path, dpi) -> str`

薄函数:从 `doc_dir` 推出 `work_dir`(=`doc_dir/_work`)、`stem`(=basename)、
`assets_dir`(=`doc_dir/stem + ".assets"`),`cp.load_manifest(work_dir)` 拿 `total`
(page_count) → 调既有 `assemble(work_dir, total, stem, assets_dir, pdf_path, dpi)`
→ 把 `result["md"]` 写到 `doc_dir/stem.md` → 返回 md_path。

- 只写 md,**不写 selfcheck、不动 manifest**(取舍 A)。
- `convert_pdf` 现有那段"assemble→写md"**不改**(取舍 B);新函数独立存在。
- manifest 缺失 / total 为 0 时:安全返回(不写、不抛),视同"没东西可重组"。

### 4.2 `debug_view.py` — `/reassemble` 路由 + dirty 门控

- `handle_post` 增加两个显式入参:`state`(可变 dict,含 `dirty`)与 `reassemble_fn`
  (可注入回调,默认绑定 `convert.reassemble_md`),保持"纯函数 + 显式状态"便于单测。
- `handle_post` 逻辑:
  - `path == "/corrections"` 且 `set_correction_status` 成功 → `state["dirty"] = True`。
  - `path == "/reassemble"` → `state["dirty"]` 为真才调 `reassemble_fn`、清 `state["dirty"]`;
    否则直接 200 秒回(不重跑)。
- `serve()` 创建 `state = {"dirty": False}` 由闭包持有,连同 `pdf_path`/`dpi`
  (已通过 `_resolve_pdf` 持有)传入 `handle_post`。dirty 行为因此落在纯函数上、可单测。

### 4.3 `debug_assets/app.js` — 翻页时触发

- `gotoIndex`(翻页/跳页)时 `fetch('/reassemble', {method:'POST'})`,
  fire-and-forget(失败不打断审核;可选一个不打扰的角标提示"已同步到 md")。
- 采纳/驳回按钮本身逻辑不变(仍走 `/corrections`),不需要前端自己判断脏。

## 5. 取舍决策(已确认)

| 取舍 | 决定 | 理由 |
|---|---|---|
| A | 只更新 .md,不同步 selfcheck.json | selfcheck 的 suspicions/katex 暂时陈旧无碍:debug UI 的 R/E 筛选靠 render_errors,不依赖 selfcheck;留待下次正式 convert 刷新 |
| B | 不收敛复用、convert_pdf 不改 | 本次不牵动正式管线,降低连带风险;新函数独立 |
| 触发时机 | 翻页/跳页时统一重跑 | 对齐现有 gotoIndex 刷新点,一页多处改完一次落;审核 UI 不显示 md,即时性无可见收益 |
| 门控 | 后端 dirty 标记 | 无新采纳的翻页不空跑整本 assemble |

## 6. 一次性回填

机制上线并测试通过后,对现存 `Paul_p1-100_scan` 手动补跑一次 `reassemble_md`,
把 4 条"已采纳未落盘"(50/3、53/20、54/6、54/15)补进现有 `.md`,让 json 与 md 对齐。
这是数据回填,不是代码,单独一步执行。

## 7. 关联 follow-up(不在本 spec 代码范围)

交接文档 §1.2/§3.4 的"已定论:不做任何形式的图片打包/别重新实验"措辞有误,需订正
(纯文档,不改代码):

- **L-T31 合成图打包**(N 图拼成一张 → 一次 Read,分辨率被压):实测 10/10 下标读错,
  "别用"成立。
- **L-T30 路径列表打包**(N 个独立文件、原分辨率、模型各自 Read):只测过更慢更省钱,
  **未测过正确性**,无降质证据。当前 `call_claude_vision_batch` 走的正是这种,
  真实跑 13/13 采纳是其不降质的正面证据。

订正方向:把"禁一切打包"收窄为"仅禁 L-T31 合成图打包",并把两条 lessons 措辞分清。
`vision_repair.py` 代码**不改**。是否本次顺手改文档,待所有者决定。

## 8. 测试计划(TDD 先行)

**`test_convert.py`**:
- `reassemble_md` 把 accepted 修正落进 md;pending/rejected 不落。
- 无 `corrections.json` 时正常产出(与不存在这一层行为一致)。
- manifest 缺失 / total=0 时安全返回,不抛。

**`test_debug_view.py`**:
- `handle_post` 收到 `/reassemble` 触发注入的 `reassemble_fn`。
- `/corrections` 成功后 dirty 置真;`/reassemble` 后 dirty 清零;无变更的 `/reassemble` 不触发重跑。

全套 `python -m pytest scripts/pipelines/textbooks/tests/` 保持全绿。
