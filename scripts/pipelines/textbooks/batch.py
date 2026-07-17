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
from scripts.pipelines.textbooks import katex_triage
from scripts.pipelines.textbooks.katex_scan import scan_katex_work_pages
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.power import keep_system_awake
from scripts.pipelines.textbooks.watchdog import run_until_done

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_TEXTBOOKS_SRC", str(PROJECT_ROOT / "02_Source" / "textbooks"))
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "textbooks"

# Task 10:batch 汇总的 source audit 分级(计划 §7.2/Task 10 checklist)。severe
# 清单逐字取自计划:adoption_error/audit_error/numeric 类/sign_flip/decimal_shift/
# exponent_change;其余 issue code 一律记为 mild。分级清单/打印页数上限均为注入
# 参数——这里给出的是占位默认值,真实生产值待 Task 13 用样书语料标定后再调整,
# 不当已标定生产阈值直接使用。
DEFAULT_SEVERE_ISSUE_CODES = frozenset({
    "adoption_error", "audit_error",
    "numeric_mismatch", "numeric_missing",
    "sign_flip", "decimal_shift", "exponent_change",
})

# 批处理摘要打印的 suspect 页码上限:只列页码 + issue 类别名,绝不打印审计报告
# 原文/源文本本身,避免刷屏也避免间接泄露敏感源文本。
DEFAULT_SUSPECT_PRINT_LIMIT = 5


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
              no_selfcheck_json: bool, allow_sleep: bool = False,
              force_ocr: bool = False, work_hours: float = 6,
              rest_minutes: float = 40) -> list[str]:
    argv = ["--src", str(pdf), "--out", str(out_root), "--dpi", str(dpi)]
    if work_root:
        argv.extend(["--work-dir", str(work_root)])
    if no_selfcheck_json:
        argv.append("--no-selfcheck-json")
    if force_ocr:
        argv.append("--force-ocr")
    argv.extend(["--work-hours", str(work_hours),
                 "--rest-minutes", str(rest_minutes)])
    if allow_sleep:
        argv.append("--allow-sleep")
    return argv


def _grade_source_audit(source_audit: dict | None, total_pages: int, *,
                        severe_issue_codes: frozenset = DEFAULT_SEVERE_ISSUE_CODES,
                        ) -> dict | None:
    """把 selfcheck.json 的紧凑 source_audit 字段(Task 10,计划 §7.2)转成 batch
    汇总/分级所需信息:suspect 页率(suspect_pages/pages)+ severe/mild issue 计数。
    source_audit 缺席(旧式 selfcheck.json,或 convert 从未跑过审计)→ None,
    不伪造一份分级出来。"""
    if not source_audit:
        return None
    suspect_pages = list(source_audit.get("suspect_pages") or [])
    issue_counts = dict(source_audit.get("issue_counts") or {})
    severe = sum(n for code, n in issue_counts.items() if code in severe_issue_codes)
    mild = sum(n for code, n in issue_counts.items() if code not in severe_issue_codes)
    return {
        "status": source_audit.get("status"),
        "suspect_pages": suspect_pages,
        "suspect_page_count": len(suspect_pages),
        "pages": total_pages,
        "suspect_page_rate": (len(suspect_pages) / total_pages) if total_pages else 0.0,
        "severe_issue_count": severe,
        "mild_issue_count": mild,
        "issue_counts": issue_counts,
    }


def _read_summary(out_root: Path, work_root: Path | None, pdf: Path, *,
                  severe_issue_codes: frozenset = DEFAULT_SEVERE_ISSUE_CODES) -> dict:
    """跑完一本书(rc==0)后从磁盘读回结构化结果,供汇总报告用(拿不到 Python 返回值)。

    文档状态综合 failed_pages / selfcheck / source audit 三者:有产物但 audit
    判 SUSPECT(即便 failed_pages 为空)也不能计入 OK(Task 10)。"""
    deferred_marker = out_root / "_deferred_born_digital" / f"{pdf.stem}.txt"
    if deferred_marker.exists():
        return {"stem": pdf.stem, "status": "B", "route": "B",
                "failed_pages": 0, "selfcheck": None, "source_audit_grade": None}
    layout = resolve_layout(pdf.stem, str(out_root),
                            str(work_root) if work_root else None)
    manifest = cp.load_manifest(layout.work_dir)
    failed_pages = manifest["failed_pages"] if manifest else []
    route = manifest["route"] if manifest else "?"
    total_pages = manifest["fingerprint"]["page_count"] if manifest else 0
    selfcheck = None
    if os.path.exists(layout.selfcheck_path):
        with open(layout.selfcheck_path, encoding="utf-8") as f:
            selfcheck = json.load(f)
    grade = _grade_source_audit((selfcheck or {}).get("source_audit"), total_pages,
                                severe_issue_codes=severe_issue_codes)
    status = "SUSPECT" if failed_pages else "OK"
    if grade and grade["status"] == "SUSPECT":
        status = "SUSPECT"
    return {"stem": pdf.stem, "status": status, "route": route,
            "failed_pages": len(failed_pages), "selfcheck": selfcheck,
            "source_audit_grade": grade}


def _format_audit_grade(grade: dict | None, *,
                        limit: int = DEFAULT_SUSPECT_PRINT_LIMIT) -> str:
    """batch 摘要行的 source audit 分级片段(Task 10):只列页码与 issue 类别名
    (上限截断),绝不打印审计报告原文/源文本本身。分级/OK 且没有任何异常时返回
    空串,保证 A 路(NOT_APPLICABLE,零计数)摘要行与改动前逐字节一致。"""
    if not grade:
        return ""
    if not (grade["suspect_page_count"] or grade["severe_issue_count"] or grade["mild_issue_count"]):
        return ""
    shown = grade["suspect_pages"][:limit]
    remaining = grade["suspect_page_count"] - len(shown)
    pages_str = ",".join(str(p) for p in shown)
    if remaining > 0:
        pages_str += f",+{remaining}more"
    categories = ",".join(sorted(grade["issue_counts"]))
    return (f" audit={grade['status']} suspect={grade['suspect_page_count']}/{grade['pages']}"
            f"({grade['suspect_page_rate']:.1%}) severe={grade['severe_issue_count']} "
            f"mild={grade['mild_issue_count']} pages=[{pages_str}] issues=[{categories}]")


def run(src_paths: list[str], out: str | None = None, dpi: int = cp.DEFAULT_DPI,
        work_dir: str | None = None, resume: bool = False, limit: int | None = None,
        max_restarts: int = cp.MAX_RESTARTS, no_selfcheck_json: bool = False,
        katex_scan_enabled: bool = True, allow_sleep: bool = False,
        force_ocr: bool = False, work_hours: float = 6,
        rest_minutes: float = 40, runner=None,
        severe_issue_codes: frozenset = DEFAULT_SEVERE_ISSUE_CODES,
        suspect_print_limit: int = DEFAULT_SUSPECT_PRINT_LIMIT) -> tuple[int, list[dict]]:
    if work_hours <= 0 or rest_minutes <= 0:
        raise ValueError("work_hours 与 rest_minutes 必须大于 0")
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
            results.append({"stem": pdf.stem, "status": "SKIP", "route": None,
                             "failed_pages": 0, "selfcheck": None, "source_audit_grade": None})
            continue
        argv = _job_argv(pdf, out_root, work_root, dpi, no_selfcheck_json, allow_sleep,
                         force_ocr, work_hours, rest_minutes)
        rc = run_until_done(argv, max_restarts=max_restarts, runner=runner)
        if rc != 0:
            n_giveup += 1
            print(f"  [GIVEUP] {pdf.stem}")
            results.append({"stem": pdf.stem, "status": "GIVEUP", "route": None,
                             "failed_pages": 0, "selfcheck": None, "source_audit_grade": None})
            continue
        summary = _read_summary(out_root, work_root, pdf, severe_issue_codes=severe_issue_codes)
        if katex_scan_enabled and summary["status"] != "B":
            layout = resolve_layout(pdf.stem, str(out_root),
                                    str(work_root) if work_root else None)
            try:
                katex_result = scan_katex_work_pages(layout, layout.render_errors_path)
            except ValueError as e:
                print(f"[katex] 检查点不完整,跳过 {pdf.stem}: {e}")
                katex_result = {}
            if katex_result is None:
                print(f"[katex] node 缺失,跳过 {pdf.stem}")
            elif katex_result:
                # 硬错分桶 + 视觉工单(SOP-09):有硬错时打印各桶 + 落工单,指引后续修复
                try:
                    katex_triage.report_for_batch(layout, katex_result)
                except Exception as e:
                    print(f"[triage] 分桶跳过 {pdf.stem}: {e}")
        results.append(summary)
        if summary["status"] == "B":
            print(f"  [B] {pdf.stem} — 已登记 deferred")
        else:
            cov = ""
            if summary["selfcheck"]:
                c = summary["selfcheck"]
                cov = f" coverage={c['in_md']}/{c['total']}"
            audit_line = _format_audit_grade(summary.get("source_audit_grade"),
                                             limit=suspect_print_limit)
            print(f"  [{summary['status']}] {pdf.stem} — route={summary['route']} "
                  f"failed_pages={summary['failed_pages']}{cov}{audit_line}")

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
    ap.add_argument("--force-ocr", action="store_true",
                    help="忽略优质文本层并强制逐页栅格化 OCR")
    ap.add_argument("--work-hours", type=float, default=6,
                    help="每轮连续 OCR 时长(小时，默认6)")
    ap.add_argument("--rest-minutes", type=float, default=40,
                    help="每轮结束后的 GPU 空闲时长(分钟，默认40)")
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
    if args.work_hours <= 0 or args.rest_minutes <= 0:
        ap.error("--work-hours 与 --rest-minutes 必须大于 0")

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
                        allow_sleep=args.allow_sleep,
                        force_ocr=args.force_ocr,
                        work_hours=args.work_hours,
                        rest_minutes=args.rest_minutes)
        return rc
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
