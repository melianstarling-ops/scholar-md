# textbooks 管线

扫描/教科书 PDF → Markdown（PaddleOCR-VL 1.6 + 确定性重组）。
设计见 `docs/superpowers/specs/2026-07-01-textbooks-pipeline-design.md`。

## 环境
独立 `.venv-textbooks`（勿混用 patents/general 的 .venv）。装 `requirements.txt`。

## 用法
    .venv-textbooks/Scripts/python scripts/pipelines/textbooks/convert.py --src <pdf> [--out <dir>]

## 首版范围
单文档、无/低质文本层扫描件走 OCR 主路。分块/批量/Opus 审查/HTML 复核待后续。

## 模块
- triage.py — 文本层可信度判 A(无层)/B(优质,登记不转)/C(低质) → A/C 走 OCR
- preprocess.py — PDF→PNG 200dpi
- engine.py — PaddleOCR-VL 1.6 封装(惰性单例)
- reconstruct.py — parsing_res_list → md(按 order 重组、公式编号 \tag 绑定、页眉页脚 order=None 剔除、着重号还原)
- selfcheck.py — Tier0 block 覆盖 lint

## 测试
    .venv-textbooks/Scripts/python -m pytest scripts/pipelines/textbooks/tests/ -v

## 已知边界(后续)
分块(≤50页)/批量/断点续跑、Opus AI 审查、debug_view HTML 复核、B 路文本层直取、triage 阈值标定、vllm 加速。

## 大文件 / 无人值守

单本大部头(700+ 页)转换耗时以小时计(本机 ~50s/页@DPI150),支持断点续跑与磁盘有界:

- 逐页流式:任一时刻临时目录仅 1 张 PNG,检查点为 `_work/page_NNNN_res.json`(每页)。
- 断点续跑:重跑同命令自动跳过已完成页;PDF 内容或 `--dpi` 变则自动清空重跑(防混合精度)。
- 坏页隔离:单页异常记入 manifest `failed_pages`,不影响其它页。
- 无人值守:用 `watchdog.py` 反复拉起 convert,进程级崩溃(CUDA/驱动/OOM)自动续跑直到跑完。

```bash
# 单趟(可续跑,手动重跑接着走)
.venv-textbooks/Scripts/python -m scripts.pipelines.textbooks.convert --src book.pdf --out ./out --dpi 150
# 无人值守(崩了自动续跑)
.venv-textbooks/Scripts/python -m scripts.pipelines.textbooks.watchdog --src book.pdf --out ./out
```
