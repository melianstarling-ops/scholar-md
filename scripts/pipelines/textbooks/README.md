# textbooks 管线

扫描/教科书 PDF → Markdown（PaddleOCR-VL 1.6 + 确定性重组）。
设计见 `docs/superpowers/specs/2026-07-01-textbooks-pipeline-design.md`。

## 环境
独立 `.venv-textbooks`（勿混用 patents/general 的 .venv）。装 `requirements.txt`。

## 用法
    .venv-textbooks/Scripts/python scripts/pipelines/textbooks/convert.py --src <pdf> [--out <dir>]

## 首版范围
单文档、无/低质文本层扫描件走 OCR 主路。分块/批量/Opus 审查/HTML 复核待后续。
