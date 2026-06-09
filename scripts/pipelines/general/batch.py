#!/usr/bin/env python3
"""
batch.py — general 管线一键入口(自适应输入/输出)。

流程:  收集 PDF(文件/目录/多个)
          → ① Marker 转换(born-digital, 文本层 + 版面识别)
          → ② Typora 重排(md 提根 + 图入 <name>.assets/ + 路径改写)
          → ③ Tier0 自检(SOP-03: 内容覆盖校验 + 形态检查)+ 写报告

自适应:
  --src 接受 文件 或 目录、单个 或 多个;目录自动扫 *.pdf。
  --out 省略 → 每份产物落到其 PDF 所在目录;指定 → 统一到该目录。
  不预设/写死任何输入输出路径。源 PDF 只读,绝不改动。

用法:
    # 目录(扫全部 *.pdf),产物就地
    python batch.py --src "D:/.../References"
    # 指定若干 PDF 文件,产物各自就地
    python batch.py --src "a.pdf" "b.pdf"
    # 多源 + 统一输出目录;调试只跑前 N 份;扫描版降级
    python batch.py --src "dir1" "x.pdf" --out "D:/out"
    python batch.py --src "dir" --limit 1
    python batch.py --src "dir" --force-ocr
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert as _convert          # noqa: E402
import typora_layout as _typora     # noqa: E402
import selfcheck as _selfcheck      # noqa: E402


def collect_jobs(src_paths, out=None) -> list[tuple[Path, Path]]:
    """把 src(文件/目录/多个)展开成 [(pdf, out_dir)]。

    out 为 None → 每份落到其 PDF 所在目录;否则统一到 out。
    """
    out_dir = Path(out).resolve() if out else None
    jobs: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for sp in src_paths:
        p = Path(sp).resolve()
        if p.is_dir():
            for pdf in sorted(p.glob("*.pdf")):
                if pdf not in seen:
                    seen.add(pdf)
                    jobs.append((pdf, out_dir or p))
        elif p.is_file() and p.suffix.lower() == ".pdf":
            if p not in seen:
                seen.add(p)
                jobs.append((p, out_dir or p.parent))
        else:
            print(f"  ⚠️ 跳过(既非 PDF 文件也非目录): {p}", file=sys.stderr)
    return jobs


def run(src_paths, out=None, dpi: int = 300, force_ocr: bool = False, limit: int | None = None) -> int:
    jobs = collect_jobs(src_paths, out)
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        print("未发现可转换的 PDF")
        return 0

    # ① 转换
    print(f"=== ① Marker 转换 · 共 {len(jobs)} 份 ===")
    n_fail = 0
    for i, (pdf, od) in enumerate(jobs, 1):
        od.mkdir(parents=True, exist_ok=True)
        print(f"\n[{i}/{len(jobs)}] {pdf.name}  ->  {od}")
        r = _convert.convert_pdf(pdf, od, dpi=dpi, force_ocr=force_ocr)
        if not r["success"]:
            n_fail += 1
            print(f"  ❌ 转换失败 rc={r['returncode']}", file=sys.stderr)

    out_dirs = sorted({od for _, od in jobs}, key=str)

    # ② Typora 重排(逐输出目录)
    print("\n=== ② Typora 重排 ===")
    for od in out_dirs:
        for folder, md in _typora.find_marker_outputs(od):
            res = _typora.relayout_one(folder, md, od)
            print(f"  [完成] {res.name}  (图 {res.n_images_moved}, 改写引用 {res.n_refs_rewritten})")
            for s in res.skipped:
                print(f"         ⚠️ {s}")

    # ③ Tier0 自检(逐输出目录,各写报告)
    print("\n=== ③ Tier0 自检(SOP-03) ===")
    n_err = 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for od in out_dirs:
        per_file = {md.name: _selfcheck.check_one(md, od) for md in sorted(od.glob("*.md"))}
        findings = [f for fs in per_file.values() for f in fs]
        errs = [f for f in findings if f.severity == "error"]
        n_err += len(errs)
        status = "✅ 通过" if not findings else f"{len(errs)} error / {len(findings) - len(errs)} warn"
        print(f"  · {od}: {status}")
        for f in findings:
            print(f"      {f}")
        logdir = od / "_selfcheck"
        logdir.mkdir(parents=True, exist_ok=True)
        (logdir / f"{stamp}_tier0.md").write_text(_selfcheck.build_report(per_file), encoding="utf-8")

    print(f"\n=== 汇总: 转换失败 {n_fail} · Tier0 错误 {n_err} · 输出目录 {len(out_dirs)} ===")
    return 1 if (n_fail or n_err) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="general 管线一键(自适应):转换 → Typora 重排 → Tier0 自检")
    ap.add_argument("--src", "-s", nargs="+", required=True,
                    help="一个或多个 PDF 文件 或 目录(目录自动扫 *.pdf)")
    ap.add_argument("--out", "-o", help="统一产物目录;省略则每份落到其 PDF 所在目录")
    ap.add_argument("--dpi", type=int, default=300, help="提取图片分辨率(默认 300)")
    ap.add_argument("--force-ocr", action="store_true", help="强制 OCR(仅扫描版)")
    ap.add_argument("--limit", type=int, help="只处理前 N 份(调试用)")
    args = ap.parse_args()
    return run(args.src, args.out, dpi=args.dpi, force_ocr=args.force_ocr, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
