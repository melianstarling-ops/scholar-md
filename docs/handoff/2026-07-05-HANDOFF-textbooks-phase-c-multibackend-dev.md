# HANDOFF —— textbooks 收尾(p48/滚动/agy 修 p48)完成;下一步 Phase C 生成侧 + 多后端封装

> 撰写:2026-07-05。承前:[采纳→落 md 交接](2026-07-05-HANDOFF-textbooks-accept-reassemble-md-done.md)。
> 本会话把前份交接剩的两个 bug(p48 检测漏洞、审核 UI 滚动)修了,并用 **Antigravity(agy) 视觉后端实修了 p48 那条公式**(端到端验证新链路)。**下一步进入 §3.3 生成侧调度 + §3.4 多后端封装,按所有者定的「Claude opus 指挥 + CLI 分派 codex/agy/kimi」模式开发。**

## §0 一句话

采纳→落 md 闭环、p48 检测漏洞、审核 UI 滚动 bug **全部已修**;agy(Gemini 3.5 Flash) 视觉后端实测读图逐字级、已实修 p48 block6 并采纳落 md(KaTeX 报错已消)。下一步:**§3.3 Phase C 生成侧调度 + §3.4 多后端封装**,用 **Claude opus 大脑指挥 + CLI 分派 codex/agy/kimi 执行** 的模式开发。

## §1 本会话已完成

1. **p48 检测漏洞修复**(commit `70d4689`):KaTeX 硬报错 `render_errors`(零假阳性)靠 `latex_head`↔`block_content` 前缀匹配回 display_formula 块,与启发式疑似合并去重纳入候选池。`build_repair_worklist` 现报 **14 处**(原 13 + 漏掉的 block6)。260 passed。
2. **审核 UI 滚动 bug 修复**(commit `10eba6a`):**根因是 VS Code Simple Browser(webview/iframe)特异,Chrome 本来就正常**——难怪前人在通用 CSS 层修不好。两处:
   - `body{height:100%}` → `height:100vh`(对齐 `patents/debug_view`;webview iframe 里 `height:100%` 链条塌陷成内容高度、让 webview 层滚动)。
   - 点左栏框联动的 `scrollIntoView({block:"center"})` 会连带滚 iframe/body 层、把顶栏挤出 → 改 `scrollCenter` 手动滚"最近可滚容器"居中,**绝不碰 root**。
3. **agy 视觉后端验证 + 实修 p48**(见 §2)。
4. 前置收尾(前份交接已记):3 条前端 polish(`bcb08aa`)、采纳→落 md 闭环(5 commit)、交接订正(`e549dfa`)、Paul 回填。

## §2 agy(Antigravity) 视觉后端 —— 已验证可用

调用细节见 memory `reference_multi-backend-clis`。要点:
- 工具 `agy`(Antigravity CLI 1.0.16),路径 `~/AppData/Local/agy/bin/agy.exe`。默认模型 **Gemini 3.5 Flash (High)**;`--model` 可切(Gemini 3.1 Pro / Claude Sonnet 4.6 / Opus 4.6 / GPT-OSS 120B)。
- 调用:`agy -p "<prompt>"`(非交互),读图让它用 Read 工具读**绝对路径**。**别加 `--dangerously-skip-permissions`** —— 读操作默认放行,加了反被 Claude Code auto mode 安全门拦。
- **实测(2026-07-05)**:读 p48 block12 裁图,输出与人工确认答案逐字级一致(最难的"围道下标 s'/c' 而非分母"也读对);block6(1.53b 缺右花括号)修正 `l=-\frac{\mu\int_c \vec{\mathcal{H}}_t\cdot\vec{a}_n dl}{I(z,t)}` 已人工采纳、reassemble 落 md → **p48 KaTeX 报错已消**。视觉质量与 claude 后端同级。
- 意义:这是 §3.4 多后端的**第一个实证后端**。

## §3 下一步开发(本次交接重点)

### §3.1 编排模式(所有者已定)

**Claude opus 作大脑指挥**:负责规划、编排、审核;把读图/生成等实际执行**通过 CLI 分派给 codex / agy(antigravity) / kimi**。对齐 [[feedback_delegate-vision-to-subagent-cli]](读图重活委派 CLI)与 [[feedback_right-size-orchestration]](按难度选编排强度)。

CLI 后端清单(均在 PATH):
- **agy** —— 已验证(§2)。
- **codex** —— `~/AppData/Roaming/npm/codex`;接口待实测(OpenAI Codex CLI,一般 `codex exec "<prompt>"`)。
- **kimi** —— `~/AppData/Roaming/npm/kimi`;UTF-8 locale 前提见主仓 `lessons_kb_ingest` K11。
- **claude** —— 现有 `vision_repair.py` 后端(`_resolve_claude_bin` 绕 npm shim、`--strict-mcp-config` 禁 MCP),13/13 已验证。

### §3.2 Phase C 生成侧调度(前份 §3.3 承接)

让转换后能(**显式可选开关,不默认跑**)自动跑 `debug_repair`(裁图)+ `vision_repair`(视觉修 → pending corrections)。人工确认门红线不变(生成自动、`accepted` 前不生效)。建议先做成 `convert.py --repair` 阶段,几轮真实验证后再议默认接入。

### §3.3 多后端封装(前份 §3.4 承接)

把 `vision_repair` 的单 claude 后端做成**多后端可切换**(`--backend claude|agy|codex|kimi`)。参照 `Project_MRI_Safety` `kb_core.resolve_backend_argv`/`call_backend` + `SOP-Batch_Agent_Run`。
- **打包认知已订正**:仅禁 L-T31 合成图打包;L-T30 路径列表打包(当前默认 `batch_size=5`)与并发不冲突、可用。
- agy 后端骨架已有实证调用方式(§2),codex/kimi 接口需先各跑一次小测确认(读图 + 输出格式)。

## §4 其它开放项

- **selfcheck.json 采纳后陈旧**(已知限制,by design):留待下次正式 `convert` 重算。
- (审核滚动 bug 已修,从开放项移除。)

## §5 环境 / 红线

- 分支 `feature/textbooks-engine`(领先 main 很多 commit;本次已 push)。本会话代码 commit:`70d4689`(p48 检测)、`10eba6a`(滚动)、`bcb08aa`(polish),及采纳→落 md 的 5 commit(`129f6c4..8b02876`)。
- 测试:`.venv-textbooks/Scripts/python.exe -m pytest scripts/pipelines/textbooks/tests/`(基线 260 passed)。
- 红线:人工确认门保留、`res.json` 不动、`02_Source/` 只读、`03_Output/` 是 gitignore 的 OneDrive symlink(产物不入版本库)、对外操作(装依赖/merge/push)前所有者确认。**agy 调用别加 `--dangerously-skip-permissions`**。
