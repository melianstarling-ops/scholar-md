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
import hashlib
import json
import os
import sys
from pathlib import Path

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import katex_triage
from scripts.pipelines.textbooks.katex_scan import scan_katex_work_pages
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.power import keep_system_awake
from scripts.pipelines.textbooks.repair_policy import (
    CompletionStatus,
    FORMULA_REPAIR_MODES,
    QUALITY_REPAIR_MODES,
    RepairPolicy,
    add_repair_policy_arguments,
    quality_final_is_conclusive,
    repair_policy_from_namespace,
    source_audit_blocks_completion,
)
from scripts.pipelines.textbooks.selfcheck import build_source_audit_field
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

# audit 报告 schema 版本——与 convert.py/source_audit.py 的 schema_version 同步维护
# (独立常量,不跨模块 import convert.py 制造不必要耦合;风格同 selfcheck.py 里类似的
# "只读参考,独立维护"惯例)。
AUDIT_SCHEMA_VERSION = 6

# 与 convert.py 的 BORN_DIGITAL_MODES 同步维护(独立常量,同上惯例)。
BORN_DIGITAL_MODES = ("defer", "ocr", "hybrid")

# 与 convert.py 的 FORMULA_REPAIR_MODES 同步维护(独立常量,同上惯例)。
#
# 三入口默认统一为 agents-apply。batch 将该值透传给 convert.py，并在
# formula_repair != "off" 时关闭自己早期遗留的 katex_scan/katex_triage 收尾，
# 保证同一本书只跑一遍公式后处理。只有显式选择 off 时才走 batch 遗留收尾。
QUALITY_DISCOVERY_MODES = ("off", "signals")
QUALITY_LEARN_MODES = ("off", "package")

# selfcheck.json 里紧凑 source_audit 字段做分级要用到的最小键集合——用来判断该字段
# 是否结构完好,而非"字段存在就信"(Review Important 1:字段存在但类型/结构损坏时,
# 必须转磁盘兜底读取,不能悄悄漏报)。
_COMPACT_AUDIT_REQUIRED_KEYS = {"status", "suspect_pages", "issue_counts"}


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


def _repair_stages_complete(
        layout, *, formula_repair: str, quality_repair: str) -> bool:
    """Whether --resume may skip post-processing as well as OCR checkpoints."""
    if formula_repair != "off":
        if not os.path.exists(layout.formula_repair_path):
            return False
        try:
            with open(layout.formula_repair_path, encoding="utf-8") as handle:
                formula = json.load(handle)
        except Exception:                           # noqa: BLE001 corrupt sidecar => rerun
            return False
        if not isinstance(formula, dict) or formula.get("status") == "error":
            return False
        candidates = int(
            (formula.get("formula_candidates") or {}).get("count") or 0)
        if formula.get("mode") == "deterministic" and candidates:
            return False
        agents = formula.get("agents")
        if formula_repair in {"agents", "agents-apply"}:
            if not isinstance(agents, dict) or agents.get("status") != "ok":
                return False
            if (agents.get("pending_ids") or agents.get("circuit_broken")
                    or agents.get("rolled_back")
                    or int(agents.get("rejected") or 0) > 0):
                return False
            if (formula_repair == "agents-apply" and candidates
                    and agents.get("run_mode") != "apply"):
                return False

    if quality_repair == "off":
        return True
    source_audit = _fresh_source_audit_for_resume(layout)
    if source_audit is None:
        return False
    quality = _read_quality_repair_result(layout)
    if quality is None or quality.get("status") == "error":
        return False
    expected_hash = quality.get("after_sha256")
    if not isinstance(expected_hash, str) or not os.path.isfile(layout.md_path):
        return False
    actual_hash = hashlib.sha256(Path(layout.md_path).read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        return False
    if quality_repair == "apply":
        return (
            quality_final_is_conclusive(quality)
            and not source_audit_blocks_completion(source_audit, quality)
        )
    return True


def _content_fingerprint(path: str) -> dict | None:
    try:
        payload = Path(path).read_bytes()
    except OSError:
        return None
    return {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _fresh_source_audit_for_resume(layout) -> dict | None:
    """Return the current source audit, or ``None`` when any input is stale."""

    try:
        with open(layout.source_audit_path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        return None
    if (not isinstance(report, dict)
            or report.get("schema_version") != AUDIT_SCHEMA_VERSION
            or report.get("stem") != layout.stem):
        return None

    manifest = cp.load_manifest(layout.work_dir)
    if not isinstance(manifest, dict):
        return None
    pdf_path = manifest.get("pdf_path")
    dpi = manifest.get("dpi")
    if (not isinstance(pdf_path, str) or not pdf_path
            or not isinstance(dpi, int) or isinstance(dpi, bool) or dpi <= 0):
        return None
    try:
        current_pdf = cp.pdf_fingerprint(pdf_path)
    except (OSError, RuntimeError, ValueError):
        return None
    recorded_pdf = report.get("pdf_fingerprint")
    if not isinstance(recorded_pdf, dict):
        return None
    if any(recorded_pdf.get(key) != current_pdf[key]
           for key in ("size_bytes", "page_count")):
        return None
    if "sha256" in recorded_pdf:
        current_pdf_content = _content_fingerprint(pdf_path)
        if (current_pdf_content is None
                or recorded_pdf.get("sha256")
                != current_pdf_content["sha256"]):
            return None

    current_ocr = cp.ocr_results_fingerprint(
        layout.work_dir, current_pdf["page_count"], dpi)
    if report.get("ocr_fingerprint") != current_ocr:
        return None
    if report.get("corrections_fingerprint") != _content_fingerprint(
            layout.corrections_path):
        return None
    return report


def _job_argv(pdf: Path, out_root: Path, work_root: Path | None, dpi: int,
              no_selfcheck_json: bool, allow_sleep: bool = False,
              force_ocr: bool = False, work_hours: float = 6,
              rest_minutes: float = 40, born_digital_mode: str = "hybrid",
              formula_repair: str = "deterministic", quality_repair: str = "apply",
              quality_agents: list[str] | None = None,
              quality_discovery: str = "signals", quality_learn: str = "off",
              quality_agent_timeout: int = 300,
              formula_agents: list[str] | None = None,
              repair_policy: RepairPolicy | None = None) -> list[str]:
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
    argv.extend(["--born-digital-mode", born_digital_mode])
    if repair_policy is None:
        argv.extend(["--formula-repair", formula_repair])
        argv.extend(["--quality-repair", quality_repair])
        for agent in quality_agents or []:
            argv.extend(["--quality-agent", agent])
        for agent in formula_agents or []:
            argv.extend(["--repair-agent", agent])
    else:
        argv.extend(["--repair", repair_policy.mode,
                     "--repair-workers", str(repair_policy.workers),
                     "--repair-max-rounds", str(repair_policy.max_rounds)])
        for spec in repair_policy.formula_agents:
            argv.extend(["--repair-agent", spec.to_cli()])
        if repair_policy.legacy_formula_explicit:
            argv.extend(["--formula-repair", repair_policy.formula_mode])
        if repair_policy.legacy_quality_explicit:
            argv.extend(["--quality-repair", repair_policy.quality_mode])
        if repair_policy.legacy_quality_agents_explicit:
            for spec in repair_policy.quality_agents:
                argv.extend(["--quality-agent", spec.to_cli()])
    argv.extend(["--quality-discovery", quality_discovery,
                 "--quality-learn", quality_learn,
                 "--quality-agent-timeout", str(quality_agent_timeout)])
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


def _is_valid_compact_audit(field) -> bool:
    """selfcheck.json 里 source_audit 字段的最小结构校验(不是完整 schema 校验)——
    只确保分级要用到的键都在、且是 dict。字段缺失(旧式 selfcheck.json)或结构损坏
    (非 dict/缺关键键)时返回 False,调用方据此转磁盘兜底读取,不能悄悄漏报。"""
    return isinstance(field, dict) and _COMPACT_AUDIT_REQUIRED_KEYS <= field.keys()


def _disk_audit_fallback(layout, pdf: Path, dpi: int) -> dict:
    """selfcheck.json 缺失(如 --no-selfcheck-json)或其 source_audit 字段缺失/结构
    损坏时的兜底:audit 报告由 convert 主链独立管理(Task 9 保证总会写),直接读磁盘
    上的 <stem>_source_audit.json,不能因为 selfcheck 没落盘/字段坏了就把"其实是
    SUSPECT"漏报成 OK。

    只有报告文件确实存在时才需要真实 PDF 指纹做新鲜度比对——报告不存在时
    build_source_audit_field 在比对指纹前就已短路返回 audit_report_missing,不需要
    也不应该为此打开 PDF(测试常用的占位/非法 PDF 字节在此路径下不会被触碰)。
    """
    pdf_fingerprint: dict = {}
    if os.path.exists(layout.source_audit_path):
        try:
            pdf_fingerprint = cp.pdf_fingerprint(str(pdf))
        except Exception:                          # noqa: BLE001 兜底读取不逃逸
            pdf_fingerprint = {}
    return build_source_audit_field(
        layout.source_audit_path, pdf_fingerprint, dpi, AUDIT_SCHEMA_VERSION)


def _read_summary(out_root: Path, work_root: Path | None, pdf: Path, *,
                  severe_issue_codes: frozenset = DEFAULT_SEVERE_ISSUE_CODES,
                  dpi: int = cp.DEFAULT_DPI) -> dict:
    """跑完一本书(rc==0)后从磁盘读回结构化结果,供汇总报告用(拿不到 Python 返回值)。

    文档状态综合 failed_pages / selfcheck / source audit 三者:有产物但 audit
    判 SUSPECT(即便 failed_pages 为空)也不能计入 OK(Task 10)。selfcheck.json
    缺失或其 source_audit 字段缺失/结构损坏时,直接读磁盘上的审计报告兜底
    (Review Important 1),不依赖 selfcheck.json 是否落盘。"""
    deferred_marker = out_root / "_deferred_born_digital" / f"{pdf.stem}.txt"
    if deferred_marker.exists():
        return {"stem": pdf.stem, "status": "B", "route": "B",
                "failed_pages": 0, "selfcheck": None, "source_audit_grade": None,
                "formula_repair_flag": None, "quality_repair_flag": None}
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
    source_audit_field = (selfcheck or {}).get("source_audit")
    if not _is_valid_compact_audit(source_audit_field):
        source_audit_field = _disk_audit_fallback(layout, pdf, dpi)
    grade = _grade_source_audit(source_audit_field, total_pages,
                                severe_issue_codes=severe_issue_codes)
    quality_result = _read_quality_repair_result(layout)
    status = "SUSPECT" if failed_pages else "OK"
    if (source_audit_blocks_completion(source_audit_field, quality_result)
            and (source_audit_field or {}).get("status") != "UNSCORABLE"):
        status = "SUSPECT"
    return {"stem": pdf.stem, "status": status, "route": route,
            "failed_pages": len(failed_pages), "selfcheck": selfcheck,
            "source_audit_grade": grade,
            "formula_repair_flag": _read_formula_repair_flag(layout),
            "quality_repair_flag": _read_quality_repair_flag(layout)}


def _read_formula_repair_flag(layout) -> str | None:
    """agents-apply 安全网触发事件的分诊信号(Review Important):熔断/回滚发生时,
    corrections 悄悄退回 pending、没有人工复核(agents-apply 的设计本就不留人工
    accept 门)——这是运行期间唯一能穿透到批量汇总的痕迹,不然只能靠事后翻
    子进程 stdout 或逐本翻 formula_agent_ledger.jsonl。旧书/未跑过公式修复环时
    sidecar 不存在,按"无异常"处理,不伪造。"""
    if not os.path.exists(layout.formula_repair_path):
        return None
    try:
        with open(layout.formula_repair_path, encoding="utf-8") as f:
            fr = json.load(f)
    except Exception:                              # noqa: BLE001 兜底读取不逃逸
        return None
    if not isinstance(fr, dict):
        return None
    agents = fr.get("agents")
    if not isinstance(agents, dict):
        return None
    if agents.get("circuit_broken"):
        return "circuit_broken"
    if agents.get("rolled_back"):
        return "rolled_back"
    return None


def _read_quality_repair_result(layout) -> dict | None:
    path = os.path.join(layout.quality_repair_dir, "latest.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:                              # noqa: BLE001 批量汇总不逃逸
        return {"mode": "unknown", "status": "error"}
    return data if isinstance(data, dict) else {"mode": "unknown", "status": "error"}


def _read_quality_repair_flag(layout) -> str | None:
    """读取显式 quality-repair run 的紧凑异常标志；未运行/旧书返回 None。"""
    data = _read_quality_repair_result(layout)
    if data is None:
        return None
    if data.get("mode") == "unknown" and data.get("status") == "error":
        return "report_error"
    if data.get("mode") == "off":
        return None
    if data.get("status") == "error":
        return "run_error"
    if data.get("rolled_back"):
        return "rolled_back"
    if int(data.get("conflicts") or 0) > 0:
        return "conflicts"
    counts = data.get("severity_counts") or {}
    if int(counts.get("P0") or 0) > 0:
        return "P0_findings"
    if int(counts.get("P1") or 0) > 0:
        return "P1_findings"
    reason = str(data.get("reason") or "")
    if reason and reason != "empty patch plan":
        return "apply_blocked"
    return None


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
        suspect_print_limit: int = DEFAULT_SUSPECT_PRINT_LIMIT,
        born_digital_mode: str = "hybrid",
        formula_repair: str = "deterministic", quality_repair: str = "apply",
        quality_agents: list[str] | None = None,
        quality_discovery: str = "signals", quality_learn: str = "off",
        quality_agent_timeout: int = 300,
        formula_agents: list[str] | None = None,
        repair_policy: RepairPolicy | None = None) -> tuple[int, list[dict]]:
    if work_hours <= 0 or rest_minutes <= 0:
        raise ValueError("work_hours 与 rest_minutes 必须大于 0")
    if formula_repair not in FORMULA_REPAIR_MODES:
        raise ValueError(f"formula_repair 须为 {FORMULA_REPAIR_MODES},收到 {formula_repair!r}")
    if quality_repair not in QUALITY_REPAIR_MODES:
        raise ValueError(f"quality_repair 须为 {QUALITY_REPAIR_MODES},收到 {quality_repair!r}")
    if quality_discovery not in QUALITY_DISCOVERY_MODES:
        raise ValueError(f"quality_discovery 须为 {QUALITY_DISCOVERY_MODES},收到 {quality_discovery!r}")
    if quality_learn not in QUALITY_LEARN_MODES:
        raise ValueError(f"quality_learn 须为 {QUALITY_LEARN_MODES},收到 {quality_learn!r}")
    if quality_agent_timeout <= 0:
        raise ValueError("quality_agent_timeout 必须大于 0")
    # Task B dedup(见 FORMULA_REPAIR_MODES 上方注释):formula_repair != "off" 时
    # 每本书的 convert.py 子进程会自己跑 katex_scan+katex_triage,batch 收尾这里
    # 对应步骤必须让路,不管调用方传的 katex_scan_enabled 是什么——避免双跑。
    katex_scan_enabled = katex_scan_enabled and formula_repair == "off"
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
                if skip:
                    layout = resolve_layout(
                        pdf.stem, str(out_root),
                        str(work_root) if work_root else None)
                    skip = _repair_stages_complete(
                        layout, formula_repair=formula_repair,
                        quality_repair=quality_repair)
            except Exception as e:
                print(f"  [WARN] {pdf.stem}: --resume 指纹校验失败"
                      f"({type(e).__name__}: {e}),按未完成处理")
        if skip:
            print(f"  [SKIP] {pdf.stem}")
            results.append({"stem": pdf.stem, "status": "SKIP", "route": None,
                             "failed_pages": 0, "selfcheck": None, "source_audit_grade": None,
                             "formula_repair_flag": None, "quality_repair_flag": None})
            continue
        argv = _job_argv(pdf, out_root, work_root, dpi, no_selfcheck_json, allow_sleep,
                         force_ocr, work_hours, rest_minutes, born_digital_mode,
                          formula_repair, quality_repair, quality_agents,
                          quality_discovery, quality_learn, quality_agent_timeout,
                          formula_agents, repair_policy)
        rc = run_until_done(argv, max_restarts=max_restarts, runner=runner)
        if rc == CompletionStatus.FAILED:
            n_giveup += 1
            print(f"  [GIVEUP] {pdf.stem}")
            results.append({"stem": pdf.stem, "status": "GIVEUP", "route": None,
                             "failed_pages": 0, "selfcheck": None, "source_audit_grade": None,
                             "formula_repair_flag": None, "quality_repair_flag": None,
                             "completion_status": CompletionStatus.FAILED})
            continue
        summary = _read_summary(out_root, work_root, pdf,
                                severe_issue_codes=severe_issue_codes, dpi=dpi)
        if rc == CompletionStatus.SUSPECT:
            summary["status"] = "SUSPECT"
            summary["completion_status"] = CompletionStatus.SUSPECT
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
            fr_flag = summary.get("formula_repair_flag")
            fr_line = f" fr={fr_flag}" if fr_flag else ""
            qr_flag = summary.get("quality_repair_flag")
            qr_line = f" qr={qr_flag}" if qr_flag else ""
            print(f"  [{summary['status']}] {pdf.stem} — route={summary['route']} "
                  f"failed_pages={summary['failed_pages']}{cov}{audit_line}{fr_line}{qr_line}")

    def _is_unscorable(result: dict) -> bool:
        # 审计"读不出"(报告缺失/半截/过期)不该悄悄跟"审计判 OK"共享 OK 计数——
        # rollup 顺手改进(Review):单列 UNSCORABLE,不动逐本(per-line)输出。
        grade = result.get("source_audit_grade")
        return bool(grade) and grade.get("status") == "UNSCORABLE"

    def _has_formula_repair_flag(result: dict) -> bool:
        # agents-apply 熔断/回滚同样不该悄悄计入 OK(Review Important)——单列一档,
        # 与 UNSCORABLE 互斥统计(同一本书两者都中时只算一次,避免 n_ok 被多减)。
        return bool(result.get("formula_repair_flag"))

    n_unscorable = sum(1 for r in results if r["status"] == "OK" and _is_unscorable(r))
    n_formula_repair_flag = sum(
        1 for r in results
        if r["status"] in ("OK", "B") and _has_formula_repair_flag(r) and not _is_unscorable(r)
    )
    n_quality_repair_flag = sum(
        1 for r in results
        if r["status"] in ("OK", "B") and r.get("quality_repair_flag")
        and not _is_unscorable(r) and not _has_formula_repair_flag(r)
    )
    n_ok = (sum(1 for r in results if r["status"] in ("OK", "B"))
            - n_unscorable - n_formula_repair_flag - n_quality_repair_flag)
    n_suspect = sum(1 for r in results if r["status"] == "SUSPECT")
    n_skip = sum(1 for r in results if r["status"] == "SKIP")
    print(f"\n{'=' * 56}\n批处理完成: {n_ok} OK/B / {n_suspect} SUSPECT / "
          f"{n_unscorable} UNSCORABLE / {n_giveup} GIVEUP / {n_skip} SKIP / "
          f"{n_formula_repair_flag} FR_FLAG / {n_quality_repair_flag} QR_FLAG → {out_root}")
    if n_giveup:
        completion_status = CompletionStatus.FAILED
    elif (n_suspect or n_unscorable or n_formula_repair_flag
          or n_quality_repair_flag):
        completion_status = CompletionStatus.SUSPECT
    else:
        completion_status = CompletionStatus.OK
    return int(completion_status), results


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
    ap.add_argument("--born-digital-mode", choices=list(BORN_DIGITAL_MODES), default="hybrid",
                    help="路线 B(born-digital)采信模式:hybrid=块级混合采信(默认)/"
                         "defer=登记不转(回退开关)/ocr=完全走 OCR 忽略文本层(回退开关,转发给每本书的 convert.py)")
    add_repair_policy_arguments(ap)
    ap.add_argument("--quality-discovery", choices=list(QUALITY_DISCOVERY_MODES),
                    default="signals")
    ap.add_argument("--quality-learn", choices=list(QUALITY_LEARN_MODES), default="off")
    ap.add_argument("--quality-agent-timeout", type=int, default=300)
    args = ap.parse_args()
    if args.work_hours <= 0 or args.rest_minutes <= 0:
        ap.error("--work-hours 与 --rest-minutes 必须大于 0")
    repair_policy = repair_policy_from_namespace(args)
    formula_agents = (
        None if repair_policy.use_legacy_formula_chain
        else [spec.to_cli() for spec in repair_policy.formula_agents]
    )

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
                        rest_minutes=args.rest_minutes,
                        born_digital_mode=args.born_digital_mode,
                        formula_repair=repair_policy.runtime_formula_repair,
                        quality_repair=repair_policy.runtime_quality_repair,
                        quality_agents=list(repair_policy.runtime_quality_agents),
                        quality_discovery=args.quality_discovery,
                        quality_learn=args.quality_learn,
                        quality_agent_timeout=args.quality_agent_timeout,
                        formula_agents=formula_agents,
                        repair_policy=repair_policy)
        return rc
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
