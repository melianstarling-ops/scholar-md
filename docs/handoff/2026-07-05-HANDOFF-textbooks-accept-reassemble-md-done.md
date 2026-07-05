# HANDOFF —— textbooks "采纳→落 md" 闭环已建成 + 收尾;下一步 Phase C 生成侧调度 + 多后端封装

> 撰写:2026-07-05。承前:[2026-07-05 视觉修复审核闭环交接](2026-07-05-HANDOFF-textbooks-vision-repair-review-loop-done.md)。
> 本会话补齐了"debug 审核点采纳→修正进最终 .md"这条断掉的闭环,做了 3 处收尾 polish,并订正了前一份交接的过时/错误描述。**下一步聚焦 §3.3 Phase C 生成侧调度 + §3.4 多后端封装。**

## §0 一句话结论

采纳→落 md 闭环已建成(257 测试全绿)、真实数据回填成功。**但交互终审(人在浏览器点采纳、肉眼看 md 变)还没做**——当前 Paul 的 13 条修正全部已采纳、没有 pending 可审,所以要等下次出现新的公式识别错误(产生新 pending correction)时才能真机验一次(见 §2)。下一步:**§3.3 Phase C 生成侧调度**(做了它就会产生新 pending,正好顺带做完 §2 的终审)+ **§3.4 多后端封装**。

## §1 本会话已完成

### §1.1 采纳→落 md 闭环(代码)

根因:debug 审核点"采纳"只改 `corrections.json` 的 `status`,而真正写 `.md` 的是转换管线的 `assemble()`,两个入口互不触发 → 采纳后 `.md` 要等下次完整 `convert` 才更新。

修法(设计/计划见 `docs/superpowers/{specs,plans}/2026-07-05-textbooks-accept-reassemble-md-*`):
- **`convert.reassemble_md(doc_dir, pdf_path, dpi)`**:复用管线里唯一的 `assemble()` 幂等重组、覆盖写 `stem.md`。只写 md,不碰 selfcheck/manifest;人工确认门不变(只应用 `status=="accepted"`)。
- **`debug_view` serve**:加 `/reassemble` POST 路由 + `dirty` 门控(无新采纳的翻页秒回不空跑)+ 一把 `threading.Lock`(串行化 POST,杜绝并发写同一 md)+ **启动即对账**(serve 一起来先跑一次 reassemble,把 md 同步到 json 当前状态)。
- **触发点(多点、幂等、互为安全网)**:启动对账 / 翻页 ping / 页尾 `S` 键+"同步 md"按钮 / `debug_view --reassemble` CLI(无 UI 收尾与回填入口)。
- **人工确认门红线保留**:生成 corrections 可自动,但 `status: accepted` 之前不落 md。

### §1.2 收尾 polish(commit bcb08aa)

- reload 分支的 `/reassemble` ping 加 `keepalive:true`(防 `location.reload()` 卸载页面时中止请求)。
- `syncmd` 按钮 / `S` 键做 SERVE-gate(静态导出下禁用,不再死点)。
- `handle_post` dirty 乐观清零加注释(失败不重试本轮,靠下次采纳/启动对账补偿)。

### §1.3 数据回填 + 文档订正

- 对 Paul 跑了一次 `--reassemble`,把此前"已采纳未落盘"的 4 条(50/3、53/20、54/6、54/15)补进 `.md`,13 条现全部落盘。
- 订正了前一份交接 §2 状态表(9+4待审 → 13 全 accepted)、§1.2/§3.4 打包措辞(见那份文档的〔订正〕:**仅禁 L-T31 合成图打包,L-T30 路径列表打包无降质证据、可用**)。

## §2 待办:闭环交互终审(等新 pending 时做)

**为什么没做**:回填后 Paul 13 条修正全是 `accepted`,没有 pending 可审。闭环的**逻辑正确性**已由 257 测试 + 真实回填(13 条真进了 md)证明;但"人在真浏览器点采纳、肉眼确认 md 跟着变"这一交互终审,需要有 pending correction 才能走。

**下次出现新公式识别错误(新 pending)时,这样验一次**(devtools Network 面板看请求):
1. 起服务:`.venv-textbooks/Scripts/python.exe -m scripts.pipelines.textbooks.debug_view --doc <stem-dir> --serve`
2. `R` 键筛"待审",采纳一条 → 翻页 → 看 `<stem>.md` 里那条变成修正版(Network 有 `POST /reassemble` 200)。
3. 页尾:最后一页采纳后不翻页,按 `S` 键 → md 更新。
4. dirty 门控:纯翻页不采纳,`/reassemble` 回 200 但 md 不变。

**没有新 pending 时**也可用"驳回↔采纳来回"验(会真实改 `corrections.json`,验完记得把状态改回 accepted):驳回一条→翻页→md 那条变回引擎原文→采纳回来→翻页→变回修正版。

> **注意**:§3.3 做完(生成侧能产出新 corrections=pending)后,自然就有 pending 可审,这条终审可顺带一起完成。

## §3 下一步(本次交接的重点)

### §3.1 Phase C 生成侧调度

现状:"应用"半条腿已在生产路径(`convert.assemble` + debug 采纳即时落 md);缺"生成"半条腿的调度——即什么时候自动跑 `debug_repair.py`(检测疑似)+ `vision_repair.py`(视觉修 → 产 pending corrections)。

要点(承前一份交接 §3.3,红线不变):
- 建议先做成 `convert.py` 的**显式可选阶段**(命令行开关如 `--repair`,不默认跑),几轮真实使用验证后再考虑默认接入。
- 人工确认门必须保留:生成 corrections 自动,`accepted` 之前不生效。
- **顺带把 §3.2 的 p48 检测漏洞一起处理**(见前一份交接 §3.2):`find_suspicious_blocks` 只认两种启发式,没覆盖"花括号不配对/截断"硬报错;把 `render_errors.json`(KaTeX 真报错、零假阳性)纳入疑似候选池,别为每种报错写新正则。

### §3.2 视觉调用多后端封装

现状:`vision_repair.py` 只有 claude 一个后端;`run_vision_repair` 有 `batch_fn`/`parallel`/`vision_fn` 注入点,但没做成"多后端可配置切换"封装。

要点(承前一份交接 §3.4):
- 全程参照 `Project_MRI_Safety`:`00_System/scripts/kb_core.py` 的 `resolve_backend_argv`/`call_backend`;`SOP-Batch_Agent_Run.md`(禁 MCP、进度、适度并发);`lessons_kb_ingest.md`(K7 npm shim、K11 kimi 独立 exe+UTF-8、K13/K15 MCP 污染)。
- **打包认知已订正**:L-T30 路径列表打包(当前默认 `batch_size=5`)与并发不冲突、可并存;只禁 L-T31 合成图打包。设计并发照抄现有 `ThreadPoolExecutor(max_workers=parallel)`。
- **Kimi 是否接入待所有者拍板**(省钱/限流备份的产品决策,非技术阻塞);claude 单后端质量已验证很好,开工前先问一句。

## §4 其它开放项(保留,未动)

- **§3.1 滚动异常 bug**(前一份交接 §3.1):审核卡片多时工具栏滚出视口;试过 `html/body` 锁高度没解决,需真浏览器 devtools 定位 `document.scrollingElement`。
- **selfcheck.json 采纳后陈旧**(已知限制、by design):采纳改了 md 但不刷新 selfcheck 的 suspicions/katex,留待下次正式 `convert` 重算。

## §5 环境 / 红线

- 分支 `feature/textbooks-engine`(领先 main 很多 commit,保持现状继续开发,未 merge/push)。本次代码 5 commit `129f6c4..8b02876` + polish `bcb08aa`。
- 测试:`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/`(基线 257 passed)。
- 红线:人工确认门保留、`res.json` 不动、`02_Source/` 只读、`03_Output/` 是 gitignore 的 OneDrive symlink(产物不入版本库)、对外操作(装依赖/merge/push)前所有者确认。
