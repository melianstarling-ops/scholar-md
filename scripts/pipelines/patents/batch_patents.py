#!/usr/bin/env python3
"""batch_patents.py — 批量把美国专利 PDF 转为结构化 Markdown（确定性几何引擎）。

适用：美国授权专利（born-digital，带文字层，双栏+中央行号）。
主转换零成本、确定性、无幻觉；Tier0 自检常开；Tier1 云端 AI 审查可选(--review)。

用法:
    python scripts/patents/batch_patents.py
    python scripts/patents/batch_patents.py --list
    python scripts/patents/batch_patents.py --resume
    python scripts/patents/batch_patents.py --review --review-model anthropic:claude-haiku-4-5
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from convert_patent import convert
from profiles import get_profile

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# 专利 PDF 源目录,用环境变量配置(默认仓库内 02_Source/patents/)
#   SCHOLARMD_PATENTS_SRC
SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_PATENTS_SRC", str(PROJECT_ROOT / "02_Source" / "patents"))
)
OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "patents"


def discover() -> list[Path]:
    return sorted(SOURCE_ROOT.glob("*.pdf"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="只列出待处理 PDF")
    ap.add_argument("--resume", action="store_true", help="跳过已存在输出")
    ap.add_argument("--review", action="store_true", help="转换后跑 Tier1 云端 AI 审查")
    ap.add_argument("--review-model", default="anthropic:claude-haiku-4-5")
    ap.add_argument("--review-max-pages", type=int, default=3)
    args = ap.parse_args()

    pdfs = discover()
    if args.list:
        for p in pdfs:
            print(f"  {p.name}")
        print(f"共 {len(pdfs)} 份 @ {SOURCE_ROOT}")
        return 0

    profile = get_profile()
    print(f"[{datetime.now():%H:%M:%S}] 转换 {len(pdfs)} 份专利 → {OUTPUT_ROOT}\n")
    ok = suspect = failed = 0
    for p in pdfs:
        out_dir = OUTPUT_ROOT / p.stem
        if args.resume and (out_dir / f"{p.stem}.md").exists():
            print(f"  [SKIP] {p.stem}")
            continue
        try:
            r = convert(p, out_dir, profile)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {p.stem}: {e}")
            continue
        cov = r.selfcheck["coverage"]
        flag = "OK" if r.selfcheck["passed"] else "SUSPECT"
        if r.selfcheck["passed"]:
            ok += 1
        else:
            suspect += 1
        print(f"  [{flag}] {p.stem} — body={r.n_body_pages} figs={r.n_figures} "
              f"missing={cov['missing_ratio']:.4f} issues={len(r.selfcheck['issues'])}")
        for it in r.selfcheck["issues"][:5]:
            print(f"        · {it}")

    print(f"\n{'='*56}\n转换完成: {ok} OK / {suspect} 需复核 / {failed} 失败")

    if args.review:
        import ai_review
        print(f"\n[Tier1] AI 审查（{args.review_model}）…")
        for p in pdfs:
            out_dir = OUTPUT_ROOT / p.stem
            if not (out_dir / f"{p.stem}.md").exists():
                continue
            try:
                rep = ai_review.review_document(p, out_dir, args.review_model, args.review_max_pages)
                n = sum(len(r["issues"]) for r in rep["results"])
                print(f"  [review] {p.stem} — {n} 条 → {out_dir / (p.stem + '_review.md')}")
            except Exception as e:  # noqa: BLE001
                print(f"  [review-ERROR] {p.stem}: {e}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
