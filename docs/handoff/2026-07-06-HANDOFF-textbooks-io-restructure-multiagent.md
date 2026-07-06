# HANDOFF —— textbooks I/O 双根重构 + 模块化完成;下一步实验室机器 GPU 端到端

> 撰写:2026-07-06。承 [Phase C 多后端交接](2026-07-05-HANDOFF-textbooks-phase-c-multibackend-dev.md)。
> 本会话把转换管线产物拆成**交付根/过程根双目录**、路径收口到 `paths.py`、KaTeX 扫描接入为一等 CLI、更新 README;旧产物已迁移;非 GPU 读取路径已在真实数据上逐字验证。**下一步:把仓库克隆到实验室机器(公司机=RTX 5060/Blackwell,须 cu129)跑真实 GPU 端到端,验证从零转换也落双根。**

## §0 一句话

textbooks I/O 双根重构 8 Task 全部完成(277 单测绿),交付物(md+assets)与过程物(_work/修复/报错/自检/debug.html)分离,`--out`/`--work-dir` 贯穿所有 stage CLI,katex_scan 接入 batch 默认自动跑。真实迁移数据上**读取路径逐字验证通过**。剩 **GPU 真转端到端**未验(需引擎)—— 交给实验室机器。

## §1 本会话已完成

1. **双根布局 + `paths.py` 单一真相源**(commit `6608444`):`DocLayout`/`resolve_layout(stem, deliverables_root, work_root=None)`,`work_root` 缺省 `<out>/_work_root`。所有产物路径经它取,改布局只动一处。
2. **各 stage 吃双根**(`42553f2` convert / `b321b69` debug_repair / `3881e78` vision_repair(仅路径,未碰 AI 逻辑)/ `b2ab0a9` debug_view(debug.html 落过程根))。
3. **katex_scan 新 CLI 积木 + batch 默认接入**(`9f20527` / `fda89ee`):薄壳调 `debug_assets/scan_katex_errors.mjs`,`node` 缺失优雅跳过;batch 每本转完自动跑,`--no-katex-scan` 可关。
4. **README 重写 + watchdog 补 `--work-dir` 转发**(`71f7fbf`)。
5. **旧产物迁移**(已执行):`_realrun_100page_test/Paul_p1-100_scan` 与 `Paul_Analysis_MTL_scan_page49` 两个标准目录迁入双根(过程物含 debug.html 入 `_work_root/`);非标准实验/样本目录原样保留。
6. **规划文档**:spec `docs/superpowers/specs/2026-07-06-textbooks-io-restructure-design.md`、plan `docs/superpowers/plans/2026-07-06-textbooks-io-restructure.md`(`3fbc36c`)。

## §2 双根布局(核心产物形态)

```
交付根 --out(小,可同步/打包)            过程根 --work-dir(大,本地大盘;默认 <out>/_work_root)
<out>/<stem>/                            <work>/<stem>/
  ├─ <stem>.md          ← 成果            ├─ _work/                    manifest + 逐页 res.json
  └─ <stem>.assets/     ← 成果图片        ├─ <stem>_repair/            修复裁图 + worklist.json
                                          ├─ <stem>_render_errors.json
                                          ├─ <stem>_corrections.json
                                          ├─ <stem>_selfcheck.json
                                          └─ <stem>_debug.html
```

删掉整个过程根,交付根仍是一套完整可分发成果。所有 stage CLI 统一接 `--out`(交付根)+ `--work-dir`(过程根,缺省 `<out>/_work_root`)。完整工作流表 + 各 CLI 用法见 `scripts/pipelines/textbooks/README.md`。

## §3 验证结论(证据)

- **全量单测 277 passed**(`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/`)。
- **真实迁移数据读取路径逐字验证(非 GPU)**:用 `resolve_layout` + 真实双根,`assemble()` 从真实 `_work_root/_work` 读 1209 blocks + 应用真实 corrections 重组 md → 与现存 md **190381 字逐字一致**。证明新代码在真实数据上正确地"去过程根找 `_work`、从过程根读 corrections、从交付根读 assets"。
- **katex_scan CLI 真实 md 实跑**:报 0 硬报错(当前 md 已是采纳修正后的干净版);现存 `render_errors.json` 那 1 条正是已修的 p48 公式(采纳后陈旧,by design)。
- **未验**:从零 GPU 转换 → 产物落双根的**写路径**(需真实引擎,见 §4)。

## §4 下一步:实验室机器 GPU 端到端(本次交接重点)

**目标**:在真实引擎上验证"从零 OCR 转换也正确落双根"(本会话只验了读路径 + 迁移数据 + 单测)。

**机器/环境红线**(务必先做):
- 实验室机器 = **公司机 = RTX 5060 / Blackwell** → **`.venv-textbooks` 必须用 cu129 源重建**;cu126 wheel 直接 `Unsupported GPU architecture` 起不来(见 lessons L-T21)。这是硬前置,先建环境再跑。
- 克隆得到的是 git 内容:`scripts/`(全部代码)、`docs/`(本交接 + spec/plan)。**私有内容不随 git**:`04_Docs/`(lessons)、`02_Source/`、`03_Output/` 是 OneDrive/junction,需在该机登录 OneDrive `huliang2026@outlook.com` 同步(见 TODO.md 末「双机同步」),或手动拷一份样本 PDF。
- 本机 pytest 若报临时目录权限,加 `--basetemp=./.pytest_tmp`(见 lessons L-T22)。

**跑法**:
```bash
V=.venv-textbooks/Scripts/python.exe
# 样本:02_Source/textbooks_samples/Paul_Analysis_MTL_scan.pdf(803 页;或先切小页数试)
$V -m scripts.pipelines.textbooks.batch --src <pdf或目录> --out <交付根> --work-dir <过程根/大盘>
```

**验收清单**:
1. 交付根 `<out>/<stem>/` 只有 `<stem>.md` + `<stem>.assets/`(不含任何过程物)。
2. 过程根 `<work>/<stem>/` 有 `_work/`(逐页 res.json)+ `<stem>_selfcheck.json` + `<stem>_render_errors.json`(katex 默认自动跑产出;若该机无 `node` 会打印 `[katex] node 缺失,跳过` 且不失败)。
3. `debug.html`(若跑 debug_view)落**过程根**,不进交付根。
4. 顺带推进 TODO 长期挂着的 **300/800 页分档实测**(磁盘有界/续跑/坏页在更长跑的表现)。

## §5 其它开放项

- **多后端封装 plan 待执行**:`docs/superpowers/plans/2026-07-05-textbooks-vision-repair-multibackend.md`(vision_repair 接 `--backend claude|agy|codex|kimi`)。本会话已把 vision_repair 的**路径**改成双根,多后端是**另一层**(改 AI 调用),两者不冲突,可在双根形状上加。plan 里 Task 7 的 crop 绝对路径已随迁移更新。
- **多 agent 协作模型设计**:所有者要建"Opus 总指挥 + 分派 codex/agy/kimi"的协作模型,待专门讨论(见 TODO 2026-07-06 + memory `project_multi-agent-orchestration`,含本轮实测底子)。
- **`.pytest_tmp_convert/`**:codex 沙箱遗留、被占用无法删(git 读不了、不会误提交),重启后清。
- **selfcheck/render_errors 采纳后陈旧**:已知限制,by design,留下次正式 convert 重算。

## §6 环境 / 红线

- 分支 `feature/textbooks-engine`(领先 main 很多 commit,不合并、原地保留)。本会话 commit:`3fbc36c`(规划基线)→ `71f7fbf`(README),共 9 个。
- 测试:`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/`(基线 277 passed)。
- 红线:人工确认门保留(corrections `status` 语义不动)、`02_Source/` 只读、`03_Output/` 是 gitignore 的 OneDrive symlink(产物不入版本库)、对外操作(装依赖/merge/push)前所有者确认。

## §7 多 agent 执行纪要(本会话方法论)

本轮 I/O 重构由 **Opus 总指挥 + CLI agent 执行**完成,实证:
- **分派**:codex 执行 Task 1–5、7(代码重构),**agy(Antigravity/Gemini)执行 Task 6**(katex_scan,试水成功),Opus 写 Task 8(README/watchdog)+ 全程审核。
- **并行**:两批各并行 2–3 路(改**不相交文件**保证零冲突),墙钟砍半。
- **审核门**:每 Task 由 Opus 独立跑测 + 读 diff 才提交;agent **不 commit**(共享工作树 git 视图会看到全部改动)。
- **省 Opus 流量**:实现代码由 codex/agy 烧各自额度写,Opus 只花在指令 + 审核。
- **踩坑**:agy 交互式"未登录" ≠ `agy -p` 无头不可用(实测无头正常);别只凭 agent 回的"完成摘要"判成功(要独立跑测 + 看产物 + 必要时对时间戳)。详见 memory `reference_multi-backend-clis` / `project_multi-agent-orchestration` 与 lessons L-T35~。
