"""单文档编排:分诊 → (A/C)逐页流式 OCR(可续跑/磁盘有界/坏页隔离) → 重组 md。B 登记不转。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time

import fitz

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown
from scripts.pipelines.textbooks.corrections import load_corrections, apply_corrections
from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout
from scripts.pipelines.textbooks.selfcheck import (
    block_coverage, katex_incompat_scan, aggregate_warnings, detect_column_layout,
    summarize_suspicions, build_source_audit_field, build_ocr_degeneration_field,
    inline_math_delimiter_ws_scan,
)
from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import images
from scripts.pipelines.textbooks import katex_triage
from scripts.pipelines.textbooks.katex_scan import scan_katex_work_pages
from scripts.pipelines.textbooks.formula_candidates import collect_formula_candidates
from scripts.pipelines.textbooks.formula_agents.adapters import default_adapters
from scripts.pipelines.textbooks.formula_agents.orchestrator import run_agents
from scripts.pipelines.textbooks.formula_agents.cli import crops_only_collect
from scripts.pipelines.textbooks.power import keep_system_awake
from scripts.pipelines.textbooks.source_audit import (
    extract_source_page, page_geometry, assign_source_words,
    audit_document, write_audit_report,
    ROUTE_B_V1_THRESHOLDS, THRESHOLD_PROFILE_V1,
    DRY_RUN_ADOPTION_THRESHOLDS,
)
from scripts.pipelines.textbooks.prose_adoption import (
    AdoptionThresholds, adopt_prose_blocks, apply_adoption,
)

DEFAULT_WORK_SECONDS = 6 * 60 * 60
DEFAULT_REST_SECONDS = 40 * 60

BORN_DIGITAL_MODES = ("defer", "ocr", "hybrid")

# Task B(2026-07-17 所有者批准)+ Task 1(2026-07-18 所有者裁决):单本转换收尾
# 自动接公式修复环。四档语义:
#   deterministic(默认,零成本零网络) —— katex_scan → (硬错时)katex_triage 分桶
#     + 视觉工单 → formula candidates 漏斗(确定性聚合,无 LLM 调用)。
#   agents —— deterministic 全部 + 公式 Agent 五道门(冻结模型链,外部 LLM 调用);
#     corrections 以 propose 模式落 pending,供人工审阅档使用。adapters 全不可用
#     时优雅降级为 deterministic 行为。
#   agents-apply —— deterministic 全部 + 公式 Agent 五道门,以 apply 模式全自动
#     应用(所有者 2026-07-18 裁决:撤销旧版"人工 accept 红线",安全兜底改由
#     orchestrator 内建机制承担——五道门/置信阈值 0.8/熔断阈值 0.6/自动回滚/
#     `.pre_agent.bak` 快照,编排层不再额外拦截)。adapters 全不可用时同样降级
#     为 deterministic 行为。
#   off —— 现状,只转换不后处理,零调用。
FORMULA_REPAIR_MODES = ("deterministic", "agents", "agents-apply", "off")

# 审计报告 schema 版本(与 source_audit.audit_document 输出的 schema_version 一致;
# 独立维护,断点恢复的指纹过期判定据此比对)。
AUDIT_SCHEMA_VERSION = 2

# 路线 B 采信/审计生产 profile——Task 13 标定冻结(所有者 2026-07-17 批准)。
# 采信阈值维持标定前取值(fixture 证据:构造错误页采信 NED 0.0097,NED 0.2 是
# 20× 余量的安全阀);审计阈值见 source_audit.ROUTE_B_V1_THRESHOLDS 的标定注释。
# F5:生产采信阈值与审计 dry-run 采信阈值同值,单一来源在 source_audit——本处
# 直接引用,不再双写三个字面量(值不变,冻结锁行为一致)。
ROUTE_B_ADOPTION_THRESHOLDS = DRY_RUN_ADOPTION_THRESHOLDS
ROUTE_B_AUDIT_THRESHOLDS = ROUTE_B_V1_THRESHOLDS


class ScheduledRest:
    """在页边界执行的活跃时长节流，不中断正在进行的 OCR 调用。"""

    def __init__(self, work_seconds: float, rest_seconds: float, *,
                 clock=time.monotonic, sleeper=time.sleep):
        self.work_seconds = work_seconds
        self.rest_seconds = rest_seconds
        self.clock = clock
        self.sleeper = sleeper
        self.window_started = clock()

    def rest_if_due(self) -> bool:
        if self.clock() - self.window_started < self.work_seconds:
            return False
        print(f"[rest] 已连续运行 {self.work_seconds / 3600:g}h，"
              f"休息 {self.rest_seconds / 60:g}min...")
        self.sleeper(self.rest_seconds)
        self.window_started = self.clock()
        return True


def _expected_visual_filenames(blocks: list[dict], page: int) -> list[str]:
    return [images.crop_filename(page, b.get("block_id"))
            for b in blocks
            if images.is_visual_block(b.get("block_label", ""))
            and b.get("block_order") is None and b.get("block_bbox")]


def _backfill_missing_assets(blocks: list[dict], pdf_path: str, dpi: int,
                              work_dir: str, assets_dir: str, page: int) -> None:
    """裁图钩子只覆盖本次运行处理的页;已完成页(续跑/历史检查点)不会重新进入
    OCR 循环,PNG 早已删除。这里对每页核对应有的裁图文件是否在盘,缺失则用
    manifest 记录的 dpi 重新栅格化该页(同 DPI 保证 bbox 对齐)、裁图、删 PNG。"""
    expected = _expected_visual_filenames(blocks, page)
    if not expected:
        return
    if all(os.path.exists(os.path.join(assets_dir, f)) for f in expected):
        return
    png = None
    try:
        png = pdf_page_to_png(pdf_path, page, work_dir, dpi=dpi)
        images.crop_block_images(png, blocks, assets_dir, page)
    except Exception:                                          # noqa: BLE001 补裁失败不掀翻整批
        pass
    finally:
        if png and os.path.exists(png):
            os.remove(png)


def assemble(work_dir: str, total: int, stem: str, assets_dir: str,
             pdf_path: str | None, dpi: int,
             corrections_dir: str | None = None,
             adopt_ctx: "_AdoptContext | None" = None) -> dict:
    """按页序读检查点 → 应用公式修正叠加层(§2,可选,无 corrections.json 则无影响)
    → (hybrid)块级采信替换 → 重组 md + 补裁缺失资产 + 汇总告警/双栏嫌疑页/缺失资产清单。

    adopt_ctx 非空(路线 B hybrid)时,对每页在 reconstruct 前做源文本采信替换,
    并把逐页采信决策收集进 adopt_ctx.decisions_by_page(供审计消费)。采信/审计
    的异常由调用方在整本层面隔离——本函数不吞异常,让其上抛以触发整本回退。"""
    doc_dir = corrections_dir or os.path.dirname(os.path.normpath(work_dir))
    corrections = load_corrections(doc_dir)
    md_pages: list[str] = []
    all_blocks: list[dict] = []
    all_warnings: list[dict] = []
    missing_assets: list[str] = []
    column_layout_suspected: list[int] = []
    for i in range(1, total + 1):
        blocks = cp.load_page_blocks(work_dir, i)
        if corrections:
            blocks = apply_corrections(blocks, i, corrections)
        if adopt_ctx is not None:
            blocks = adopt_ctx.adopt_page(blocks, i)
        all_blocks.extend(blocks)
        _backfill_missing_assets(blocks, pdf_path, dpi, work_dir, assets_dir, i)
        expected = _expected_visual_filenames(blocks, i)
        missing_assets.extend(f for f in expected
                              if not os.path.exists(os.path.join(assets_dir, f)))
        if detect_column_layout(blocks):
            column_layout_suspected.append(i)
        page_md, warnings = reconstruct_markdown(blocks, stem=stem, page=i)
        all_warnings.extend(warnings)
        if page_md.strip():
            md_pages.append(page_md)
    return {
        "md": "\n\n".join(md_pages) + "\n",
        "blocks": all_blocks,
        "warnings": all_warnings,
        "missing_assets": missing_assets,
        "column_layout_suspected": column_layout_suspected,
    }


def reassemble_md(layout: DocLayout, pdf_path: str | None, dpi: int) -> str | None:
    """幂等对账:读 _work 检查点 → 应用采纳的修正 → 重组 → 覆盖写 layout.md_path。
    复用 convert_pdf 用的同一个 assemble(),保证 debug 采纳出的 md 与正式转换逐字一致。
    只写 md,不写 selfcheck、不动 manifest。manifest 缺失/total 为 0 时返回 None。"""
    manifest = cp.load_manifest(layout.work_dir)
    if not manifest:
        return None
    total = manifest["fingerprint"]["page_count"]
    if not total:
        return None
    result = assemble(layout.work_dir, total, layout.stem, layout.assets_dir,
                      pdf_path, dpi, corrections_dir=layout.doc_work_dir)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    with open(layout.md_path, "w", encoding="utf-8") as f:
        f.write(result["md"])
    return layout.md_path


def _register_deferred(pdf_path: str, out_dir: str, stem: str) -> dict:
    deferred = os.path.join(out_dir, "_deferred_born_digital")
    os.makedirs(deferred, exist_ok=True)
    with open(os.path.join(deferred, stem + ".txt"), "w", encoding="utf-8") as f:
        f.write(pdf_path + "\n")
    return {"route": "B", "md_path": None, "selfcheck": None, "failed_pages": [],
            "source_audit": None, "born_digital_mode": "defer", "adoption_error": False,
            "formula_repair": {"mode": "off", "reason": "route_b_deferred_no_md"}}


class _AdoptContext:
    """路线 B hybrid 的逐页采信上下文(约束 8:同一 blocks 列表贯穿
    assign_source_words / adopt_prose_blocks / apply_adoption,绝不过滤/重排)。

    采信只依赖 checkpoint JSON(OCR blocks + 页级 width/height)+ 源 PDF + 注入阈值,
    无时间戳/随机性进入内容 → resume 与一次跑完逐字节一致。"""

    def __init__(self, pdf_doc, work_dir: str, thresholds: AdoptionThresholds):
        self.pdf_doc = pdf_doc
        self.work_dir = work_dir
        self.thresholds = thresholds
        self.decisions_by_page: dict[int, list] = {}

    def adopt_page(self, blocks: list[dict], page: int) -> list[dict]:
        ocr_result = cp.load_page_result(self.work_dir, page)
        fitz_page = self.pdf_doc[page - 1]
        source_page = extract_source_page(fitz_page)
        geometry = page_geometry(fitz_page, ocr_result)
        assignment = assign_source_words(source_page["words"], blocks, geometry)
        decisions = adopt_prose_blocks(
            blocks, assignment, source_page, not geometry.unscorable, self.thresholds)
        self.decisions_by_page[page] = decisions
        return apply_adoption(blocks, decisions)


def _load_audit_report(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _audit_fresh(report: dict | None, pdf_path: str, dpi: int,
                 expected_mode: str) -> bool:
    """审计报告是否新鲜:schema_version / born_digital_mode / PDF 指纹(页数+字节)/
    DPI 均未变。任一变化即判过期,调用方据此只重算 adoption+reconstruct+audit
    (不重跑 OCR)。

    expected_mode 是本轮运行会落盘的模式标签(hybrid/ocr/"n/a"/"unknown")。
    模式与报告的 born_digital_mode 不符即判过期(F1):hybrid 成功后按回退路径改用
    ocr 重跑(反之亦然),md 内容已换但旧报告仍顶着上一模式的采信 provenance——
    强制重算,不让陈旧报告误导操作者。"""
    if not report:
        return False
    if report.get("schema_version") != AUDIT_SCHEMA_VERSION:
        return False
    if report.get("born_digital_mode") != expected_mode:
        return False
    cur = cp.pdf_fingerprint(pdf_path)
    fp = report.get("pdf_fingerprint") or {}
    if fp.get("page_count") != cur["page_count"] or fp.get("size_bytes") != cur["size_bytes"]:
        return False
    if (report.get("ocr_fingerprint") or {}).get("dpi") != dpi:
        return False
    # 采信/审计异常留下的 SUSPECT 报告不得当 fresh 复用——下次 resume 必须重算,
    # 否则采信/审计已能成功仍顶着陈旧错误报告(Minor 3)。
    issue_counts = (report.get("summary") or {}).get("issue_counts") or {}
    if issue_counts.get("adoption_error") or issue_counts.get("audit_error"):
        return False
    return True


def _fingerprint_fields(pdf_path: str, dpi: int) -> tuple[dict, dict]:
    cur = cp.pdf_fingerprint(pdf_path)
    return (
        {"size_bytes": cur["size_bytes"], "page_count": cur["page_count"]},
        {"dpi": dpi, "page_count": cur["page_count"]},
    )


def _not_applicable_report(pdf_path: str, layout: DocLayout, dpi: int) -> dict:
    """A 路(无文本层)最小审计报告:显式 NOT_APPLICABLE(跟随 selfcheck 惯例——
    始终产出一个可发现的报告文件,而不是静默不写)。"""
    pdf_fp, ocr_fp = _fingerprint_fields(pdf_path, dpi)
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "stem": layout.stem, "route": "A", "born_digital_mode": "n/a",
        "pdf_fingerprint": pdf_fp, "ocr_fingerprint": ocr_fp,
        "threshold_profile": None, "adoption_source": "n/a",
        "summary": {"status": "NOT_APPLICABLE", "pages": ocr_fp["page_count"],
                    "scorable_pages": 0, "suspect_pages": [], "issue_counts": {}},
        "pages": [],
    }


def _error_audit_report(pdf_path: str, layout: DocLayout, dpi: int, *,
                        route: str, mode: str | None, code: str, detail: str,
                        adoption_source: str) -> dict:
    """convert 层错误的 SUSPECT 审计报告:文档计为完成(SUSPECT 是完成状态),
    marker 正常删除,避免每轮批处理活锁重跑。code 区分来源:
      - adoption_error:hybrid 采信/组装步骤异常(内容已整本回退等价 ocr)。
      - audit_error:纯审计步骤异常(ocr/C/F 的 _finalize_audit,或 hybrid 中
        audit_document 调用本身)——内容不动,仅审计未完成。

    adoption_source 由调用点传入真实值(#15),不再硬编码 "recorded":hybrid 两条
    错误路径为 "recorded"(采信已记录/已尝试),非 hybrid(ocr/C/F)的审计错误
    路径为 "dry_run"(那些路只跑 dry-run 推演,从不落地采信)。"""
    pdf_fp, ocr_fp = _fingerprint_fields(pdf_path, dpi)
    issue = {"code": code, "block_id": None, "detail": detail}
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "stem": layout.stem, "route": route, "born_digital_mode": mode,
        "pdf_fingerprint": pdf_fp, "ocr_fingerprint": ocr_fp,
        "threshold_profile": None, "adoption_source": adoption_source,
        "summary": {"status": "SUSPECT", "pages": ocr_fp["page_count"],
                    "scorable_pages": 0, "suspect_pages": [],
                    "adoption": {"prose_blocks": 0, "adopted": 0,
                                 "fallback_ocr": 0, "fallback_reasons": {}},
                    "issue_counts": {code: 1}},
        "issues": [issue], "pages": [],
    }


def _write_audit_safely(report: dict, path: str) -> bool:
    """错误/正常路径统一的审计落盘:写盘失败(OSError 等)记日志、返回 False,
    绝不逃逸破坏批处理隔离(Minor 4)。"""
    try:
        write_audit_report(report, path)
        return True
    except Exception as e:                     # noqa: BLE001 写盘失败不逃逸
        print(f"[textbooks] 审计报告写盘失败,跳过: {type(e).__name__}: {e}")
        return False


def _maybe_remove_deferred_marker(out_dir: str, stem: str, *,
                                  giveup: bool, audit_ok: bool) -> None:
    """仅当该 stem 的 B 转换成功(产出内容 + 审计报告在盘)才删除旧 deferred 登记标记;
    giveup(无任一页完成)不算成功、SUSPECT 算完成状态。删除时显式日志。"""
    if giveup or not audit_ok:
        return
    marker = os.path.join(out_dir, "_deferred_born_digital", stem + ".txt")
    if os.path.exists(marker):
        os.remove(marker)
        print(f"[textbooks] B 转换完成,删除 deferred 登记标记: {marker}")


def _safe_probe(adapter) -> bool:
    """probe() 只做 shutil.which/进程存在性检查,理论上不该抛,但外部 adapter 是
    第三方 CLI 封装——防御式吞异常,决不能让"检测是否可用"这一步本身崩掉整轮。"""
    try:
        return bool(adapter.probe())
    except Exception:                              # noqa: BLE001 探测异常不逃逸
        return False


def _run_agents_stage(layout: DocLayout, pdf_path: str, dpi: int,
                      agent_mode: str) -> dict:
    """公式 Agent 终检链入口(stage 9)。agent_mode 语义(所有者 2026-07-18 裁决,
    撤销旧版"绝不传 apply"红线——见 convert.py 顶部 FORMULA_REPAIR_MODES 注释块):
      - "propose":对应 --formula-repair agents,corrections 落 pending,供人工
        审阅档使用,不传 collect_fn(propose 档现状不变,回归锁定)。
      - "apply":对应 --formula-repair agents-apply,全自动应用,传
        collect_fn=crops_only_collect 过滤候选(硬性要求,见同一注释块)。安全
        兜底完全由 run_agents 内建的五道门/置信阈值/熔断/自动回滚/快照承担,
        编排层只透传 mode,不重新实现任何拦截逻辑。

    adapters 全不可用(未装 CLI/未登录)→ 优雅降级为等价 deterministic 行为:
    不调用 run_agents(不产生任何 agent 相关调用/文件写入),只留明确记录,不崩。"""
    try:
        adapters = default_adapters()
    except Exception as e:                         # noqa: BLE001 adapter 构造异常不逃逸
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    available = [a for a in adapters if _safe_probe(a)]
    if not available:
        return {"status": "degraded_deterministic",
                "reason": "无可用 formula-agent CLI adapter(PATH 未装/未登录),"
                          "已降级为 deterministic 行为,未调用任何 agent"}
    try:
        kwargs = {"collect_fn": crops_only_collect} if agent_mode == "apply" else {}
        report = run_agents(layout, adapters=available, pdf_path=pdf_path or "",
                            dpi=dpi, mode=agent_mode, **kwargs)
        return {"status": "ok", "run_mode": report.mode,
                "n_candidates": report.n_candidates, "applied": report.applied,
                "rejected": len(report.rejected), "pending_ids": report.pending_ids,
                "circuit_broken": report.circuit_broken,
                "rolled_back": report.rolled_back, "reason": report.reason}
    except Exception as e:                         # noqa: BLE001 agent 链异常不逃逸
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _run_formula_repair(layout: DocLayout, pdf_path: str, mode: str, dpi: int) -> dict:
    """转换收尾自动接公式修复环(Task B,2026-07-17 所有者批准)。

    deterministic:katex_scan(node 缺失优雅跳过)→ 有硬错才跑 katex_triage 分桶
    + 视觉工单(镜像 batch.py 收尾已有自动化)→ formula candidates 漏斗(确定性
    聚合,读 worklist/render_errors,无 LLM 调用)。
    agents / agents-apply:deterministic 全部 + _run_agents_stage(五道门),
    仅 agent_mode 不同("propose" 落 pending / "apply" 全自动应用,见
    _run_agents_stage docstring)。
    off:零调用。

    每个子步骤独立 try/except——一步异常不影响其它步骤继续跑,也不影响本函数
    正常返回(顶层调用方 convert_pdf 另有一层兜底,双保险)。"""
    result: dict = {"mode": mode}
    if mode == "off":
        return result

    katex_result = None
    try:
        katex_result = scan_katex_work_pages(layout, layout.render_errors_path)
    except Exception as e:                         # noqa: BLE001 扫描异常不逃逸
        result["katex_scan"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    else:
        if katex_result is None:
            result["katex_scan"] = {"status": "skipped", "reason": "node_missing"}
        else:
            errors = katex_result.get("errors", [])
            result["katex_scan"] = {"status": "ok", "hard_errors": len(errors)}
            if errors:
                try:
                    worklist_path = katex_triage.report_for_batch(layout, katex_result)
                    result["katex_triage"] = {"status": "ok", "worklist_path": worklist_path}
                except Exception as e:              # noqa: BLE001 分桶异常不逃逸
                    result["katex_triage"] = {"status": "error",
                                              "error": f"{type(e).__name__}: {e}"}

    try:
        collected = collect_formula_candidates(layout, write=True)
        candidates = collected.get("candidates", []) if isinstance(collected, dict) else []
        result["formula_candidates"] = {"status": "ok", "count": len(candidates)}
    except Exception as e:                         # noqa: BLE001 候选聚合异常不逃逸
        result["formula_candidates"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    if mode in ("agents", "agents-apply"):
        agent_mode = "apply" if mode == "agents-apply" else "propose"
        result["agents"] = _run_agents_stage(layout, pdf_path, dpi, agent_mode)

    return result


def convert_pdf(pdf_path: str, deliverables_dir: str | None = None,
                work_dir: str | None = None, dpi: int = cp.DEFAULT_DPI,
                write_selfcheck: bool = True, force_ocr: bool = False,
                work_seconds: float = DEFAULT_WORK_SECONDS,
                rest_seconds: float = DEFAULT_REST_SECONDS,
                born_digital_mode: str = "hybrid",
                formula_repair: str = "deterministic") -> dict:
    if born_digital_mode not in BORN_DIGITAL_MODES:
        raise ValueError(f"born_digital_mode 须为 {BORN_DIGITAL_MODES},收到 {born_digital_mode!r}")
    if formula_repair not in FORMULA_REPAIR_MODES:
        raise ValueError(f"formula_repair 须为 {FORMULA_REPAIR_MODES},收到 {formula_repair!r}")
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    deliverables_dir = deliverables_dir or os.path.dirname(os.path.abspath(pdf_path))
    layout = resolve_layout(stem, deliverables_dir, work_dir)
    detected_route = triage(pdf_path)
    route = "F" if force_ocr and detected_route == "B" else detected_route
    # 路线 B:defer 保持登记不转(逐字不变);ocr/hybrid 走 OCR 主链(manifest route 仍 "B")。
    if route == "B" and born_digital_mode == "defer":
        return _register_deferred(pdf_path, deliverables_dir, stem)

    work_dir_ = layout.work_dir
    assets_dir = layout.assets_dir

    # 指纹校验:源或 DPI 变 → 清空全新跑
    manifest = cp.load_manifest(work_dir_)
    if manifest is None or not cp.fingerprint_ok(manifest, pdf_path, dpi):
        if manifest is not None:
            print(f"[textbooks] 指纹失配(源或DPI变),清空 {work_dir_} 全新跑")
        cp.reset_work_dir(work_dir_)
        if os.path.isdir(assets_dir):                # assets 在交付根不在过程根,
            shutil.rmtree(assets_dir)                 # reset_work_dir 碰不到,不清会变孤儿文件
        manifest = cp.new_manifest(pdf_path, cp.pdf_fingerprint(pdf_path), dpi, route)
        cp.save_manifest(work_dir_, manifest)

    # 毒页 startup 解析:上次进程崩在某页且已达硬尝试上限 → 标 process-killed
    cp.resolve_poison(manifest, work_dir_)
    cp.save_manifest(work_dir_, manifest)

    # 清理上次崩溃残留的 PNG(磁盘有界:_work 内不应留存 PNG)
    for fn in os.listdir(work_dir_):
        if fn.startswith("page_") and fn.endswith(".png"):
            try:
                os.remove(os.path.join(work_dir_, fn))
            except OSError:
                pass

    total = manifest["fingerprint"]["page_count"]
    poisoned = {f["page"] for f in manifest["failed_pages"]
                if f["kind"] == "process-killed"}
    todo = [p for p in cp.pages_todo(work_dir_, total) if p not in poisoned]
    done = sum(1 for i in range(1, total + 1) if cp.is_page_done(work_dir_, i))
    durations: list[float] = []
    scheduled_rest = ScheduledRest(work_seconds, rest_seconds)
    for page in todo:
        t = time.time()
        cp.set_in_progress(manifest, page)   # predict 前留痕:进程硬崩后可检出毒页
        cp.save_manifest(work_dir_, manifest)
        png = None
        try:
            png = pdf_page_to_png(pdf_path, page, work_dir_, dpi=dpi)
            blocks = predict_page(png, work_dir_)   # 非空时 engine 已落 res.json
            if not blocks and not cp.is_page_done(work_dir_, page):
                cp.write_empty_page(work_dir_, page)   # 空白页显式标记完成
            elif blocks:
                images.crop_block_images(png, blocks, assets_dir, page)  # PNG 删除前裁图
        except Exception as e:                        # noqa: BLE001 坏页隔离
            cp.record_failure(manifest, page, f"{type(e).__name__}: {e}",
                              "page-exception")
        finally:
            if png and os.path.exists(png):
                os.remove(png)                        # 磁盘有界:predict 后即删
        cp.clear_in_progress(manifest)
        cp.save_manifest(work_dir_, manifest)
        if cp.is_page_done(work_dir_, page):
            done += 1
        durations.append(time.time() - t)
        avg = sum(durations) / len(durations)
        eta_h = avg * (total - done) / 3600
        nfail = len(manifest["failed_pages"])
        print(f"[page {page}/{total}] {durations[-1]:.0f}s "
              f"(完成 {done} 失败 {nfail} ETA {eta_h:.1f}h)")
        scheduled_rest.rest_if_due()

    # 陈旧失败清理 + 去重:已完成的页移除;同页多次失败只留最后一条(含 process-killed)
    dedup: dict[int, dict] = {}
    for f in manifest["failed_pages"]:
        if not cp.is_page_done(work_dir_, f["page"]):
            dedup[f["page"]] = f
    manifest["failed_pages"] = list(dedup.values())

    # 从检查点重组(每次运行都做,部分完成也产出部分 md);hybrid 走采信整本隔离。
    # 顺带补裁续跑/历史检查点缺失的资产。
    is_hybrid = route == "B" and born_digital_mode == "hybrid"
    adoption_error = False
    audit_report: dict | None = None
    if is_hybrid:
        result, audit_report, adoption_error = _finalize_hybrid(
            pdf_path, layout, work_dir_, total, stem, assets_dir, dpi)
    else:
        result = assemble(work_dir_, total, stem, assets_dir, pdf_path, dpi,
                          corrections_dir=layout.doc_work_dir)
        # 审计落盘提前到这里(非 hybrid 路由):selfcheck 的 source_audit/
        # ocr_degeneration 紧凑字段(计划 §7.2)要读到本轮刚落盘的审计报告,
        # 顺序必须先于下面的 selfcheck 组装(hybrid 路由的审计已在
        # _finalize_hybrid 内完成,顺序天然满足,不需要额外挪动)。
        audit_report = _finalize_audit(route, born_digital_mode, pdf_path, layout, dpi)

    md, all_blocks = result["md"], result["blocks"]
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    with open(layout.md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    check["formula_suspicions"] = summarize_suspicions(md)
    check["inline_math_delimiter_ws"] = inline_math_delimiter_ws_scan(md)
    check.update(aggregate_warnings(result["warnings"]))
    check["missing_assets"] = result["missing_assets"]
    check["column_layout_suspected"] = result["column_layout_suspected"]
    pdf_fp, _ocr_fp = _fingerprint_fields(pdf_path, dpi)
    check["source_audit"] = build_source_audit_field(
        layout.source_audit_path, pdf_fp, dpi, AUDIT_SCHEMA_VERSION)
    check["ocr_degeneration"] = build_ocr_degeneration_field(
        layout.source_audit_path, pdf_fp, dpi, AUDIT_SCHEMA_VERSION)
    if write_selfcheck:
        os.makedirs(layout.doc_work_dir, exist_ok=True)
        with open(layout.selfcheck_path, "w", encoding="utf-8") as f:
            json.dump(check, f, ensure_ascii=False, indent=2)
    cp.save_manifest(work_dir_, manifest)

    # 路线 B(ocr/hybrid)转换成功 → 清旧 deferred 登记标记(SUSPECT 算完成、giveup 不算)。
    if route == "B" and born_digital_mode in ("ocr", "hybrid"):
        _maybe_remove_deferred_marker(
            deliverables_dir, stem,
            giveup=(done == 0), audit_ok=audit_report is not None)

    # 转换本体到这里已成功、md/selfcheck 已落盘——后处理失败隔离硬要求(Task B):
    # 公式修复环任何异常都不得改变本次转换已经成功这个事实,顶层兜底一次(内部
    # _run_formula_repair 逐步骤已各自 try/except,这里是双保险,防止其自身的
    # 编排代码本身出问题,如 default_adapters() 之外的意外)。
    try:
        formula_repair_result = _run_formula_repair(layout, pdf_path, formula_repair, dpi)
    except Exception as e:                          # noqa: BLE001 后处理整体隔离,不影响已产出的 md/selfcheck
        print(f"[textbooks] 公式修复后处理异常(md/selfcheck 完好,不受影响): "
              f"{type(e).__name__}: {e}")
        formula_repair_result = {"mode": formula_repair, "status": "error",
                                 "error": f"{type(e).__name__}: {e}"}

    # 落盘供批量收尾分诊(Review Important):agents-apply 的熔断/回滚等安全网
    # 触发事件此前只留在子进程 stdout 里,batch 汇总读不到——同 selfcheck 写盘
    # 同一套 write_selfcheck 开关,不新造旗标。
    if write_selfcheck:
        os.makedirs(layout.doc_work_dir, exist_ok=True)
        with open(layout.formula_repair_path, "w", encoding="utf-8") as f:
            json.dump(formula_repair_result, f, ensure_ascii=False, indent=2)

    return {"route": route, "md_path": layout.md_path, "selfcheck": check,
            "failed_pages": manifest["failed_pages"],
            "source_audit": audit_report,
            "born_digital_mode": born_digital_mode if route == "B" else None,
            "adoption_error": adoption_error,
            "formula_repair": formula_repair_result}


def _finalize_hybrid(pdf_path: str, layout: DocLayout, work_dir: str, total: int,
                     stem: str, assets_dir: str, dpi: int) -> tuple[dict, dict | None, bool]:
    """路线 B hybrid 的采信 + 审计,分步崩溃隔离。返回 (assemble 结果, 审计报告, adoption_error)。

    两段独立隔离(异常绝不逃逸、绝不半页采信半页丢、不触碰任何 OCR checkpoint):
      - 采信/组装步骤异常 → 整本回退等价 ocr 模式重建内容(原始 blocks reconstruct),
        SUSPECT + issue adoption_error,adoption_error=True。
      - 采信成功但审计步骤(audit_document/写盘)异常 → 保留已采信 md(内容已安全产出,
        仅审计未完成),SUSPECT + issue audit_error,adoption_error=False。
    两种情形均计为完成状态,marker 正常删除(避免批处理活锁)。"""
    # ---- 第一段:采信 + 组装 ----
    try:
        pdf_doc = fitz.open(pdf_path)
        try:
            ctx = _AdoptContext(pdf_doc, work_dir, ROUTE_B_ADOPTION_THRESHOLDS)
            result = assemble(work_dir, total, stem, assets_dir, pdf_path, dpi,
                              corrections_dir=layout.doc_work_dir, adopt_ctx=ctx)
            decisions_by_page = ctx.decisions_by_page
        finally:
            pdf_doc.close()
    except Exception as e:                     # noqa: BLE001 采信/组装整本回退,绝不逃逸
        print(f"[textbooks] 采信/组装异常,整本回退等价 ocr 重建: {type(e).__name__}: {e}")
        result = assemble(work_dir, total, stem, assets_dir, pdf_path, dpi,
                          corrections_dir=layout.doc_work_dir)
        report = _error_audit_report(pdf_path, layout, dpi, route="B", mode="hybrid",
                                     code="adoption_error", detail=f"{type(e).__name__}: {e}",
                                     adoption_source="recorded")
        return result, (report if _write_audit_safely(report, layout.source_audit_path)
                        else None), True

    # ---- 第二段:审计(采信已成功,内容不再回退) ----
    try:
        existing = _load_audit_report(layout.source_audit_path)
        if _audit_fresh(existing, pdf_path, dpi, "hybrid") and \
                existing.get("adoption_source") == "recorded":
            return result, existing, False     # 断点恢复:指纹新鲜则复用(内容等价,确定性)
        report = audit_document(pdf_path, layout, ROUTE_B_AUDIT_THRESHOLDS,
                                decisions_by_page, born_digital_mode="hybrid",
                                threshold_profile=THRESHOLD_PROFILE_V1)
        report["route"] = "B"
        return result, (report if _write_audit_safely(report, layout.source_audit_path)
                        else None), False
    except Exception as e:                     # noqa: BLE001 纯审计异常,保留已采信 md
        print(f"[textbooks] 审计异常(采信已成功,保留 md): {type(e).__name__}: {e}")
        report = _error_audit_report(pdf_path, layout, dpi, route="B", mode="hybrid",
                                     code="audit_error", detail=f"{type(e).__name__}: {e}",
                                     adoption_source="recorded")
        return result, (report if _write_audit_safely(report, layout.source_audit_path)
                        else None), False


def _finalize_audit(route: str, born_digital_mode: str, pdf_path: str,
                    layout: DocLayout, dpi: int) -> dict | None:
    """非 hybrid 路由的审计落盘(仅在缺失/指纹过期时重算——断点恢复不重跑 OCR)。

    A 路:NOT_APPLICABLE 最小报告。B-ocr/C/F 路:dry-run 决策审计(绝不 apply);
    C 路借此保存页级 source health,但不用坏文本层覆盖率作硬判断、不采信。审计步骤异常
    → 写 audit_error SUSPECT 报告(文档计为完成、marker 正常删除,避免活锁),不逃逸、
    不影响已写好的 md/checkpoint。落盘报告的 route 字段覆写为真实路由(audit_document
    内部硬编码 "B",约束 7 禁改它,故在此覆盖)。"""
    existing = _load_audit_report(layout.source_audit_path)
    if route == "A":
        if _audit_fresh(existing, pdf_path, dpi, "n/a") and \
                (existing.get("summary") or {}).get("status") == "NOT_APPLICABLE":
            return existing
        report = _not_applicable_report(pdf_path, layout, dpi)
        return report if _write_audit_safely(report, layout.source_audit_path) else None
    mode_label = born_digital_mode if route == "B" else None
    # audit_document 把 None 归一为 "unknown"(C/F 路),freshness 校验须比对同一标签。
    expected_mode = mode_label if mode_label is not None else "unknown"
    if _audit_fresh(existing, pdf_path, dpi, expected_mode):
        return existing
    try:
        report = audit_document(pdf_path, layout, ROUTE_B_AUDIT_THRESHOLDS,
                                None, born_digital_mode=mode_label,
                                threshold_profile=THRESHOLD_PROFILE_V1)
        report["route"] = route                # Important 1:落盘报告 route 字段失实修正
        return report if _write_audit_safely(report, layout.source_audit_path) else None
    except Exception as e:                     # noqa: BLE001 审计异常写 SUSPECT、不逃逸
        print(f"[textbooks] 审计(dry-run)异常,写 audit_error 报告: {type(e).__name__}: {e}")
        report = _error_audit_report(pdf_path, layout, dpi, route=route, mode=mode_label,
                                     code="audit_error", detail=f"{type(e).__name__}: {e}",
                                     adoption_source="dry_run")
        return report if _write_audit_safely(report, layout.source_audit_path) else None


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 单文档转换(可续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="交付根(md+assets,默认就地)")
    ap.add_argument("--work-dir", default=None, help="过程根(默认 <out>/_work_root)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--force-ocr", action="store_true",
                    help="忽略优质文本层并强制逐页栅格化 OCR")
    ap.add_argument("--work-hours", type=float, default=6,
                    help="每轮连续 OCR 时长(小时，默认6)")
    ap.add_argument("--rest-minutes", type=float, default=40,
                    help="每轮结束后的 GPU 空闲时长(分钟，默认40)")
    ap.add_argument("--no-selfcheck-json", action="store_true",
                    help="不写 <stem>_selfcheck.json(控制台摘要仍输出)")
    ap.add_argument("--allow-sleep", action="store_true",
                    help="允许系统按电源计划睡眠(默认转换期间阻止睡眠)")
    ap.add_argument("--born-digital-mode", choices=list(BORN_DIGITAL_MODES), default="hybrid",
                    help="路线 B(born-digital)采信模式:hybrid=块级混合采信(默认)/"
                         "defer=登记不转(回退开关)/ocr=完全走 OCR 忽略文本层(回退开关)")
    ap.add_argument("--formula-repair", choices=list(FORMULA_REPAIR_MODES),
                    default="deterministic",
                    help="转换收尾自动接的公式修复环:deterministic=katex_scan+"
                         "(有硬错时)katex_triage 分桶+视觉工单+formula candidates 漏斗"
                         "(默认,零成本零网络)/agents=deterministic 全部+公式 Agent 五道门"
                         "(外部 LLM 调用,corrections 落 pending,人工审阅档)/"
                         "agents-apply=deterministic 全部+公式 Agent 五道门,全自动应用"
                         "(2026-07-18 裁决,安全由 orchestrator 内建熔断/回滚/快照承担)/"
                         "off=不后处理(现状)")
    args = ap.parse_args()
    if args.work_hours <= 0 or args.rest_minutes <= 0:
        ap.error("--work-hours 与 --rest-minutes 必须大于 0")
    with keep_system_awake(enabled=not args.allow_sleep):
        res = convert_pdf(args.src, args.out, args.work_dir, dpi=args.dpi,
                          write_selfcheck=not args.no_selfcheck_json,
                          force_ocr=args.force_ocr,
                          work_seconds=args.work_hours * 3600,
                          rest_seconds=args.rest_minutes * 60,
                          born_digital_mode=args.born_digital_mode,
                          formula_repair=args.formula_repair)
    print(f"[route={res['route']}] md={res['md_path']}")
    if res.get("failed_pages"):
        print(f"[textbooks] 失败页 {len(res['failed_pages'])}:",
              [f["page"] for f in res["failed_pages"]])
    if res["selfcheck"]:
        c = res["selfcheck"]
        print(f"[Tier0] blocks {c['in_md']}/{c['total']} 覆盖, 缺 {len(c['missing'])}")
        if c.get("katex_incompat"):
            print("[Tier0] KaTeX 不兼容残留:", ", ".join(c["katex_incompat"]))
    fr = res.get("formula_repair")
    if fr and fr.get("mode") != "off":
        print(f"[formula_repair] mode={fr.get('mode')} "
              f"katex_scan={fr.get('katex_scan')} "
              f"formula_candidates={fr.get('formula_candidates')}")
        if "agents" in fr:
            print(f"[formula_repair] agents={fr['agents']}")


if __name__ == "__main__":
    main()
