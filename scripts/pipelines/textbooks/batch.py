"""batch.py — textbooks 管线批量入口(自适应输入/输出,watchdog 子进程隔离)。

用法:
    python -m scripts.pipelines.textbooks.batch --src <dir_or_pdf> [...] --out <dir>
    python -m scripts.pipelines.textbooks.batch --list
    python -m scripts.pipelines.textbooks.batch --resume --max-restarts 80

--src 省略 → 回退 env SCHOLARMD_TEXTBOOKS_SRC → 仓库内 02_Source/textbooks/。
--out 省略 → 仓库内 03_Output/textbooks/(独立产物根,与单文件 convert.py"--out 省略=就地"不同)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_TEXTBOOKS_SRC", str(PROJECT_ROOT / "02_Source" / "textbooks"))
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "textbooks"


def discover(src_paths: list[str]) -> list[Path]:
    """把 --src(文件/目录/多个)展开成去重排序的 PDF 路径列表。

    跨目录同名 stem(不同路径、同文件名)会导致 out_root/<stem>/ 下的检查点互相清空打架,
    属正确性问题,检出即抛 ValueError,调用方(main)应捕获后整批不处理直接返回非零。
    """
    pdfs: list[Path] = []
    seen: set[Path] = set()
    stem_sources: dict[str, Path] = {}
    for sp in src_paths:
        p = Path(sp).resolve()
        if p.is_dir():
            candidates = sorted(p.glob("*.pdf"))
        elif p.is_file() and p.suffix.lower() == ".pdf":
            candidates = [p]
        else:
            print(f"  跳过(既非 PDF 文件也非目录): {p}", file=sys.stderr)
            continue
        for pdf in candidates:
            if pdf in seen:
                continue
            seen.add(pdf)
            if pdf.stem in stem_sources and stem_sources[pdf.stem] != pdf:
                raise ValueError(
                    f"跨目录同名 stem 冲突: '{pdf.stem}' 同时来自 "
                    f"{stem_sources[pdf.stem]} 和 {pdf}"
                )
            stem_sources[pdf.stem] = pdf
            pdfs.append(pdf)
    return pdfs
