# 设计 —— textbooks 公式修正:采纳后落进 .md(翻页/完成/启动时重跑 assemble)

> 撰写:2026-07-05。承前:[2026-07-05 视觉修复审核闭环交接](../../handoff/2026-07-05-HANDOFF-textbooks-vision-repair-review-loop-done.md)。
> 本 spec 只解决一个 bug:debug 审核里点"采纳"后,修正不会进入最终生成的 .md。
> v2:纳入一轮 spec review 结论(线程安全、页尾兜底、启动对账、签名/limitation)。

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
无需手动重跑整条 `convert_pdf`;且这条"落盘"在**页尾、并发、已分叉文档**等边界上不漏。

**非目标**:
- 不改视觉调用/打包逻辑(`vision_repair.py` 不动,见 §9)。
- 不改正式转换管线 `convert_pdf` 现有流程(取舍 B)。
- 不同步刷新 `stem_selfcheck.json`(取舍 A;陈旧作为已知限制,见 §7)。
- 人工确认门红线不变:仍然只有 `status=="accepted"` 才落进 md。

## 3. 方案:落盘是幂等对账,多点触发

核心动作 `reassemble_md`:读所有页 blocks → `apply_corrections` 只应用 accepted →
`reconstruct` → 拼 md → **覆盖写** `doc_dir/stem.md`。它是**幂等**的:相同 blocks +
相同 corrections 产出逐字相同的 md,覆盖写无副作用。因此可以在多个点安全重复触发:

```
[启动] serve 开始服务前,先跑一次 reassemble           → 开局把 md 对齐到 json 当前状态
[采纳] POST /corrections → set_correction_status 写 json.status → dirty=True(锁内)
[翻页] gotoIndex → POST /reassemble → dirty 才跑、跑完清 dirty(整段锁内)
[完成] "同步到 md"按钮/快捷键 → POST /reassemble(同上)  → 页尾不翻页也能落
[无UI] CLI 子命令 debug_view --reassemble               → SOP 收尾 / §6 回填入口
```

**关键性质:**

1. **`reassemble_md` 幂等 → 落盘正确性不依赖 dirty。** `dirty` 只是**性能门控**
   (没有新采纳的翻页不空跑整本 assemble),不是正确性的一部分;即使 dirty 判断偏保守
   多跑一次,产出也一致。
2. **落盘有多个触发点,互为安全网。** 启动对账治"已分叉文档打开即同步";翻页是常规;
   "完成"按钮/CLI 治页尾;任一漏掉,下次任一触发点都会补齐(幂等)。
3. **`/reassemble` 是后台异步 fetch,不阻塞翻页 UI。** assemble 在 assets 已齐时只做
   blocks→md 重组(不重 OCR;栅格化仅在资产缺失时发生,见 §7),很快。

复用的是转换管线里**唯一的** `assemble()`,所以 debug 采纳出的 .md 与将来正式 convert
出的 .md 逐字一致,不会分叉。

## 4. 组件改动

### 4.1 `convert.py` — 新增 `reassemble_md(doc_dir, pdf_path, dpi) -> str | None`

薄函数:从 `doc_dir` 推出 `work_dir`(=`doc_dir/_work`)、`stem`(=basename)、
`assets_dir`(=`doc_dir/stem + ".assets"`),`cp.load_manifest(work_dir)` 拿 `total`
(page_count) → 调既有 `assemble(work_dir, total, stem, assets_dir, pdf_path, dpi)`
→ 把 `result["md"]` 覆盖写到 `doc_dir/stem.md` → 返回 md_path。

- 只写 md,**不写 selfcheck、不动 manifest**(取舍 A)。
- `convert_pdf` 现有那段"assemble→写md"**不改**(取舍 B);新函数独立存在。
- manifest 缺失 / total 为 0 时:安全返回 `None`(不写、不抛),视同"没东西可重组"。
- 幂等:同输入覆盖写同内容,可被启动/翻页/完成/CLI 反复调用而无副作用。

### 4.2 `debug_view.py` — /reassemble 路由 + 锁 + 启动对账 + CLI

**handle_post(纯函数,显式状态):** 增加两个入参 `state`(可变 dict,含 `dirty`)与
`reassemble_fn`(可注入,默认绑定 `convert.reassemble_md`),保持可单测:
- `path == "/corrections"` 且 `set_correction_status` 成功 → `state["dirty"] = True`。
- `path == "/reassemble"` → `state["dirty"]` 为真才调 `reassemble_fn`、清 `state["dirty"]`;
  否则直接 200 秒回(不重跑)。

**serve(并发安全 + 启动对账):**
- 服务前先跑一次 `reassemble_md`(单线程,无需锁)→ 开局对账。之后 `dirty` 初始 `False`。
  启动对账用 try/except 包裹,失败只告警、不阻断服务(审核仍可进行,后续触发点会补)。
- 建一把 `threading.Lock`(闭包持有)。`do_POST` 中 `/corrections` 与 `/reassemble`
  两个分支都在 `with lock:` 内调用 `handle_post`——把"置脏"与"check→跑→清脏"整段串行化。
  `do_GET` 不加锁(它只读 res.json 渲染 HTML,不写 md)。
- **为什么必须锁**:`ThreadingHTTPServer`(debug_view.py:213)下每个 POST 独立线程,
  `dirty` 是跨线程共享可变、`reassemble_md` 写同一 `stem.md`;无锁时快速翻页会让两个
  `/reassemble` 线程都读到 dirty=True、并发写同一文件(写花/PermissionError),或发生
  丢更新。GIL 不保护"读 dirty→跑长 IO→写文件→清 dirty"这个复合序列。
- 需要的 `pdf_path`/`dpi`:`serve()` 已通过 `_resolve_pdf` 持有,传入。

**CLI:** `main()` 增加 `--reassemble`:`python -m ...debug_view --doc <stem-dir> --reassemble`
→ `_resolve_pdf` 拿 pdf/dpi → 调 `reassemble_md` 写 md → 打印结果。无 UI 收尾与 §6 回填共用此入口。

### 4.3 `debug_assets/app.js` — 翻页触发 + 完成按钮

- `gotoIndex`(翻页/跳页)时 `fetch('/reassemble', {method:'POST'})`,fire-and-forget
  (失败不打断审核;可选一个不打扰的角标"已同步到 md")。
- **新增"同步到 md"按钮 + 快捷键**:页尾采纳后不翻页也能手动 ping `/reassemble`,
  根治"最后一页采纳不落盘"。采纳/驳回按钮本身逻辑不变(仍走 `/corrections`)。
- 不采用 `beforeunload`:普通 fetch 卸载时不保证送达,`sendBeacon` 又多一层前端复杂度/
  不确定性;显式按钮更可靠、也贴合键盘单步操作习惯。

## 5. 取舍决策(已确认)

| 取舍 | 决定 | 理由 |
|---|---|---|
| A | 只更新 .md,不同步 selfcheck.json | selfcheck 暂陈旧无碍:debug UI 的 R/E 筛选靠 render_errors、不依赖 selfcheck;陈旧作为已知限制(§7),留待正式 convert 刷新 |
| B | 不收敛复用、convert_pdf 不改 | 本次不牵动正式管线,降低连带风险;新函数独立 |
| 触发时机 | 启动 + 翻页 + 完成按钮 + CLI 多点 | 幂等对账可多点重复触发,互为安全网;覆盖已分叉打开、常规、页尾、无 UI 四种边界 |
| 门控 | 后端 dirty 标记(锁内) | 无新采纳的翻页不空跑整本 assemble;只是性能优化,不影响正确性 |
| 并发 | serve 层一把 threading.Lock | 串行化置脏与 check-run-clear,杜绝并发写同一 .md 的竞态 |
| 页尾兜底 | 完成按钮/快捷键 + CLI | 显式、可靠;不走 beforeunload/sendBeacon |

## 6. 一次性回填(现存 Paul)

启动即对账(#3)已自动覆盖此需求:用 `--serve` 打开 `Paul_p1-100_scan` 时会先跑一次
reassemble,把 4 条"已采纳未落盘"(50/3、53/20、54/6、54/15)补进现有 `.md`。
无需起 UI 时,`debug_view --doc <Paul-dir> --reassemble` 是等价入口。属数据回填,单独执行。

## 7. 已知限制

- **selfcheck.json 陈旧无标记**:采纳后 md 公式变了,但 `block_coverage`/
  `formula_suspicions`/`katex_incompat` 不随之更新、且不写脏标记。事后直接读
  `selfcheck.json` 的人(如 batch 汇总)会拿到旧数据而无提示。缓解:下次正式 `convert_pdf`
  会重算。这是取舍 A 的代价,显式记录在此,不在本次修。
- **并发正确性靠锁、单测不覆盖**:§8 的单元测试是单线程,测不出竞态;锁是设计层保证,
  评审与实现时按 §4.2 的锁策略核对,勿以"单测全绿"当作并发无 bug。
- **dirty 语义 = 本次启动后有改动**:非"md 与 json 不一致"。因有启动即对账兜底,打开
  审核界面即完成一次对账,该语义的反直觉性已被覆盖;但纯粹进程内不会"感知"外部对 json
  的改动(除非重启或经 UI 采纳)。

## 8. 测试计划(TDD 先行)

**`test_convert.py`**:
- `reassemble_md` 把 accepted 修正落进 md;pending/rejected 不落。
- 幂等:同输入连跑两次,md 内容一致。
- 无 `corrections.json` 时正常产出(与不存在这一层行为一致)。
- manifest 缺失 / total=0 时安全返回 `None`,不写、不抛。

**`test_debug_view.py`**:
- `handle_post` 收到 `/reassemble` 且 `dirty` 为真 → 触发注入的 `reassemble_fn`、清 dirty。
- `/corrections` 成功后 `dirty` 置真;`dirty` 为假的 `/reassemble` 不触发重跑。
- CLI `--reassemble` 走通(可 smoke:调用 reassemble 并断言 md 写出)。
- **不做多线程竞态单测**(见 §7),锁策略靠评审核对。

全套 `python -m pytest scripts/pipelines/textbooks/tests/` 保持全绿(当前基线 247 passed)。

## 9. 关联 follow-up(不在本 spec 代码范围)

交接文档 §1.2/§3.4 的"已定论:不做任何形式的图片打包/别重新实验"措辞有误,需订正
(纯文档,不改代码):

- **L-T31 合成图打包**(N 图拼成一张 → 一次 Read,分辨率被压):实测 10/10 下标读错,
  "别用"成立。
- **L-T30 路径列表打包**(N 个独立文件、原分辨率、模型各自 Read):只测过更慢更省钱,
  **未测过正确性**,无降质证据。当前 `call_claude_vision_batch` 走的正是这种,
  真实跑 13/13 采纳是其不降质的正面证据。

订正方向:把"禁一切打包"收窄为"仅禁 L-T31 合成图打包",并把两条 lessons 措辞分清。
`vision_repair.py` 代码**不改**。是否本次顺手改文档,待所有者决定。
