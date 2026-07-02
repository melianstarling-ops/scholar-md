# 设计 — textbooks batch.py 外部工作区调用

> 撰写：2026-07-02。承接 [大文件稳健化交接](../../handoff/2026-07-02-HANDOFF-textbooks-large-file-done.md) §3 Task 1。

## 1. 背景与目标

`convert.py` 目前只吃单文件（`--src` 单个 PDF），且大文件稳健化（checkpoint/watchdog）已完工并全 review 通过。
下一步：批量入口 `batch.py`，对齐 [AGENTS.md §H.5](../../../AGENTS.md) 自适应 I/O 强制约定与 `patents/batch_patents.py` 的
外部工作区调用惯例（`--src`/`--out`/env 回退/`--resume`/`--list`），让所有者能一次性丢一整个书架的 PDF 进去、
无人值守跑完、产物落到任意外部目录、本仓零污染。

不涉及：`--flat` 平铺模式、Tier1 AI 审查（`--review`）、`--ocr`（patents 专属）、engine/reconstruct/selfcheck/triage 内部逻辑。

## 2. 架构：进程隔离模型

`batch.py` 是**同进程内的顺序编排器**：对每本发现的 PDF，构造一份 `convert.py` 的 argv，
调用 `watchdog.run_until_done(argv, max_restarts=...)`（Python 函数调用，非 shell 子进程套子进程）。
`watchdog` 已经把 `convert.py` 起成独立子进程并在崩溃时自动重启——这正好给了每本书**独立的崩溃隔离域**：
某本书触发 CUDA/驱动/OOM 杀进程，只影响它自己的 watchdog 循环，不拖累批次里排队的其他书。

书与书之间**严格顺序处理**（单 GPU 资源，无需并发）。`batch.py` 拿不到 `convert_pdf()` 的 Python 返回值
（只能拿子进程退出码），因此每本书跑完后要从磁盘读回结构化产物（`manifest.json`、`<stem>_selfcheck.json`）
来拼汇总报告——这也是下面 §4 要给 `convert_pdf()` 加 `write_selfcheck` 参数的原因。

## 3. 目录布局（不做 --flat）

`convert_pdf()` 现有行为不变：始终在传入的 `out_dir` 下创建 `<stem>/` 子目录（含 `_work/` 断点目录）。
`batch.py` 不新增平铺选项——大部头一本可能几百页检查点+图片，`_work` 隔离是刚需而非可选项，
平铺到同一目录会让不同书的 `_work`/图片互相污染。

```
--src 指向含 A.pdf、B.pdf 的目录，--out 指定为 D:/out：

D:/out/
  A/
    A.md
    A_selfcheck.json
    _work/          # manifest.json + page_NNNN_res.json 断点
  B/
    B.md
    B_selfcheck.json
    _work/
  _deferred_born_digital/     # 若某本书 triage 为 B 路（born-digital 优质，登记不转）
    C.txt
```

## 4. `convert_pdf()` / `convert.py` / `watchdog.py` 改动：selfcheck.json 落盘

对齐 `convert_patent.py` 现有 `write_selfcheck` 参数模式（小幅新增，不碰大文件稳健化的核心续跑/坏页/毒页逻辑）：

- `convert_pdf(pdf_path, out_dir=None, dpi=cp.DEFAULT_DPI, write_selfcheck=True)`：
  路由 A/C 完成后，若 `write_selfcheck`，把已计算出的 `check` dict 写到 `doc_out/<stem>_selfcheck.json`
  （`json.dump(check, f, ensure_ascii=False, indent=2)`，UTF-8）。路由 B（`_register_deferred`）不受影响，
  本就不产出 md/selfcheck。
- `convert.py` CLI 新增 `--no-selfcheck-json`（`store_true`），转 `write_selfcheck=not args.no_selfcheck_json`。
- `watchdog.py` CLI 同步新增 `--no-selfcheck-json`，若指定则追加到传给 `run_until_done` 的 argv 里
  （`run_until_done` 本身不用改——argv 对它是不透明透传列表）。

这三处都是已完工/全 review 通过的文件上的加法式改动，不修改现有断点/毒页/坏页判定逻辑，需要补对应单测
（`write_selfcheck=False` 时不落盘、CLI flag 正确透传）。

## 5. `batch.py` CLI

```
python -m scripts.pipelines.textbooks.batch
  --src [PATH ...]        文件/目录/多个；省略 → env SCHOLARMD_TEXTBOOKS_SRC → 仓库内 02_Source/textbooks/
  --out PATH                省略 → 仓库内 03_Output/textbooks/（对齐 patents 的独立产物根，
                                    与 convert.py 单文件"--out 省略=就地"不同——batch 场景默认更适合汇总到统一产物树）
  --dpi INT                  默认 cp.DEFAULT_DPI，透传每本书
  --resume                    跳过"已全部跑完"的书（见 §6）
  --limit N                  只处理发现列表的前 N 本（调试/小样验证，如 §3 待验收任务的 100→300→800 页分档实测）
  --max-restarts N          透传给每本书的 watchdog，默认 cp.MAX_RESTARTS
  --no-selfcheck-json     不写 <stem>_selfcheck.json（manifest.json 始终写，不受此影响）
  --list                       只列出发现的待处理 PDF + 各自的解析产物路径，不转换
```

`discover(src_paths)`：把 `--src`（文件/目录/多个）展开成去重排序的 PDF 路径列表——
接受目录自动扫 `*.pdf`，接受单文件直接收录，两者可混用（对齐 H.5 §5 与 `general/batch.py:collect_jobs` 的展开逻辑，
但输出根统一用 `--out`/默认根，不做 per-PDF 就地）。**跨目录同名 stem 检出即硬失败**：多 `--src` 混用时，
两个不同目录下的同名 `A.pdf`（不同内容）会撞到同一个 `out_root/A/`，指纹失配会互相清空对方的 `_work`
断点、批跑互相打架——这是正确性问题，不是该警告后继续跑的事。`discover()` 检出重复 stem 时列出冲突路径，
`main()` 直接返回非零、不处理任何一本书。

## 6. `--resume` 判断（B 路不设短路 + 指纹校验 + 毒页感知）

`convert_pdf()` 无论有无 `--resume` 都会按页断点自动续跑（看 manifest 指纹）；但它**每次运行都会写出部分
`<stem>.md`**（哪怕还没跑完），所以不能像 patents 那样用"md 文件存在"判断跳过。判断逻辑有三处需要与
`convert_pdf()`/`triage()` 自身语义对齐，否则会误判：

**B 路不设跳过短路。** 路由 B 的书完全不经过 checkpoint 系统——`_register_deferred` 只在
`out_dir/_deferred_born_digital/<stem>.txt` 留一个登记标记，不存指纹，源文件被同名替换后标记也不会失效。
与其给 `_register_deferred` 加指纹存储（要动已测过的 B 路文件格式），不如直接不把这个标记当跳过信号：
`triage()`（`triage.py`）只做 5 页 PyMuPDF 文本采样，无 GPU、毫秒级，每次 `--resume` 重新 triage+登记是
幂等且便宜的，顺带自动解决"源文件被替换"的过期问题。

**A/C 路必须校验指纹/DPI。** 只看 `pages_todo` 为空不够——用户 `--dpi 200` 重跑，或同名源文件被替换内容，
manifest 会显示"全部完成"但按 `convert_pdf()` 自己的重置条件（`convert.py:50`）其实该整本重转。跳过判断要
用同一条件（`cp.fingerprint_ok`），失配即不算 done（代价是多开一次 fitz，和 `convert_pdf()` 本来就要做的一样）。

**毒页与瞬时失败页要分别处理。** `manifest["failed_pages"]` 混了两种：`kind=page-exception`（页级瞬时异常，
没写 res.json，留在 `pages_todo()` 里，`convert_pdf()` 每轮都会重试——这是特性）和 `kind=process-killed`
（毒页，`convert.py:70-72` 显式从 `todo` 里过滤掉，`convert_pdf()` 自己都不会再碰它）。跳过判断要照抄
`convert.py:70-72` 同一段过滤逻辑，**不能**再要求 `not manifest["failed_pages"]`——一本"跑完但有 1 个毒页"
的书，`convert_pdf()` 重跑时对它什么也不会做（只是白白重新 assemble 一遍），应判定为 done（SUSPECT）而不是
永远判"未完成"、每轮 `--resume` 都重新起 watchdog 子进程。

```python
def _already_done(out_root: Path, pdf_path: Path, dpi: int) -> bool:
    work_dir = out_root / pdf_path.stem / "_work"
    manifest = cp.load_manifest(str(work_dir))
    if manifest is None:
        return False
    if not cp.fingerprint_ok(manifest, str(pdf_path), dpi):
        return False                         # 源变了或 DPI 变了：不算 done
    total = manifest["fingerprint"]["page_count"]
    poisoned = {f["page"] for f in manifest["failed_pages"] if f["kind"] == "process-killed"}
    todo = [p for p in cp.pages_todo(str(work_dir), total) if p not in poisoned]
    return not todo                          # 毒页不算"未完成"；瞬时失败页仍算（允许重试）
```

复用 `checkpoint.py` 现成的 `load_manifest`/`fingerprint_ok`/`pages_todo`，不重新手撸判定逻辑。
`--resume` 触发跳过时只打印 `[SKIP] stem`，不起 watchdog 子进程——省掉整趟子进程开销（B 路书不跳过，
但它的重新登记本身就很便宜，见上）。

`--limit` 在 `--resume` 过滤**之前**应用于原始发现列表（对齐 patents/general 惯例：先截断再逐个判断跳过）。
已知行为、非 bug：`--resume --limit 100` 若前 100 本都已跳过，不会自动往后推进到第 101 本——`--limit` 是
调试/小样验证用的截断，不是"处理接下来 N 本未完成的书"的游标，需要手动加大 `--limit` 才能推进。

## 7. 汇总报告

每本书跑完（`rc==0`）后：

- 若命中 `_deferred_born_digital/<stem>.txt` → 打印 `[B] stem — 已登记 deferred`。
- 否则读回 `out_root/stem/_work/manifest.json`（`route`、`failed_pages`）+
  `out_root/stem/<stem>_selfcheck.json`（若存在——`--no-selfcheck-json` 时不存在，退化为只报 route/failed_pages）。
  打印 `[OK/SUSPECT] stem — route=A/C failed_pages=N coverage=...`（若有 selfcheck）。
- `rc!=0`（watchdog 达 `--max-restarts` 仍未跑完）→ 打印 `[GIVEUP] stem`，计入失败计数。

结尾打印总计（OK/SUSPECT/GIVEUP/SKIP 计数 + 输出根路径）。返回码语义明确：**仅 GIVEUP（watchdog 达
`--max-restarts` 仍未跑完）计入失败、返回 1**；SUSPECT（有 `failed_pages` 但 `rc==0` 跑完）不影响返回码，
对齐 patents（`return 0 if failed == 0 else 1`，`failed` 指异常/崩溃而非自检不通过）——SUSPECT 是需要人工
复核的产物，不是批处理本身失败。

## 8. 测试策略

- `convert.py`/`watchdog.py` 新增参数补单测：`write_selfcheck=False` 时不落盘、`--no-selfcheck-json` CLI 正确
  转为 `write_selfcheck=False`、watchdog 正确把该 flag 透传进子进程 argv。全程打桩 `predict_page`，零 GPU
  （沿用现有 `test_convert.py`/`test_watchdog.py` 模式）。
- `batch.py` 自身单测：像 `test_watchdog.py` 一样给 `watchdog.run_until_done` 注入假 `runner`
  ——`batch.py` 里驱动每本书的函数接受可选 `runner` 参数并透传给 `wd.run_until_done(argv, max_restarts=..., runner=runner)`，
  默认 `None`（真实场景走真子进程）。单测用假 `runner` + 临时目录预置 `manifest.json`/`_deferred_born_digital` 标记，
  验证：`discover()` 展开逻辑（文件/目录/多个/去重/跨目录同名 stem 硬失败）、`--resume` 跳过判断
  （指纹/DPI 失配不跳、毒页豁免但瞬时失败页不豁免）、`--limit` 截断顺序、汇总计数正确、返回码正确
  （仅 GIVEUP 记失败）。全程无真子进程、无真 GPU。

## 9. 范围外（已确认）

`--flat`、`--review`/Tier1 AI 审查、`--ocr`（patents 专属 OCR 夹层）、"`--out` 省略时就地"（batch 场景统一走
独立 `03_Output/textbooks/` 根，与单文件 `convert.py` 的"就地"默认不同，两者场景不同不冲突）。
