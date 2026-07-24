"""单文档编排:分诊 → (A/C)逐页流式 OCR(可续跑/磁盘有界/坏页隔离) → 重组 md。B 登记不转。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import fitz

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_fragments
from scripts.pipelines.textbooks.corrections import load_corrections, apply_corrections
from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout
from scripts.pipelines.textbooks.selfcheck import (
    block_coverage, katex_incompat_scan, aggregate_warnings, detect_column_layout,
    summarize_suspicions, build_source_audit_field, build_ocr_degeneration_field,
    inline_math_delimiter_ws_scan,
)
from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import derived_cache as dc
from scripts.pipelines.textbooks import images
from scripts.pipelines.textbooks import katex_triage
from scripts.pipelines.textbooks.document_lock import DocumentLock
from scripts.pipelines.textbooks.katex_scan import scan_katex_work_pages
from scripts.pipelines.textbooks.formula_candidates import collect_formula_candidates
from scripts.pipelines.textbooks.formula_agents.adapters import CliAdapter, default_adapters
from scripts.pipelines.textbooks.formula_agents.orchestrator import run_agents
from scripts.pipelines.textbooks.formula_agents.cli import crops_only_collect
from scripts.pipelines.textbooks.power import keep_system_awake
from scripts.pipelines.textbooks.repair_policy import (
    AgentSpec as RepairAgentSpec,
    CompletionStatus,
    FORMULA_REPAIR_MODES,
    QUALITY_REPAIR_MODES,
    add_repair_policy_arguments,
    quality_final_is_conclusive,
    repair_policy_from_namespace,
    source_audit_blocks_completion,
)
from scripts.pipelines.textbooks.source_audit import (
    extract_source_page, page_geometry, assign_source_words,
    audit_document, write_audit_report,
    corrections_file_fingerprint,
    ROUTE_B_V1_THRESHOLDS, THRESHOLD_PROFILE_V1,
    DRY_RUN_ADOPTION_THRESHOLDS,
)
from scripts.pipelines.textbooks.prose_adoption import (
    AdoptionDecision, AdoptionThresholds, adopt_prose_blocks, apply_adoption,
)

DEFAULT_WORK_SECONDS = 6 * 60 * 60
DEFAULT_REST_SECONDS = 40 * 60

BORN_DIGITAL_MODES = ("defer", "ocr", "hybrid")

# Task B(2026-07-17 所有者批准)+ Task 1(2026-07-18 所有者裁决):单本转换收尾
# 自动接公式修复环。四档语义:
#   deterministic(零成本零网络) —— katex_scan → (硬错时)katex_triage 分桶
#     + 视觉工单 → formula candidates 漏斗(确定性聚合,无 LLM 调用)。
#   agents —— deterministic 全部 + 公式 Agent 五道门(冻结模型链,外部 LLM 调用);
#     corrections 以 propose 模式落 pending,供人工审阅档使用。adapters 全不可用
#     时优雅降级为 deterministic 行为。
#   agents-apply(默认) —— deterministic 全部 + 公式 Agent 五道门,以 apply 模式全自动
#     应用(所有者 2026-07-18 裁决:撤销旧版"人工 accept 红线",安全兜底改由
#     orchestrator 内建机制承担——五道门/置信阈值 0.8/熔断阈值 0.6/自动回滚/
#     `.pre_agent.bak` 快照,编排层不再额外拦截)。adapters 全不可用时同样降级
#     为 deterministic 行为。
#   off —— 现状,只转换不后处理,零调用。
QUALITY_DISCOVERY_MODES = ("off", "signals")
QUALITY_LEARN_MODES = ("off", "package")

# 审计报告 schema 版本(与 source_audit.audit_document 输出的 schema_version 一致;
# 独立维护,断点恢复的指纹过期判定据此比对)。
AUDIT_SCHEMA_VERSION = 6

# 路线 B 采信/审计生产 profile——Task 13 标定冻结(所有者 2026-07-17 批准)。
# 采信阈值维持标定前取值(fixture 证据:构造错误页采信 NED 0.0097,NED 0.2 是
# 20× 余量的安全阀);审计阈值见 source_audit.ROUTE_B_V1_THRESHOLDS 的标定注释。
# F5:生产采信阈值与审计 dry-run 采信阈值同值,单一来源在 source_audit——本处
# 直接引用,不再双写三个字面量(值不变,冻结锁行为一致)。
ROUTE_B_ADOPTION_THRESHOLDS = DRY_RUN_ADOPTION_THRESHOLDS
ROUTE_B_AUDIT_THRESHOLDS = ROUTE_B_V1_THRESHOLDS
DERIVED_RECONSTRUCT_PROFILE = "textbooks-reconstruct-v2"
DERIVED_NO_ADOPTION_PROFILE = "not-applicable-v1"


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


def _page_corrections(corrections: list[dict], page: int) -> list[dict]:
    return [item for item in corrections if item.get("page") == page]


def _adoption_cache_fields(
        adopt_ctx: "_AdoptContext | None") -> tuple[str, dict]:
    if adopt_ctx is None:
        return DERIVED_NO_ADOPTION_PROFILE, {}
    thresholds = adopt_ctx.thresholds
    return THRESHOLD_PROFILE_V1, {
        "adoption_min_char_ratio": thresholds.adoption_min_char_ratio,
        "adoption_max_char_ratio": thresholds.adoption_max_char_ratio,
        "adoption_max_ned": thresholds.adoption_max_ned,
    }


def _ocr_page_sha256(work_dir: str, page: int) -> str:
    path = cp.page_res_path(work_dir, page)
    if os.path.isfile(path):
        return dc.sha256_file(path)
    return dc.sha256_json({"missing_page_result": page})


def _cache_key_for_page(
        *, stem: str, source_pdf_sha256: str, dpi: int, work_dir: str,
        page: int, corrections: list[dict],
        adopt_ctx: "_AdoptContext | None",
        page_overlays: list[dict]) -> dict:
    adoption_profile, adoption_thresholds = _adoption_cache_fields(adopt_ctx)
    return dc.build_cache_key(
        stem=stem,
        source_pdf_sha256=source_pdf_sha256,
        dpi=dpi,
        ocr_page_sha256=_ocr_page_sha256(work_dir, page),
        page_corrections=_page_corrections(corrections, page),
        page_overlay=page_overlays,
        adoption_thresholds=adoption_thresholds,
        reconstruct_profile=DERIVED_RECONSTRUCT_PROFILE,
        adoption_profile=adoption_profile,
    )


def _decisions_from_cache(record: dict) -> list[AdoptionDecision]:
    return [
        AdoptionDecision(
            block_id=item["block_id"],
            content_source=item["content_source"],
            reasons=list(item.get("reasons") or []),
            block_ned=item.get("block_ned"),
            adopted_text=item.get("adopted_text"),
        )
        for item in record.get("adoption_decisions", [])
    ]


def _page_markdown(fragments: list[dict]) -> str:
    return "\n\n".join(fragment["md"] for fragment in fragments) + "\n"


def _apply_page_overlays(
        page_md: str, fragments: list[dict],
        page_overlays: list[dict]) -> tuple[str, list[dict]]:
    current = page_md
    block_ids = [
        block_id for fragment in fragments
        for block_id in fragment.get("bids", fragment.get("block_ids", []))
    ]
    for overlay in page_overlays:
        if overlay.get("kind") != "exact_page_replacement":
            raise RuntimeError(f"未知 page overlay: {overlay.get('kind')!r}")
        if dc.sha256_text(current) != overlay.get("baseline_page_sha256"):
            raise RuntimeError(
                f"page overlay baseline 漂移(page={overlay.get('page')})，拒绝静默丢修复")
        current = overlay.get("replacement_page_markdown")
        if not isinstance(current, str) or \
                dc.sha256_text(current) != overlay.get("replacement_page_sha256"):
            raise RuntimeError(
                f"page overlay replacement 损坏(page={overlay.get('page')})")
        fragment_md = current[:-1] if current.endswith("\n") else current
        fragments = [{"bids": block_ids, "md": fragment_md}]
    return current, fragments


def _commit_derived_cache(
        work_dir: str, page_records: list[dict],
        pending_records: list[dict], final_markdown: str) -> None:
    for record in pending_records:
        dc.write_page_cache(work_dir, record)
    index = dc.build_document_index(page_records, final_markdown=final_markdown)
    dc.write_document_index(work_dir, index)


def _publish_assembled_result(layout: DocLayout, result: dict) -> None:
    """Atomically publish cache/index and, unless preserved, the final MD."""
    md = result["md"]
    pending_pages = [record["page"] for record in result["_pending_cache_records"]]
    cache_snapshot = dc.snapshot_cache_files(layout.work_dir, pending_pages)
    try:
        if result["_page_cache_records"]:
            _commit_derived_cache(
                layout.work_dir,
                result["_page_cache_records"],
                result["_pending_cache_records"],
                md,
            )
        if result.get("_preserve_existing_md"):
            return
        os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
        encoded = md.encode("utf-8")
        if (os.path.isfile(layout.md_path)
                and Path(layout.md_path).read_bytes() == encoded):
            return
        temp = layout.md_path + ".assemble.tmp"
        try:
            with open(temp, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, layout.md_path)
        finally:
            try:
                os.remove(temp)
            except FileNotFoundError:
                pass
    except Exception:
        dc.restore_cache_files(cache_snapshot)
        raise


def _reconcile_legacy_result(result: dict, existing_markdown: str) -> None:
    """Make a no-cache legacy candidate exactly reproduce the current truth."""
    if result["md"] != existing_markdown:
        try:
            reconciled = dc.reconcile_page_overlays(
                result["_page_cache_records"],
                current_final_markdown=existing_markdown,
            )
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "legacy_cache_migration_unresolved: 页缓存候选与当前最终 MD "
                f"无法安全归属到单页；保留正本且不提交缓存: {exc}") from exc
        result["_page_cache_records"] = reconciled
        result["_pending_cache_records"] = list(reconciled)
        result["md"] = existing_markdown
    result["_preserve_existing_md"] = True


def assemble(work_dir: str, total: int, stem: str, assets_dir: str,
             pdf_path: str | None, dpi: int,
             corrections_dir: str | None = None,
             adopt_ctx: "_AdoptContext | None" = None,
             *, derived_source_sha256: str | None = None,
             affected_pages: set[int] | None = None,
             corrections_override: list[dict] | None = None,
             page_cache_overrides: dict[int, dict] | None = None) -> dict:
    """按页序读检查点 → 应用公式修正叠加层(§2,可选,无 corrections.json 则无影响)
    → (hybrid)块级采信替换 → 重组 md + 补裁缺失资产 + 汇总告警/双栏嫌疑页/缺失资产清单。

    adopt_ctx 非空(路线 B hybrid)时,对每页在 reconstruct 前做源文本采信替换,
    并把逐页采信决策收集进 adopt_ctx.decisions_by_page(供审计消费)。采信/审计
    的异常由调用方在整本层面隔离——本函数不吞异常,让其上抛以触发整本回退。"""
    doc_dir = corrections_dir or os.path.dirname(os.path.normpath(work_dir))
    corrections = (
        list(corrections_override)
        if corrections_override is not None
        else load_corrections(doc_dir)
    )
    prior_index = dc.read_document_index(work_dir)
    document_newline_style = (
        prior_index["newline_style"] if prior_index is not None
        else dc.NEWLINE_LF
    )
    forced_pages = set(affected_pages or ())
    invalid_forced = sorted(page for page in forced_pages
                            if not isinstance(page, int) or isinstance(page, bool)
                            or page < 1 or page > total)
    if invalid_forced:
        raise ValueError(f"affected_pages 超出文档页范围: {invalid_forced}")
    md_pages: list[str] = []
    all_blocks: list[dict] = []
    all_warnings: list[dict] = []
    missing_assets: list[str] = []
    column_layout_suspected: list[int] = []
    page_cache_records: list[dict] = []
    pending_cache_records: list[dict] = []
    for i in range(1, total + 1):
        blocks = cp.load_page_blocks(work_dir, i)
        if corrections:
            blocks = apply_corrections(blocks, i, corrections)
        cache_key = None
        cached = None
        existing_cache = None
        page_overlays: list[dict] = []
        if derived_source_sha256 is not None:
            existing_cache = (
                (page_cache_overrides or {}).get(i)
                or dc.read_page_cache(work_dir, i)
            )
            if existing_cache is not None:
                page_overlays = list(existing_cache.get("page_overlays") or [])
            cache_key = _cache_key_for_page(
                stem=stem, source_pdf_sha256=derived_source_sha256,
                dpi=dpi, work_dir=work_dir, page=i, corrections=corrections,
                adopt_ctx=adopt_ctx, page_overlays=page_overlays)
            if i not in forced_pages and existing_cache is not None and \
                    dc.page_cache_is_fresh(existing_cache, cache_key):
                cached = existing_cache
        if cached is not None:
            decisions = _decisions_from_cache(cached)
            if adopt_ctx is not None:
                adopt_ctx.decisions_by_page[i] = decisions
            blocks = apply_adoption(blocks, decisions)
            page_md = cached["page_markdown"]
            warnings = list(cached["warnings"])
            expected = list(cached["expected_assets"])
            column_suspected = bool(cached["column_layout_suspected"])
            page_cache_records.append(cached)
        else:
            if adopt_ctx is not None:
                blocks = adopt_ctx.adopt_page(blocks, i)
                decisions = adopt_ctx.decisions_by_page.get(i, [])
            else:
                decisions = []
            fragments, warnings = reconstruct_fragments(blocks, stem=stem, page=i)
            page_md = _page_markdown(fragments)
            if page_overlays:
                page_md, fragments = _apply_page_overlays(
                    page_md, fragments, page_overlays)
            expected = _expected_visual_filenames(blocks, i)
            column_suspected = detect_column_layout(blocks)
            if cache_key is not None:
                record = dc.materialize_page_cache(
                    page=i,
                    cache_key=cache_key,
                    adopted_decisions=decisions,
                    fragments=fragments,
                    page_markdown=page_md,
                    warnings=warnings,
                    expected_assets=expected,
                    column_layout_suspected=column_suspected,
                    page_overlays=page_overlays,
                )
                page_cache_records.append(record)
                pending_cache_records.append(record)
        all_blocks.extend(blocks)
        _backfill_missing_assets(blocks, pdf_path, dpi, work_dir, assets_dir, i)
        missing_assets.extend(f for f in expected
                              if not os.path.exists(os.path.join(assets_dir, f)))
        if column_suspected:
            column_layout_suspected.append(i)
        all_warnings.extend(warnings)
        if page_md.strip():
            md_pages.append(page_md)
    canonical_md = "\n\n".join(md_pages) + "\n"
    final_md = (
        dc.assemble_document(
            page_cache_records, newline_style=document_newline_style)
        if page_cache_records else canonical_md
    )
    return {
        "md": final_md,
        "blocks": all_blocks,
        "warnings": all_warnings,
        "missing_assets": missing_assets,
        "column_layout_suspected": column_layout_suspected,
        "_page_cache_records": page_cache_records,
        "_pending_cache_records": pending_cache_records,
    }


def _build_reassembled_result(
        layout: DocLayout, pdf_path: str | None, dpi: int,
        affected_pages: set[int] | None = None,
        corrections_override: list[dict] | None = None,
        page_cache_overrides: dict[int, dict] | None = None) -> tuple[dict, bool] | None:
    """Build a candidate plus cache records; never writes MD or cache."""
    manifest = cp.load_manifest(layout.work_dir)
    if not manifest:
        return None
    total = manifest["fingerprint"]["page_count"]
    if not total:
        return None
    report = _load_audit_report(layout.source_audit_path)
    if report is None and manifest.get("route") == "B":
        raise RuntimeError(
            f"路线 B 书审计报告缺失或损坏: {layout.source_audit_path}。"
            "无法判定是否 hybrid 采信书,拒绝重组以免静默丢采信;请重跑 convert 再生报告。")
    issue_counts = ((report or {}).get("summary") or {}).get("issue_counts") or {}
    hybrid_recorded = (report is not None
                       and report.get("born_digital_mode") == "hybrid"
                       and report.get("adoption_source") == "recorded"
                       and not issue_counts.get("adoption_error"))
    had_page_cache = bool(page_cache_overrides) or any(
        dc.page_cache_path(layout.work_dir, page).is_file()
        for page in range(1, total + 1)
    )
    pdf_doc = None
    source_pdf_sha256 = None
    if pdf_path and os.path.exists(pdf_path):
        if not cp.fingerprint_ok(manifest, pdf_path, dpi):
            raise RuntimeError(
                f"源 PDF 与检查点指纹不符(换源或 DPI 变): {pdf_path!r}。"
                "重放采信会把新 PDF 的词映射到旧 OCR 块上产出错位文本——拒绝重组。")
        source_pdf_sha256 = dc.sha256_file(pdf_path)
    if hybrid_recorded:
        if source_pdf_sha256 is None:
            raise RuntimeError(
                f"hybrid 书重组需要源 PDF 校验缓存/重放采信,但未提供或不存在: {pdf_path!r}。"
                "拒绝在无强源指纹下复用缓存或回退成纯 OCR。")
        pdf_doc = fitz.open(pdf_path)
    try:
        adopt_ctx = (_AdoptContext(pdf_doc, layout.work_dir, ROUTE_B_ADOPTION_THRESHOLDS)
                     if pdf_doc is not None else None)
        result = assemble(
            layout.work_dir, total, layout.stem, layout.assets_dir,
            pdf_path, dpi, corrections_dir=layout.doc_work_dir,
            adopt_ctx=adopt_ctx,
            derived_source_sha256=source_pdf_sha256,
            affected_pages=affected_pages,
            corrections_override=corrections_override,
            page_cache_overrides=page_cache_overrides,
        )
    finally:
        if pdf_doc is not None:
            pdf_doc.close()

    legacy_hybrid_migration = hybrid_recorded and not had_page_cache
    if legacy_hybrid_migration:
        if not os.path.isfile(layout.md_path):
            raise RuntimeError(
                "legacy_cache_migration_unresolved: 旧 hybrid 缺最终 MD，"
                "无法证明页缓存候选等价")
        with open(layout.md_path, encoding="utf-8", newline="") as handle:
            existing = handle.read()
        _reconcile_legacy_result(result, existing)
    return result, legacy_hybrid_migration


def build_reassembled_markdown(
        layout: DocLayout, pdf_path: str | None, dpi: int,
        affected_pages: set[int] | None = None) -> str | None:
    """只构建、不覆盖最终 MD 的幂等重组结果。

    读 _work 检查点 → 应用采纳的修正 → (hybrid 书)重放采信 → 重组。
    供事务提交前比较候选内容；不写 md/selfcheck/manifest。
    manifest 缺失/total 为 0 时返回 None。

    hybrid 书(审计报告 born_digital_mode=hybrid 且 adoption_source=recorded,
    排除 adoption_error 整本回退的情形)必须重建 _AdoptContext 重放采信:检查点
    永远是纯 OCR(见 _finalize_hybrid),不带 adopt_ctx 重组会把全书采信块静默
    回退成 OCR。采信纯确定性(checkpoint+源 PDF+冻结阈值,见 _AdoptContext),
    重放与初次转换逐字节一致。缺源 PDF 时 fail-loud 拒绝重组——静默丢采信比
    报错危险得多。"""
    built = _build_reassembled_result(
        layout, pdf_path, dpi, affected_pages=affected_pages)
    return None if built is None else built[0]["md"]


def reassemble_md(
        layout: DocLayout, pdf_path: str | None, dpi: int,
        affected_pages: set[int] | None = None) -> str | None:
    """幂等对账并覆盖写 layout.md_path。

    构建逻辑统一委托 build_reassembled_markdown()，保证预览候选、debug 采纳
    和正式转换使用同一条重组路径。只写 md，不写 selfcheck、不动 manifest。
    """
    built = _build_reassembled_result(
        layout, pdf_path, dpi, affected_pages=affected_pages)
    if built is None:
        return None
    result, _legacy_hybrid_migration = built
    _publish_assembled_result(layout, result)
    return layout.md_path


def _register_deferred(pdf_path: str, out_dir: str, stem: str) -> dict:
    deferred = os.path.join(out_dir, "_deferred_born_digital")
    os.makedirs(deferred, exist_ok=True)
    with open(os.path.join(deferred, stem + ".txt"), "w", encoding="utf-8") as f:
        f.write(pdf_path + "\n")
    return {"route": "B", "md_path": None, "selfcheck": None, "failed_pages": [],
            "source_audit": None, "born_digital_mode": "defer", "adoption_error": False,
            "formula_repair": {"mode": "off", "reason": "route_b_deferred_no_md"},
            "quality_repair": {"mode": "off"},
            "completion_status": CompletionStatus.OK}


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
                 expected_mode: str, work_dir: str,
                 corrections_path: str | None = None) -> bool:
    """审计报告是否新鲜:schema_version / born_digital_mode / PDF 指纹(页数+字节)/
    OCR 结果集 / corrections 文件强哈希均未变。任一变化即判过期,调用方据此
    只重算 adoption+reconstruct+audit(不重跑 OCR)。无 corrections 文件时继续
    接受不含 corrections_fingerprint 的旧版报告。

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
    stored_ocr = report.get("ocr_fingerprint") or {}
    current_ocr = cp.ocr_results_fingerprint(
        work_dir, cur["page_count"], dpi)
    # Pre-result-set reports are intentionally stale even when PDF/DPI match.
    # All audit inputs must match, including corrected/new page JSON and the
    # current manifest failure state.
    if any(stored_ocr.get(key) != value for key, value in current_ocr.items()):
        return False
    if report.get("corrections_fingerprint") != \
            corrections_file_fingerprint(corrections_path):
        return False
    # 采信/审计异常留下的 SUSPECT 报告不得当 fresh 复用——下次 resume 必须重算,
    # 否则采信/审计已能成功仍顶着陈旧错误报告(Minor 3)。
    issue_counts = (report.get("summary") or {}).get("issue_counts") or {}
    if issue_counts.get("adoption_error") or issue_counts.get("audit_error"):
        return False
    return True


def _fingerprint_fields(
        pdf_path: str, dpi: int, work_dir: str) -> tuple[dict, dict]:
    cur = cp.pdf_fingerprint(pdf_path)
    return (
        {"size_bytes": cur["size_bytes"], "page_count": cur["page_count"]},
        cp.ocr_results_fingerprint(work_dir, cur["page_count"], dpi),
    )


def _not_applicable_report(pdf_path: str, layout: DocLayout, dpi: int) -> dict:
    """A 路(无文本层)最小审计报告:显式 NOT_APPLICABLE(跟随 selfcheck 惯例——
    始终产出一个可发现的报告文件,而不是静默不写)。"""
    pdf_fp, ocr_fp = _fingerprint_fields(pdf_path, dpi, layout.work_dir)
    report = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "stem": layout.stem, "route": "A", "born_digital_mode": "n/a",
        "pdf_fingerprint": pdf_fp, "ocr_fingerprint": ocr_fp,
        "threshold_profile": None, "adoption_source": "n/a",
        "summary": {"status": "NOT_APPLICABLE", "pages": ocr_fp["page_count"],
                    "scorable_pages": 0, "suspect_pages": [], "issue_counts": {}},
        "pages": [],
    }
    corrections_fp = corrections_file_fingerprint(layout.corrections_path)
    if corrections_fp is not None:
        report["corrections_fingerprint"] = corrections_fp
    return report


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
    pdf_fp, ocr_fp = _fingerprint_fields(pdf_path, dpi, layout.work_dir)
    issue = {"code": code, "block_id": None, "detail": detail}
    report = {
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
    corrections_fp = corrections_file_fingerprint(layout.corrections_path)
    if corrections_fp is not None:
        report["corrections_fingerprint"] = corrections_fp
    return report


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


def _sha256_path(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_agents_stage(layout: DocLayout, pdf_path: str, dpi: int,
                      agent_mode: str,
                      agent_specs: list[RepairAgentSpec] | None = None,
                      workers: int | None = None,
                      defer_publish: bool = False) -> dict:
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
        adapters = (
            default_adapters() if agent_specs is None
            else [CliAdapter(spec.provider, model=spec.model, effort=spec.effort)
                  for spec in agent_specs]
        )
    except Exception as e:                         # noqa: BLE001 adapter 构造异常不逃逸
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    available = [a for a in adapters if _safe_probe(a)]
    if not available:
        return {"status": "degraded_deterministic",
                "reason": "无可用 formula-agent CLI adapter(PATH 未装/未登录),"
                          "已降级为 deterministic 行为,未调用任何 agent"}
    try:
        kwargs = {"collect_fn": crops_only_collect} if agent_mode == "apply" else {}
        if workers is not None:
            kwargs.update(per_provider=workers, max_workers=workers)
        report = run_agents(layout, adapters=available, pdf_path=pdf_path or "",
                            dpi=dpi, mode=agent_mode,
                            defer_publish=defer_publish, **kwargs)
        result = {"status": "ok", "run_mode": report.mode,
                "n_candidates": report.n_candidates, "applied": report.applied,
                "rejected": len(report.rejected), "pending_ids": report.pending_ids,
                "circuit_broken": report.circuit_broken,
                "rolled_back": report.rolled_back, "reason": report.reason}
        if defer_publish:
            result["corrections_payload"] = report.corrections_payload
        return result
    except Exception as e:                         # noqa: BLE001 agent 链异常不逃逸
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _run_formula_repair(
        layout: DocLayout, pdf_path: str, mode: str, dpi: int,
        agent_specs: list[RepairAgentSpec] | None = None,
        workers: int | None = None,
        defer_publish: bool = False) -> dict:
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
        result["agents"] = _run_agents_stage(
            layout, pdf_path, dpi, agent_mode, agent_specs=agent_specs,
            workers=workers, defer_publish=defer_publish)

    return result


def _run_quality_repair(layout: DocLayout, mode: str, agents: list[str],
                        discovery: str, learn: str, timeout: int, *,
                        workers: int = 1, max_rounds: int = 1) -> dict:
    """转换完成后的独立质量阶段。

    ``apply`` 只把 patch plan 命中的 Markdown 区间事务写回；不会重跑 OCR，
    也不会重组整本。自动模式可给 ``max_rounds > 1``，每轮重新审计当前正本，
    无剩余 finding、回滚或状态无进展时立即停止。
    """
    from datetime import datetime

    from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec
    from scripts.pipelines.textbooks.quality_repair.cli import default_registry
    from scripts.pipelines.textbooks.quality_repair.engine import (
        audit_document, auto_apply, propose_document,
    )
    from scripts.pipelines.textbooks.quality_repair.gates import build_default_gates
    from scripts.pipelines.textbooks.quality_repair.models import DetectorContext

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    if workers <= 0 or max_rounds <= 0:
        raise ValueError("workers and max_rounds must be positive")
    run_dir = os.path.join(layout.quality_repair_dir, run_id)
    registry = default_registry(discovery=discovery)
    specs = [AgentSpec.parse(value) for value in agents]
    context = DetectorContext.from_paths(
        stem=layout.stem, md_path=layout.md_path,
        work_dir=layout.work_dir, run_dir=run_dir)
    if mode == "audit":
        summary = audit_document(context, registry=registry)
        result = {"mode": mode, "status": summary.status,
                  "findings": summary.finding_count, "applied": 0,
                  "severity_counts": dict(summary.counts_by_severity),
                  "conflicts": 0, "report_dir": summary.report_dir}
    elif mode == "propose":
        proposed = propose_document(
            context, registry=registry, agent_specs=specs,
            agent_timeout=timeout, learn=learn, agent_workers=workers)
        result = {"mode": mode, "status": proposed.summary.status,
                  "findings": proposed.summary.finding_count, "applied": 0,
                  "severity_counts": dict(proposed.summary.counts_by_severity),
                  "conflicts": len(proposed.patch_plan.conflicts),
                  "proposals": len(proposed.patch_plan.proposals),
                  "report_dir": proposed.summary.report_dir}
    else:
        result = auto_apply(
            context,
            registry=registry,
            agent_specs=specs,
            agent_timeout=timeout,
            learn=learn,
            agent_workers=workers,
            max_rounds=max_rounds,
            gate_factory=lambda round_context: build_default_gates(
                round_context.md_path, round_context.run_dir),
        )
        if result["applied"]:
            _refresh_quality_selfcheck(layout)
    return _write_quality_latest(layout, result)


def _write_quality_latest(layout: DocLayout, result: dict) -> dict:
    """Atomically publish the repair terminal state bound to current MD bytes."""
    payload = dict(result)
    if os.path.isfile(layout.md_path):
        payload["after_sha256"] = hashlib.sha256(
            Path(layout.md_path).read_bytes()).hexdigest()
    os.makedirs(layout.quality_repair_dir, exist_ok=True)
    latest = os.path.join(layout.quality_repair_dir, "latest.json")
    with open(latest + ".tmp", "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(latest + ".tmp", latest)
    return payload


def _refresh_final_selfcheck(
        layout: DocLayout, check: dict | None, *, persist: bool) -> dict:
    """Refresh MD-derived Tier0 fields and optionally persist the same state."""
    updated = dict(check or {})
    try:
        with open(layout.md_path, encoding="utf-8", newline="") as handle:
            md = handle.read()
        updated["katex_incompat"] = katex_incompat_scan(md)
        updated["formula_suspicions"] = summarize_suspicions(md)
        updated["inline_math_delimiter_ws"] = inline_math_delimiter_ws_scan(md)
        if persist:
            os.makedirs(layout.doc_work_dir, exist_ok=True)
            temp = layout.selfcheck_path + ".final.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
            os.replace(temp, layout.selfcheck_path)
    except Exception as exc:                       # noqa: BLE001 最终 MD 已过门禁,sidecar 刷新不回滚
        print(f"[quality_repair] selfcheck 刷新失败: {type(exc).__name__}: {exc}")
    return updated


def _refresh_quality_selfcheck(layout: DocLayout) -> None:
    """Direct quality API compatibility: refresh an existing sidecar in place."""
    if not os.path.exists(layout.selfcheck_path):
        return
    try:
        with open(layout.selfcheck_path, encoding="utf-8") as handle:
            check = json.load(handle)
    except Exception as exc:                       # noqa: BLE001 sidecar is diagnostic
        print(f"[quality_repair] selfcheck 读取失败: {type(exc).__name__}: {exc}")
        return
    _refresh_final_selfcheck(layout, check, persist=True)


def _determine_completion_status(
        *, failed_pages: list[dict], selfcheck: dict | None,
        source_audit: dict | None, adoption_error: bool,
        formula_repair: dict | None,
        quality_repair: dict | None) -> CompletionStatus:
    """Collapse existing conversion/repair signals into the 0/2 completion contract."""
    if failed_pages or adoption_error:
        return CompletionStatus.SUSPECT

    if source_audit_blocks_completion(source_audit, quality_repair):
        return CompletionStatus.SUSPECT

    check = selfcheck or {}
    inline_ws = check.get("inline_math_delimiter_ws") or {}
    if (check.get("missing") or check.get("missing_assets")
            or check.get("katex_incompat")
            or int(inline_ws.get("count") or 0) > 0):
        return CompletionStatus.SUSPECT
    if (check.get("column_layout_suspected")
            and not quality_final_is_conclusive(quality_repair)):
        return CompletionStatus.SUSPECT

    formula = formula_repair or {}
    if formula.get("status") == "error":
        return CompletionStatus.SUSPECT
    for stage in ("katex_scan", "katex_triage", "formula_candidates"):
        if (formula.get(stage) or {}).get("status") == "error":
            return CompletionStatus.SUSPECT
    candidate_count = int(
        (formula.get("formula_candidates") or {}).get("count") or 0)
    if formula.get("mode") == "deterministic" and candidate_count:
        return CompletionStatus.SUSPECT
    agents = formula.get("agents") or {}
    if isinstance(agents, dict):
        if (agents.get("status") == "error" or agents.get("pending_ids")
                or agents.get("circuit_broken") or agents.get("rolled_back")
                or int(agents.get("rejected") or 0) > 0):
            return CompletionStatus.SUSPECT
        if agents.get("status") == "degraded_deterministic" and candidate_count:
            return CompletionStatus.SUSPECT
        if (formula.get("mode") == "agents-apply" and candidate_count
                and agents.get("run_mode") != "apply"):
            return CompletionStatus.SUSPECT

    quality = quality_repair or {}
    if (quality.get("mode") != "off"
            and quality.get("status") not in {None, "OK"}):
        return CompletionStatus.SUSPECT
    if quality.get("rolled_back") or int(quality.get("conflicts") or 0) > 0:
        return CompletionStatus.SUSPECT
    return CompletionStatus.OK


def _convert_pdf_impl(pdf_path: str, deliverables_dir: str | None = None,
                      work_dir: str | None = None, dpi: int = cp.DEFAULT_DPI,
                      write_selfcheck: bool = True, force_ocr: bool = False,
                      work_seconds: float = DEFAULT_WORK_SECONDS,
                      rest_seconds: float = DEFAULT_REST_SECONDS,
                      born_digital_mode: str = "hybrid",
                      formula_repair: str = "deterministic",
                      quality_repair: str = "apply",
                      quality_agents: list[str] | None = None,
                      quality_discovery: str = "signals",
                      quality_learn: str = "off",
                      quality_agent_timeout: int = 300,
                      formula_agents: list[str | RepairAgentSpec] | None = None,
                      repair_workers: int | None = None,
                      quality_max_rounds: int = 1,
                      repair_auto: bool = False) -> dict:
    if born_digital_mode not in BORN_DIGITAL_MODES:
        raise ValueError(f"born_digital_mode 须为 {BORN_DIGITAL_MODES},收到 {born_digital_mode!r}")
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
    if repair_workers is not None and repair_workers <= 0:
        raise ValueError("repair_workers 必须大于 0")
    if quality_max_rounds <= 0:
        raise ValueError("quality_max_rounds 必须大于 0")
    quality_agents = list(quality_agents or [])
    formula_agent_specs = (
        None if formula_agents is None
        else [value if isinstance(value, RepairAgentSpec)
              else RepairAgentSpec.parse(value)
              for value in formula_agents]
    )
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
    # Legacy resume guard: old hybrid books can have complete OCR checkpoints
    # plus a manually/agent-repaired final MD but no page cache.  Capture that
    # truth before finalization; it must be reconciled into page overlays, never
    # silently overwritten by a whole-document rebuild.
    legacy_hybrid_resume_md: str | None = None
    if (route == "B" and born_digital_mode == "hybrid" and done == total
            and total > 0 and os.path.isfile(layout.md_path)
            and not any(dc.derived_dir(work_dir_).glob("page_*.json"))):
        with open(layout.md_path, encoding="utf-8", newline="") as handle:
            legacy_hybrid_resume_md = handle.read()
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
    # source_audit reads failure state from the on-disk manifest.  Persist the
    # just-reconciled state before auditing so a recovered page cannot remain
    # falsely marked page_failed until the next run.
    cp.save_manifest(work_dir_, manifest)

    # 从检查点重组(每次运行都做,部分完成也产出部分 md);hybrid 走采信整本隔离。
    # 顺带补裁续跑/历史检查点缺失的资产。
    is_hybrid = route == "B" and born_digital_mode == "hybrid"
    adoption_error = False
    audit_report: dict | None = None
    source_pdf_sha256 = dc.sha256_file(pdf_path)
    if is_hybrid:
        result, audit_report, adoption_error = _finalize_hybrid(
            pdf_path, layout, work_dir_, total, stem, assets_dir, dpi,
            source_pdf_sha256)
        if legacy_hybrid_resume_md is not None:
            _reconcile_legacy_result(result, legacy_hybrid_resume_md)
    else:
        result = assemble(work_dir_, total, stem, assets_dir, pdf_path, dpi,
                          corrections_dir=layout.doc_work_dir,
                          derived_source_sha256=source_pdf_sha256)
        # 审计落盘提前到这里(非 hybrid 路由):selfcheck 的 source_audit/
        # ocr_degeneration 紧凑字段(计划 §7.2)要读到本轮刚落盘的审计报告,
        # 顺序必须先于下面的 selfcheck 组装(hybrid 路由的审计已在
        # _finalize_hybrid 内完成,顺序天然满足,不需要额外挪动)。
        audit_report = _finalize_audit(route, born_digital_mode, pdf_path, layout, dpi)

    md, all_blocks = result["md"], result["blocks"]
    # Auto owns the only final publication.  Keep the freshly assembled
    # baseline and page-cache records in memory until formula + quality close.
    if not repair_auto:
        _publish_assembled_result(layout, result)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    check["formula_suspicions"] = summarize_suspicions(md)
    check["inline_math_delimiter_ws"] = inline_math_delimiter_ws_scan(md)
    check.update(aggregate_warnings(result["warnings"]))
    check["missing_assets"] = result["missing_assets"]
    check["column_layout_suspected"] = result["column_layout_suspected"]
    pdf_fp, _ocr_fp = _fingerprint_fields(pdf_path, dpi, layout.work_dir)
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

    if repair_auto:
        from scripts.pipelines.textbooks.auto_repair import (
            run_unified_auto_repair,
        )
        unified = run_unified_auto_repair(
            layout,
            pdf_path,
            dpi=dpi,
            formula_mode=formula_repair,
            formula_agent_specs=formula_agent_specs,
            quality_agents=quality_agents,
            discovery=quality_discovery,
            learn=quality_learn,
            timeout=quality_agent_timeout,
            workers=repair_workers or 1,
            max_rounds=quality_max_rounds,
            baseline_result=result,
        )
        formula_repair_result = unified["formula_repair"]
        quality_repair_result = unified["quality_repair"]
        if (unified["status"] != "OK"
                and (
                    not os.path.isfile(layout.md_path)
                    or Path(layout.md_path).read_bytes()
                    != result["md"].encode("utf-8")
                )):
            # A failed first conversion still returns a clearly SUSPECT
            # baseline artifact.  It is written once, never presented as the
            # repaired final document.
            _publish_assembled_result(layout, result)
        if write_selfcheck:
            os.makedirs(layout.doc_work_dir, exist_ok=True)
            with open(layout.formula_repair_path, "w", encoding="utf-8") as f:
                json.dump(
                    formula_repair_result, f, ensure_ascii=False, indent=2)
        quality_repair_result = _write_quality_latest(
            layout, quality_repair_result)
        check = _refresh_final_selfcheck(
            layout, check, persist=write_selfcheck)
    else:
        # 转换本体到这里已成功、md/selfcheck 已落盘——后处理失败隔离硬要求(Task B):
    # 公式修复环任何异常都不得改变本次转换已经成功这个事实,顶层兜底一次(内部
    # _run_formula_repair 逐步骤已各自 try/except,这里是双保险,防止其自身的
    # 编排代码本身出问题,如 default_adapters() 之外的意外)。
        try:
            formula_repair_result = _run_formula_repair(
                layout, pdf_path, formula_repair, dpi,
                agent_specs=formula_agent_specs, workers=repair_workers)
        except Exception as e:                      # noqa: BLE001 legacy stage isolation
            print(f"[textbooks] 公式修复后处理异常(md/selfcheck 完好,不受影响): "
                  f"{type(e).__name__}: {e}")
            formula_repair_result = {"mode": formula_repair, "status": "error",
                                     "error": f"{type(e).__name__}: {e}"}

    # 公式 Agent 可能已改最终 MD；quality detector 读取 selfcheck sidecar 前，
    # 必须先把其中所有依赖最终 MD 的字段刷新为同一时刻的真相。
        check = _refresh_final_selfcheck(
            layout, check, persist=write_selfcheck)

    # 落盘供批量收尾分诊(Review Important):agents-apply 的熔断/回滚等安全网
    # 触发事件此前只留在子进程 stdout 里,batch 汇总读不到——同 selfcheck 写盘
    # 同一套 write_selfcheck 开关,不新造旗标。
        if write_selfcheck:
            os.makedirs(layout.doc_work_dir, exist_ok=True)
            with open(layout.formula_repair_path, "w", encoding="utf-8") as f:
                json.dump(formula_repair_result, f, ensure_ascii=False, indent=2)

    # quality_repair 永远位于既有公式收尾之后，以最终组装 MD 为唯一真相源。
    # 默认 off 时不 import、不调用，不改变旧转换行为。
        if quality_repair == "off":
            quality_repair_result = {"mode": "off"}
        else:
            try:
                quality_repair_result = _run_quality_repair(
                    layout, quality_repair, quality_agents, quality_discovery,
                    quality_learn, quality_agent_timeout,
                    workers=repair_workers or 1,
                    max_rounds=quality_max_rounds)
            except Exception as e:                   # noqa: BLE001 legacy stage isolation
                print(f"[textbooks] quality repair 后处理异常(md/selfcheck 保持当前状态): "
                      f"{type(e).__name__}: {e}")
                quality_repair_result = {
                    "mode": quality_repair, "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                }

    # quality apply 可能再次改最终 MD。完成码与返回值必须基于最终版本，
    # 不能继续使用转换阶段留在内存中的旧 selfcheck。
        check = _refresh_final_selfcheck(
            layout, check, persist=write_selfcheck)

    completion_status = _determine_completion_status(
        failed_pages=manifest["failed_pages"], selfcheck=check,
        source_audit=audit_report, adoption_error=adoption_error,
        formula_repair=formula_repair_result,
        quality_repair=quality_repair_result)
    return {"route": route, "md_path": layout.md_path, "selfcheck": check,
            "failed_pages": manifest["failed_pages"],
            "source_audit": audit_report,
            "born_digital_mode": born_digital_mode if route == "B" else None,
            "adoption_error": adoption_error,
            "formula_repair": formula_repair_result,
            "quality_repair": quality_repair_result,
            "completion_status": completion_status}


def convert_pdf(pdf_path: str, deliverables_dir: str | None = None,
                work_dir: str | None = None, dpi: int = cp.DEFAULT_DPI,
                write_selfcheck: bool = True, force_ocr: bool = False,
                work_seconds: float = DEFAULT_WORK_SECONDS,
                rest_seconds: float = DEFAULT_REST_SECONDS,
                born_digital_mode: str = "hybrid",
                formula_repair: str = "deterministic",
                quality_repair: str = "apply",
                quality_agents: list[str] | None = None,
                quality_discovery: str = "signals",
                quality_learn: str = "off",
                quality_agent_timeout: int = 300,
                formula_agents: list[str | RepairAgentSpec] | None = None,
                repair_workers: int | None = None,
                quality_max_rounds: int = 1,
                repair_auto: bool = False) -> dict:
    """Run one document under a process-scoped lock.

    The thin wrapper deliberately acquires the lock before triage, OCR, or any
    post-processing. A competing invocation therefore cannot mutate any
    conversion or repair artifact.
    """

    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    resolved_deliverables = (
        deliverables_dir or os.path.dirname(os.path.abspath(pdf_path))
    )
    layout = resolve_layout(stem, resolved_deliverables, work_dir)
    run_id = f"convert-{os.getpid()}-{time.time_ns()}"
    with DocumentLock(
        layout.doc_work_dir,
        run_id=run_id,
        metadata={"operation": "convert_pdf", "source": os.path.abspath(pdf_path)},
    ):
        return _convert_pdf_impl(
            pdf_path,
            deliverables_dir,
            work_dir,
            dpi,
            write_selfcheck,
            force_ocr,
            work_seconds,
            rest_seconds,
            born_digital_mode,
            formula_repair,
            quality_repair,
            quality_agents,
            quality_discovery,
            quality_learn,
            quality_agent_timeout,
            formula_agents,
            repair_workers,
            quality_max_rounds,
            repair_auto,
        )


def _finalize_hybrid(pdf_path: str, layout: DocLayout, work_dir: str, total: int,
                     stem: str, assets_dir: str, dpi: int,
                     source_pdf_sha256: str) -> tuple[dict, dict | None, bool]:
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
                              corrections_dir=layout.doc_work_dir, adopt_ctx=ctx,
                              derived_source_sha256=source_pdf_sha256)
            decisions_by_page = ctx.decisions_by_page
        finally:
            pdf_doc.close()
    except Exception as e:                     # noqa: BLE001 采信/组装整本回退,绝不逃逸
        print(f"[textbooks] 采信/组装异常,整本回退等价 ocr 重建: {type(e).__name__}: {e}")
        result = assemble(work_dir, total, stem, assets_dir, pdf_path, dpi,
                          corrections_dir=layout.doc_work_dir,
                          derived_source_sha256=source_pdf_sha256)
        report = _error_audit_report(pdf_path, layout, dpi, route="B", mode="hybrid",
                                     code="adoption_error", detail=f"{type(e).__name__}: {e}",
                                     adoption_source="recorded")
        return result, (report if _write_audit_safely(report, layout.source_audit_path)
                        else None), True

    # ---- 第二段:审计(采信已成功,内容不再回退) ----
    try:
        existing = _load_audit_report(layout.source_audit_path)
        report = audit_document(pdf_path, layout, ROUTE_B_AUDIT_THRESHOLDS,
                                decisions_by_page, born_digital_mode="hybrid",
                                threshold_profile=THRESHOLD_PROFILE_V1,
                                prior_report=existing)
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
        if _audit_fresh(existing, pdf_path, dpi, "n/a", layout.work_dir,
                        corrections_path=layout.corrections_path) and \
                (existing.get("summary") or {}).get("status") == "NOT_APPLICABLE":
            return existing
        report = _not_applicable_report(pdf_path, layout, dpi)
        return report if _write_audit_safely(report, layout.source_audit_path) else None
    mode_label = born_digital_mode if route == "B" else None
    # audit_document 把 None 归一为 "unknown"(C/F 路),freshness 校验须比对同一标签。
    expected_mode = mode_label if mode_label is not None else "unknown"
    try:
        report = audit_document(pdf_path, layout, ROUTE_B_AUDIT_THRESHOLDS,
                                None, born_digital_mode=mode_label,
                                threshold_profile=THRESHOLD_PROFILE_V1,
                                prior_report=existing)
        report["route"] = route                # Important 1:落盘报告 route 字段失实修正
        return report if _write_audit_safely(report, layout.source_audit_path) else None
    except Exception as e:                     # noqa: BLE001 审计异常写 SUSPECT、不逃逸
        print(f"[textbooks] 审计(dry-run)异常,写 audit_error 报告: {type(e).__name__}: {e}")
        report = _error_audit_report(pdf_path, layout, dpi, route=route, mode=mode_label,
                                     code="audit_error", detail=f"{type(e).__name__}: {e}",
                                     adoption_source="dry_run")
        return report if _write_audit_safely(report, layout.source_audit_path) else None


def main() -> int:
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
    with keep_system_awake(enabled=not args.allow_sleep):
        res = convert_pdf(args.src, args.out, args.work_dir, dpi=args.dpi,
                          write_selfcheck=not args.no_selfcheck_json,
                          force_ocr=args.force_ocr,
                          work_seconds=args.work_hours * 3600,
                          rest_seconds=args.rest_minutes * 60,
                          born_digital_mode=args.born_digital_mode,
                          formula_repair=repair_policy.runtime_formula_repair,
                          quality_repair=repair_policy.runtime_quality_repair,
                          quality_agents=list(repair_policy.runtime_quality_agents),
                          quality_discovery=args.quality_discovery,
                          quality_learn=args.quality_learn,
                          quality_agent_timeout=args.quality_agent_timeout,
                          formula_agents=formula_agents,
                          repair_workers=repair_policy.workers,
                          quality_max_rounds=(
                              repair_policy.max_rounds
                              if repair_policy.quality_mode == "auto" else 1),
                          repair_auto=(
                              repair_policy.mode == "auto"
                              and not repair_policy.legacy_formula_explicit
                              and not repair_policy.legacy_quality_explicit))
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
    qr = res.get("quality_repair")
    if qr and qr.get("mode") != "off":
        print(f"[quality_repair] mode={qr.get('mode')} status={qr.get('status')} "
              f"findings={qr.get('findings')} applied={qr.get('applied', 0)} "
              f"report={qr.get('report_dir', '')}")
    return int(res.get("completion_status", CompletionStatus.OK))


if __name__ == "__main__":
    raise SystemExit(main())
