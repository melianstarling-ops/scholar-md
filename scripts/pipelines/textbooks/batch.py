"""batch.py — textbooks 管线批量入口(自适应输入/输出,watchdog 子进程隔离)。

用法:
    python -m scripts.pipelines.textbooks.batch --src <dir_or_pdf> [...] --out <dir>
    python -m scripts.pipelines.textbooks.batch --list
    python -m scripts.pipelines.textbooks.batch --resume --max-restarts 80

--src 省略 → 回退 env SCHOLARMD_TEXTBOOKS_SRC → 仓库内 02_Source/textbooks/。
--out 省略 → 仓库内 03_Output/textbooks/(独立产物根,与单文件 convert.py"--out 省略=就地"不同)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.katex_scan import scan_katex
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.power import keep_system_awake
from scripts.pipelines.textbooks.watchdog import run_until_done

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
            stem_key = pdf.stem.casefold()
            if stem_key in stem_sources and stem_sources[stem_key] != pdf:
                raise ValueError(
                    f"跨目录同名 stem 冲突: '{pdf.stem}' 同时来自 "
                    f"{stem_sources[stem_key]} 和 {pdf}"
                )
            stem_sources[stem_key] = pdf
            pdfs.append(pdf)
    return pdfs


def _already_done(out_root: Path, work_root: Path | None, pdf_path: Path, dpi: int) -> bool:
    """--resume 跳过判断:B 路(born-digital 登记)不走这个函数,由 main 直接不做短路
    (triage 便宜、幂等,见设计 §6)。这里只判 A/C 路:指纹/DPI 失配不算 done;
    毒页(process-killed)不算"未完成"(convert_pdf 自己也不会再碰它),
    但瞬时失败页(page-exception)仍算未完成,允许下次 --resume 重试。
    """
    layout = resolve_layout(pdf_path.stem, str(out_root),
                            str(work_root) if work_root else None)
    manifest = cp.load_manifest(layout.work_dir)
    if manifest is None:
        return False
    if not cp.fingerprint_ok(manifest, str(pdf_path), dpi):
        return False
    total = manifest["fingerprint"]["page_count"]
    poisoned = {f["page"] for f in manifest["failed_pages"] if f["kind"] == "process-killed"}
    todo = [p for p in cp.pages_todo(layout.work_dir, total) if p not in poisoned]
    return not todo


def _job_argv(pdf: Path, out_root: Path, work_root: Path | None, dpi: int,
              no_selfcheck_json: bool, allow_sleep: bool = False) -> list[str]:
    argv = ["--src", str(pdf), "--out", str(out_root), "--dpi", str(dpi)]
    if work_root:
        argv.extend(["--work-dir", str(work_root)])
    if no_selfcheck_json:
        argv.append("--no-selfcheck-json")
    if allow_sleep:
        argv.append("--allow-sleep")
    return argv


def _read_summary(out_root: Path, work_root: Path | None, pdf: Path) -> dict:
    """跑完一本书(rc==0)后从磁盘读回结构化结果,供汇总报告用(拿不到 Python 返回值)。"""
    deferred_marker = out_root / "_deferred_born_digital" / f"{pdf.stem}.txt"
    if deferred_marker.exists():
        return {"stem": pdf.stem, "status": "B", "route": "B",
                "failed_pages": 0, "selfcheck": None}
    layout = resolve_layout(pdf.stem, str(out_root),
                            str(work_root) if work_root else None)
    manifest = cp.load_manifest(layout.work_dir)
    failed_pages = manifest["failed_pages"] if manifest else []
    route = manifest["route"] if manifest else "?"
    selfcheck = None
    if os.path.exists(layout.selfcheck_path):
        with open(layout.selfcheck_path, encoding="utf-8") as f:
            selfcheck = json.load(f)
    status = "SUSPECT" if failed_pages else "OK"
    return {"stem": pdf.stem, "status": status, "route": route,
            "failed_pages": len(failed_pages), "selfcheck": selfcheck}


def run(src_paths: list[str], out: str | None = None, dpi: int = cp.DEFAULT_DPI,
        work_dir: str | None = None, resume: bool = False, limit: int | None = None,
        max_restarts: int = cp.MAX_RESTARTS, no_selfcheck_json: bool = False,
        katex_scan_enabled: bool = True, allow_sleep: bool = False,
        runner=None) -> tuple[int, list[dict]]:
    pdfs = discover(src_paths)
    if limit is not None:
        pdfs = pdfs[:limit]
    out_root = Path(out).resolve() if out else DEFAULT_OUTPUT_ROOT
    work_root = Path(work_dir).resolve() if work_dir else None
    out_root.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    n_giveup = 0
    for pdf in pdfs:
        skip = False
        if resume:
            try:
                skip = _already_done(out_root, work_root, pdf, dpi)
            except Exception as e:
                print(f"  [WARN] {pdf.stem}: --resume 指纹校验失败"
                      f"({type(e).__name__}: {e}),按未完成处理")
        if skip:
            print(f"  [SKIP] {pdf.stem}")
            results.append({"stem": pdf.stem, "status": "SKIP",
                             "route": None, "failed_pages": 0, "selfcheck": None})
            continue
        argv = _job_argv(pdf, out_root, work_root, dpi, no_selfcheck_json, allow_sleep)
        rc = run_until_done(argv, max_restarts=max_restarts, runner=runner)
        if rc != 0:
            n_giveup += 1
            print(f"  [GIVEUP] {pdf.stem}")
            results.append({"stem": pdf.stem, "status": "GIVEUP",
                             "route": None, "failed_pages": 0, "selfcheck": None})
            continue
        summary = _read_summary(out_root, work_root, pdf)
        if katex_scan_enabled and summary["status"] != "B":
            layout = resolve_layout(pdf.stem, str(out_root),
                                    str(work_root) if work_root else None)
            if scan_katex(layout.md_path, layout.render_errors_path) is None:
                print(f"[katex] node 缺失,跳过 {pdf.stem}")
        results.append(summary)
        if summary["status"] == "B":
            print(f"  [B] {pdf.stem} — 已登记 deferred")
        else:
            cov = ""
            if summary["selfcheck"]:
                c = summary["selfcheck"]
                cov = f" coverage={c['in_md']}/{c['total']}"
            print(f"  [{summary['status']}] {pdf.stem} — route={summary['route']} "
                  f"failed_pages={summary['failed_pages']}{cov}")

    n_ok = sum(1 for r in results if r["status"] in ("OK", "B"))
    n_suspect = sum(1 for r in results if r["status"] == "SUSPECT")
    n_skip = sum(1 for r in results if r["status"] == "SKIP")
    print(f"\n{'=' * 56}\n批处理完成: {n_ok} OK/B / {n_suspect} SUSPECT / "
          f"{n_giveup} GIVEUP / {n_skip} SKIP → {out_root}")
    return (1 if n_giveup else 0), results


def main() -> int:
    ap = argparse.ArgumentParser(description="textbooks 批量入口(自适应 --src/--out,watchdog 子进程隔离)")
    ap.add_argument("--src", nargs="*", default=None,
                    help="PDF 文件/目录/多个;省略回退 env SCHOLARMD_TEXTBOOKS_SRC 或仓库 02_Source/textbooks/")
    ap.add_argument("--out", default=None, help="产物根目录(省略=仓库 03_Output/textbooks/)")
    ap.add_argument("--work-dir", default=None, help="过程根(默认 <out>/_work_root)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--resume", action="store_true", help="跳过已全部跑完的书")
    ap.add_argument("--limit", type=int, default=None, help="只处理发现列表的前 N 本(调试/小样验证)")
    ap.add_argument("--max-restarts", type=int, default=cp.MAX_RESTARTS,
                    help="透传给每本书 watchdog 的累计重启上限")
    ap.add_argument("--no-selfcheck-json", action="store_true", help="不写 <stem>_selfcheck.json")
    ap.add_argument("--no-katex-scan", action="store_true", help="转换成功后不运行 KaTeX 硬报错扫描")
    ap.add_argument("--allow-sleep", action="store_true",
                    help="允许系统按电源计划睡眠(默认转换期间阻止睡眠)")
    ap.add_argument("--list", action="store_true", help="只列出待处理 PDF,不转换")
    args = ap.parse_args()

    src_paths = args.src if args.src else [str(DEFAULT_SOURCE_ROOT)]
    try:
        if args.list:
            pdfs = discover(src_paths)
            if args.limit is not None:
                pdfs = pdfs[:args.limit]
            for p in pdfs:
                print(f"  {p}")
            print(f"共 {len(pdfs)} 份 @ {src_paths}")
            return 0
        with keep_system_awake(enabled=not args.allow_sleep):
            rc, _ = run(src_paths, out=args.out, dpi=args.dpi, work_dir=args.work_dir,
                        resume=args.resume,
                        limit=args.limit, max_restarts=args.max_restarts,
                        no_selfcheck_json=args.no_selfcheck_json,
                        katex_scan_enabled=not args.no_katex_scan,
                        allow_sleep=args.allow_sleep)
        return rc
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
