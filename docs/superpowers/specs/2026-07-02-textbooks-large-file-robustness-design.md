# textbooks 大文件稳健化设计文档

- 日期：2026-07-02
- 状态：设计草案（`superpowers:brainstorming` 产出，所有者已口头批准，待复审 spec）
- 分支：`feature/textbooks-engine`
- 范围：让 `textbooks` 单文档转换在**大部头（700+ 页）**上稳健——断点续跑、磁盘有界、坏页隔离、进度反馈
- 不涉及：patents/general 管线改动；引擎/后端加速（vLLM 等，见下方性能前提）；batch.py 外部工作区调用（独立任务）

> 立项依据：[textbooks 首版交接](../../handoff/2026-07-01-HANDOFF-textbooks-v1-done.md) §3 Task 3、
> 2026-07-02 家用机性能实测（下 §1）、[OCR 推理性能调研报告](../../../04_Docs/PaddleOCR-VL%201.6%20文档%20OCRVLM%20推理性能优化可执行调研报告.md)（私有）。

## 1. 背景：性能前提（2026-07-02 实测）

在动本设计前，2026-07-02 于家用机 A（RTX 5060/Blackwell/sm_120、R9 9900X、Win11）用真实 803 页样本
`02_Source/textbooks_samples/Paul_Analysis_MTL_scan.pdf` 实测，确认了大文件转换的真实瓶颈：

- **速度地板 ≈ 50s/页**（DPI 150；200dpi=63s、150dpi=50s、120dpi=54s，第 200 页实测）。本机原生 Windows
  本地路径的便宜杠杆已耗尽：`PADDLEOCR_VL_LOCAL_BATCH_SIZE=1` 硬锁批处理、精度已 bf16、DPI 已优化。
  病根是默认动态图后端逐块串行自回归解码（GPU 利用率 96% 但功耗仅 45W/145W）。
- **不是 GPU 架构问题**：首版设计 §2 记录 RTX 4060（Ada）跑同页也是 78s，与 5060 的 ~77s 一致——
  换卡不解决，要 ~2s/页只能上 vLLM 服务化（Windows 需 WSL2/Docker，暂缓）。
- **所有者决定**：先接受 50s/页，把大文件转换做**稳/可续/磁盘有界**，让单本大部头能通宵无人值守跑完。

**50s/页的直接后果**：803 页 ≈ 11 小时、300 页 ≈ 4 小时。在这种时长下，断点续跑不是可选项而是刚需——
第 690 页崩掉不能让前 689 页（9.5 小时算力）全废。这正是本设计的核心动机。

## 2. 现状问题（[convert.py](../../../scripts/pipelines/textbooks/convert.py) 逐条）

| 问题 | 位置 | 现象 |
|---|---|---|
| 磁盘无界 | convert.py:29-34 | `tempfile.TemporaryDirectory()` + `pdf_to_pngs` 一次性栅格化**全部页**，全程堆在单一 temp（803 页@200dpi≈2.6GB） |
| 无断点续跑 | convert.py:27-35 | `all_blocks`/`md_pages` 只在内存，temp 结束即删——崩溃 = 全丢 |
| 一坏页毁全书 | convert.py:31-33 | `for png: predict_page(...)` 无 try/except，单页异常掀翻整份 |
| 全程静默 | convert.py:31-34 | 无进度输出，11 小时不知道跑到哪 |

## 3. 架构（方案 A：拆 checkpoint.py + 改 preprocess/convert）

沿用本仓"每模块单一职责"风格（triage/preprocess/engine/reconstruct/selfcheck 已各自独立）。三个改动点：

- **`preprocess.py`**：新增**单页栅格化**函数（现有 `pdf_to_pngs` 一次性全栅格化保留不动，供小文件/测试用）。
- **`checkpoint.py`（新，纯确定性、无 GPU 依赖）**：manifest 读写、PDF 指纹校验、待跑页集计算、坏页记录。
- **`convert.py`**：编排层改为逐页流式 + 可续跑。

### 3.1 工作目录布局（每份文档）

持久目录（不再用 `TemporaryDirectory`）：

```
<out_dir>/<stem>/
  <stem>.md                 # 最终产物(每次运行都从检查点重写,含部分完成态)
  _work/
    manifest.json           # 进度 + PDF 指纹 + 坏页清单
    page_0001_res.json      # 每页检查点(保留,来自 engine.predict_page 的存盘)
    page_0002_res.json
    ...
    page_0201.png           # 瞬态:仅"当前正在处理的那页"PNG 存在,predict 后即删
```

`_work/` 随输出目录走网盘同步（803 个小 json 体积可忽略，文件数多但可接受）。

### 3.2 逐页流式（砍掉"分块"参数）

交接文档原设想"每 N 页一块"。但既然 `engine.predict_page` 每页都存 `<stem>_res.json` 检查点，
分块无必要——**逐页流式**更简单也更省磁盘：

```
对每一页 i（1..N）：
  若 page_{i}_res.json 已存在且可解析 → 跳过（续跑）
  否则：
    栅格化第 i 页 → page_{i}.png       # 磁盘峰值 = 1 张 PNG
    try:
      blocks = predict_page(...)       # 非空时 engine 内部已写 page_{i}_res.json
      若 blocks 为空且文件未写出 → 编排层显式写 {"parsing_res_list": []}  # 见下"空白页"
    except: 记 failed_pages[i] = 错误摘要，继续（不写 res.json）
    删除 page_{i}.png
```

**空白页检查点（防无限重跑）**：`engine.predict_page` 对空结果 `if not results: return []` **不调用
`save_to_json`**（[engine.py:22-24](../../../scripts/pipelines/textbooks/engine.py#L22-L24)），故扫描件里常见的
空白分隔页永不落 res.json，若不处理则每次续跑都重判"未完成"、白花 50s 重confirm 它是空的。**对策**：编排层在
`predict_page` 返回后，若返回空且 `page_{i}_res.json` 不存在，**显式写 `{"parsing_res_list": []}`** 作为
"已完成、0 块"标记（不改 engine，仅补编排层）。这样空白页被正确判定为完成、续跑跳过。

**磁盘峰值 = 1 张 PNG（~1.5MB@150dpi）**，检查点粒度 = 每页。按 YAGNI 不引入 `--chunk-pages`。
（PNG 用完即删；未来 debug_view 需页图时从 PDF 按需重栅格化，不必留存。）

### 3.3 断点续跑 + 指纹

- **指纹**：manifest 存 PDF 的 `{page_count, size_bytes}` **加上请求的 `dpi`**。重跑时重算并比对：
  - **PDF 指纹失配（page_count/size_bytes 变）** ⇒ 判定源已变，清空 `_work` 全新跑（打印告警，避免脏断点污染）。
  - **DPI 失配（请求 `--dpi` ≠ manifest 记录的 dpi）** ⇒ 同样清空 `_work` 全新跑 + 告警。否则会保留旧 DPI
    的 res.json、只对剩余页用新 DPI，同一份 md 混入两种精度且无警告。DPI 是产物精度的一部分，必须纳入失配判定。
  - **全部匹配** ⇒ 进入续跑，逐页按 3.2 判据跳过已完成页。
- **续跑判据**：`page_{i}_res.json` 存在且 `json.load` 成功（`try/except` 包裹）⇒ 该页完成、跳过；
  否则（缺失/上次失败/**半截或损坏的 json**）当作未完成重跑。解析失败绝不能让重组/自检流程炸掉。
- **失败页重试**：上次记入 `failed_pages` 的页，续跑时会因"无 res.json"而自动重试（可能是瞬态 CUDA OOM）。

### 3.4 坏页隔离

`predict_page` 包 try/except。单页异常 ⇒ 记 `failed_pages`（页号 + 错误摘要）→ 继续下一页。
该页在 md 中留空缺（不插占位垃圾），并在 manifest/自检报告中明示，所有者可知情复核。

### 3.5 重组 / 自检

页循环结束后（无论是否全部成功），按页号顺序读 `_work/` 下各 `page_{i}_res.json` → 逐页
`reconstruct_markdown` → `"\n\n".join` 拼接写 `<stem>.md` → 跑 Tier0（`block_coverage` + `katex_incompat_scan`）。
**每次运行都执行此步**（廉价），故：即使跑一半中断，也产出"当前检查点状态"的部分 md + manifest（如
"690/803 完成、3 失败"）。

### 3.6 进度反馈

逐页打印：`[page 201/803] 48s (完成 199 失败 2 跳过 0 ETA 8.4h)`。ETA 用已完成页耗时的滚动均值估算。

### 3.7 进程级崩溃恢复（看门狗 + 毒页检测）

§3.4 的坏页隔离只挡**单页 Python 异常**。11 小时长跑真正会"死在半夜"的风险是**整个进程退出**——CUDA 上下文
损坏、驱动挂起、被 OOM-killer 杀、Windows 睡眠打断——这些 `try/except` 捕不到。要兑现"无人值守跑到天亮"，
需两件东西配合：

**(a) 看门狗 `watchdog.py`（新，薄包装）**：把 `convert.py` 作为**子进程**反复拉起：
```
loop:
  rc = subprocess.run([python, convert.py, --src ..., --out ..., --dpi ...])
  若 rc == 0            → convert 的页循环正常跑完(所有页 done 或硬失败) → 结束
  若 rc != 0（进程被杀）→ 重启续跑(检查点让它接着走)
  全局兜底:累计重启数 > MAX_RESTARTS(如 50) → 放弃并报告(防极端死循环)
```
看门狗**本身很薄**，因为续跑的硬骨头已在 §3.2–3.3 啃完；它只负责"进程死了再拉起来"。

**(b) 毒页检测（在 `convert.py` + `checkpoint.py`，防无限重启）**：若某页每次都让**进程整体崩**（非 §3.4 能
捕获的异常），傻重启会永远卡在这一页。对策：
- `convert.py` 在调 `predict_page` **之前**，先把 `in_progress = {page: i, attempts: k}` 写进 manifest；
  该页成功/被 §3.4 捕获后**清除** `in_progress`。
- 进程被硬杀时，manifest 会残留 `in_progress = {page: i}` 且该页无 res.json ⇒ 下次启动时判定"第 i 页崩过进程"，
  `attempts += 1`。当 `attempts >= MAX_HARD_ATTEMPTS`（如 2）⇒ 把该页移入 `failed_pages`
  （reason: "process-killed, skipped after N attempts"）并跳过，否则再试一次。
- 这样进程级毒页也会在有限次后被跳过，看门狗循环必然终止。

**退出码约定**：`convert.py` 页循环正常结束 ⇒ **exit 0**（即使有 failed_pages，失败是记录不是致命）；
仅当进程被外部杀死才由 OS 返回非零。看门狗据此判断"跑完 vs 崩溃重启"。

## 4. manifest.json 结构

```json
{
  "pdf_path": "...Paul_Analysis_MTL_scan.pdf",
  "fingerprint": {"page_count": 803, "size_bytes": 262467392},
  "dpi": 150,
  "route": "A",
  "completed_pages": [1, 2, 3],
  "failed_pages": [{"page": 47, "error": "CUDA out of memory", "kind": "page-exception"}],
  "in_progress": {"page": 201, "attempts": 1},
  "restarts": 3,
  "updated": "2026-07-02T14:03:00"
}
```

> - `dpi` 参与失配判定（§3.3）：请求 dpi ≠ 记录 dpi ⇒ 清空重跑。`completed_pages` 为示意（实际是全部已完成页号）。
> - `in_progress`：predict 前写、成功/捕获后清；进程被硬杀时残留，用于毒页检测（§3.7）。`kind` 区分
>   "page-exception"（§3.4 捕获）与 "process-killed"（§3.7 毒页超阈值）。`restarts`：看门狗累计重启数。

> `completed_pages` 与磁盘上的 `page_{i}_res.json` 互为冗余校验；判"完成"以 res.json 实际可解析为准，
> manifest 仅作汇总/审计（避免 manifest 与磁盘漂移时误跳）。

## 5. CLI / 入口

两个入口，各自可单独用：
- **`convert.py`（单趟 worker）**：现有 `--src`/`--out` 不变，续跑自动（无需 flag）。**新增 `--dpi`（默认 150）**——
  把实测甜区固化为默认（原 `preprocess.pdf_to_pngs` 默认 200 一并改 150）。跑一趟、可续跑、但不自重启。
- **`watchdog.py`（无人值守 supervisor）**：`--src`/`--out`/`--dpi` 透传给 `convert.py` 子进程，
  外加 `--max-restarts`（默认 50）。进程崩了自动拉起续跑，直到跑完或触发兜底。想真·通宵无人值守就用它；
  想手动单趟就直接用 `convert.py`。

## 6. 测试策略（TDD，尽量无 GPU）

- **`checkpoint.py`（纯单元测试，无 GPU）**：指纹匹配/失配判定、待跑页集计算（给定已存在的 res.json 子集）、
  坏页记录、manifest 往返读写、脏断点（源变）触发清空。
- **`preprocess.py`**：单页栅格化——产出页数正确、只产指定页（PyMuPDF，无 GPU）。
- **`convert.py` 编排（monkeypatch 打桩 `predict_page` 与栅格化，无 GPU）**：
  - 磁盘有界：验证任一时刻 `_work/` 下至多 1 张 PNG（predict 后即删）。
  - 续跑跳过：预置部分 res.json，验证只跑缺失页。
  - 坏页隔离：桩 predict 对指定页抛异常，验证其余页照常完成 + failed_pages 记录正确。
  - **空白页检查点**：桩 predict 对指定页返回 `[]`，验证该页落 `{"parsing_res_list": []}`、下次续跑被跳过（不重跑）。
  - PDF 指纹失配：改 size_bytes，验证 `_work` 被清空重跑。
  - **DPI 失配**：manifest 记 dpi=150、请求 --dpi 200，验证 `_work` 被清空重跑（不混合精度）。
  - **半截/损坏 res.json**：预置截断的 json，验证被当"未完成"重跑，且重组/自检不抛异常炸掉。
  - 部分完成重组：验证中断态也产出部分 md。
  - **毒页检测**：manifest 预置 `in_progress={page:i, attempts:MAX}` 且第 i 页无 res.json，验证启动时该页被移入
    `failed_pages`（kind=process-killed）并跳过，不再重试。
- **`watchdog.py`（monkeypatch/桩 subprocess，无 GPU）**：
  - 子进程返回 0 ⇒ 看门狗结束、不重启。
  - 子进程返回非 0 ⇒ 重启，`restarts` 递增。
  - 累计重启超 `--max-restarts` ⇒ 放弃并报告。
- 真实 GPU 端到端：留作实现完成后在家用机手动 smoke（非自动化测试，因 GPU 路径仅特定硬件可跑）。

## 7. 红线（继承）

不改 patents/general；确定性优先、ML 只判断不改字符；`02_Source/` 只读；每管线独立 venv；
对外操作（push/装大依赖）前所有者确认。本设计不含引擎/后端改动，纯编排稳健化。

## 8. 明确不做（YAGNI / 留待后续任务）

- **分块参数** `--chunk-pages`：逐页流式已使磁盘峰值 = 1 PNG，无必要。
- **vLLM/WSL2 服务化加速**：独立重任务，所有者已决定暂缓（见 §1）。
- **batch.py 外部工作区调用**（`--src` 吃目录/多个、`--flat`、`--list` 等）：交接 Task 2，独立任务。
- **debug_view HTML 对照**：交接 Task 5，独立任务（本设计保留 res.json 检查点为其铺路）。
- **PNG 留存**：默认用完即删；未来若 debug_view 需要，从 PDF 按需重栅格化。
