#!/usr/bin/env python3
"""batch_patents.py — 批量把美国专利 PDF 转为结构化 Markdown（确定性几何引擎）。

适用：美国授权专利（born-digital，带文字层，双栏+中央行号）。
主转换零成本、确定性、无幻觉；Tier0 自检常开；Tier1 云端 AI 审查可选(--review)。

用法:
    python scripts/patents/batch_patents.py
    python scripts/patents/batch_patents.py --list
    python scripts/patents/batch_patents.py --resume
    python scripts/patents/batch_patents.py --ocr     # 一键:前置 OCR 夹层产线(ocr_layer)
    python scripts/patents/batch_patents.py --review --review-model anthropic:claude-haiku-4-5
    # 外部工作区调用(产物落自己目录,本仓零污染):
    python scripts/patents/batch_patents.py --ocr --src <外部PDF夹> --out <外部产物夹>

--ocr(2026-06-12):对每件按需前置 OCR——无文本层页补层 + 正文坏字形页自动定点重
OCR(混合策略,文献页不动)。需要时产派生夹层 03_Output/patents/<stem>/_ocr/<stem>.pdf
(带 provenance,源 PDF 只读不动)并以其为转换输入;无需 OCR 的件直接喂原件,零额外开销。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from convert_patent import convert
from profiles import get_profile

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# 输入/输出根的默认值。外部调用用命令行 --src / --output-root 显式指定,
# 即可让产物完全落到外部目录、不碰本仓 03_Output(零污染、两工作区解耦)。
#   --src 省略时回退环境变量 SCHOLARMD_PATENTS_SRC,再回退仓库内 02_Source/patents/。
#   --output-root 省略时落仓库内 03_Output/patents/(本地开发的默认行为)。
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_PATENTS_SRC", str(PROJECT_ROOT / "02_Source" / "patents"))
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "patents"


def discover(source_root: Path) -> list[Path]:
    return sorted(source_root.glob("*.pdf"))


def _drop_ocr_sandwich(ocr_dir: Path) -> None:
    """删 OCR 中间件夹层 <stem>/_ocr/(转换的一次性输入,不算最终产物——
    产物夹只留 md + 图片 + selfcheck)。Windows/OneDrive 偶有句柄或同步锁,
    退避重试;最终仍失败则静默留存,不致命、不中断批处理。"""
    for i in range(3):
        try:
            shutil.rmtree(ocr_dir)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == 2:
                return
            time.sleep(0.4 * (i + 1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="只列出待处理 PDF")
    ap.add_argument("--resume", action="store_true", help="跳过已存在输出")
    ap.add_argument("--ocr", action="store_true",
                    help="前置 OCR 夹层产线(无层页补层+正文坏字形页自动定点重 OCR)")
    ap.add_argument("--src", type=Path, default=None,
                    help="PDF 源目录(省略=env SCHOLARMD_PATENTS_SRC 或仓库 02_Source/patents)")
    ap.add_argument("--out", type=Path, default=None,
                    help="产物根目录(省略=仓库 03_Output/patents);外部调用指向自己目录即零污染")
    ap.add_argument("--flat", action="store_true",
                    help="平铺模式:md/artifacts 直接落 --out 根目录(默认每篇建子目录)")
    ap.add_argument("--no-selfcheck-json", action="store_true",
                    help="不写 _selfcheck.json(控制台摘要仍输出)")
    ap.add_argument("--review", action="store_true", help="转换后跑 Tier1 云端 AI 审查")
    ap.add_argument("--review-model", default="anthropic:claude-haiku-4-5")
    ap.add_argument("--review-max-pages", type=int, default=3)
    args = ap.parse_args()

    source_root = (args.src or DEFAULT_SOURCE_ROOT).resolve()
    output_root = (args.out or DEFAULT_OUTPUT_ROOT).resolve()

    pdfs = discover(source_root)
    if args.list:
        for p in pdfs:
            print(f"  {p.name}")
        print(f"共 {len(pdfs)} 份 @ {source_root}")
        return 0

    profile = get_profile()
    print(f"[{datetime.now():%H:%M:%S}] 转换 {len(pdfs)} 份专利 → {output_root}\n")
    ok = suspect = failed = 0
    for p in pdfs:
        out_dir = output_root if args.flat else output_root / p.stem
        if args.resume and (out_dir / f"{p.stem}.md").exists():
            print(f"  [SKIP] {p.stem}")
            continue
        ocr_dir = out_dir / "_ocr"
        try:
            src_pdf = p
            if args.ocr:
                import ocr_layer
                sandwich = ocr_layer.prepare_sandwich(p, ocr_dir)
                if sandwich:
                    print(f"  [OCR] {p.stem} → {sandwich.relative_to(output_root)}")
                    src_pdf = sandwich
            r = convert(src_pdf, out_dir, profile,
                        write_selfcheck=not args.no_selfcheck_json)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {p.stem}: {e}")
            continue
        finally:
            # OCR 中间件转完即弃:最终产物夹只留 md + 图片 + selfcheck
            if args.ocr:
                _drop_ocr_sandwich(ocr_dir)
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
            out_dir = output_root if args.flat else output_root / p.stem
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
