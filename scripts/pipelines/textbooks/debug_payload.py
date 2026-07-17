"""逐页 debug payload(纯数据,JSON 可序列化):供 debug_view 塞进 HTML 模板。

不碰浏览器、不做渲染——只把一页的 res.json 加工成:块叠框(按 label 分色)、
逐页 reconstruct 的 md(过修复后的 sanitize)、该页 selfcheck 信号、可选页图 base64
与该页 KaTeX 报错。渲染与判红全在浏览器 JS(与 scan_katex_errors.mjs 同版本 katex)。
"""
from __future__ import annotations

from scripts.pipelines.textbooks.images import is_visual_block
from scripts.pipelines.textbooks.reconstruct import reconstruct_fragments
from scripts.pipelines.textbooks.selfcheck import detect_column_layout, scan_formula_suspicions
from scripts.pipelines.textbooks.vision_repair import content_fingerprint

# 块 label → 叠框颜色。红(#ef4444)留给"渲染报错"高亮,不用于任何 label。
LABEL_COLORS: dict[str, str] = {
    "paragraph_title": "#a855f7",   # 紫
    "doc_title": "#9333ea",
    "text": "#3b82f6",              # 蓝
    "abstract": "#3b82f6",
    "reference_content": "#3b82f6",
    "content": "#60a5fa",
    "display_formula": "#14b8a6",   # 青
    "formula_number": "#5eead4",
    "algorithm": "#b45309",         # 棕
    "image": "#f97316",             # 橙
    "chart": "#fb923c",
    "table": "#22c55e",             # 绿
    "footnote": "#4ade80",
    "figure_title": "#16a34a",
    "header": "#9ca3af",            # 灰(噪声)
    "number": "#9ca3af",
    "header_image": "#9ca3af",
}
_UNKNOWN_COLOR = "#ec4899"          # 品红:没见过的 label,醒目提示
_NOISE_LABELS = {"header", "number", "header_image"}

# missing_samples/added_samples(source_audit.audit_prose,commit 22d53eb)展示
# 上限——上游 AuditThresholds.maximum_samples_per_block 已把生产报告本身截到
# 少量(默认 3 条/≤80 字符),这里是 debug 视图侧再加一道防御性上限(报告若
# 损坏/未来放宽上游上限,也不会把大段文本糊进页面)。上限可注入,不写死。
DEFAULT_AUDIT_SAMPLES_LIMIT = 20


def label_color(label: str) -> str:
    return LABEL_COLORS.get(label, _UNKNOWN_COLOR)


def _block_metrics_entry(block_metrics: dict, block_id) -> dict:
    """按 block_id 查 prose_audit.block_metrics 一条记录。

    block_metrics 在内存里(audit_prose 直接返回值)以 int 为键;但报告落盘
    经 json.dump/json.load 一轮后,JSON object 键一律变字符串——两种来源都要
    认得,查不到就是干净块(没有 missing/added 样本,不是异常)。
    """
    if not isinstance(block_metrics, dict):
        return {}
    entry = block_metrics.get(block_id)
    if entry is None:
        entry = block_metrics.get(str(block_id))
    return entry if isinstance(entry, dict) else {}


def _limited_samples(entry: dict, key: str, limit: int) -> tuple[list[str], bool]:
    """entry[key](missing_samples/added_samples)截断到 limit 条,返回
    (截断后列表, 是否被截断)。非 list/非字符串条目一律丢弃,不猜测。"""
    raw = entry.get(key)
    items = [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
    return items[:limit], len(items) > limit


def build_audit_payload(audit_page: dict | None,
                        samples_limit: int = DEFAULT_AUDIT_SAMPLES_LIMIT) -> dict | None:
    """把一页 source_audit 报告(schema v2 page_report)加工成 debug 视图只读展示
    所需字段:页级 status/issues、块级 provenance(含 missing_samples/
    added_samples——真实来自 prose_audit.block_metrics[block_id],commit
    22d53eb 补齐;无对应 issue 的块该报告本就不产字段,payload 侧统一降级为
    空列表,不是 None)、table_audit(其 structure/content issue 已随
    page-level issues 一起带 block_id,足以定位)。

    只展示,不重算:块的 "adopted_text"(若采信会是什么文本)只在报告本身携带
    该字段时透传,报告没有就是 None——不现场跑 AdoptionDecision 推演(截至
    本次实现,上游报告尚不产出该字段,回退块面板据此只显示原因码)。

    报告缺失(None)/结构不是 dict → 返回 None,调用方/前端据此显式渲染"无审计
    数据",不猜测、不抛异常。报告是 dict 但字段残缺(损坏/旧版)时,残缺字段
    各自降级为 None/空列表,同样不抛。
    """
    if not isinstance(audit_page, dict):
        return None

    prose_audit = audit_page.get("prose_audit")
    block_metrics = prose_audit.get("block_metrics") if isinstance(prose_audit, dict) else None
    block_metrics = block_metrics if isinstance(block_metrics, dict) else {}

    blocks_out: list[dict] = []
    raw_blocks = audit_page.get("blocks")
    if isinstance(raw_blocks, list):
        for b in raw_blocks:
            if not isinstance(b, dict):
                continue
            entry = _block_metrics_entry(block_metrics, b.get("block_id"))
            missing_samples, missing_trunc = _limited_samples(entry, "missing_samples", samples_limit)
            added_samples, added_trunc = _limited_samples(entry, "added_samples", samples_limit)
            blocks_out.append({
                "block_id": b.get("block_id"),
                "label": b.get("label"),
                "content_source": b.get("content_source"),
                "reasons": b.get("reasons") or [],
                "block_ned": b.get("block_ned"),
                "adopted_text": b.get("adopted_text"),
                "missing_samples": missing_samples,
                "missing_samples_truncated": missing_trunc,
                "added_samples": added_samples,
                "added_samples_truncated": added_trunc,
            })

    raw_issues = audit_page.get("issues")
    raw_table_audit = audit_page.get("table_audit")

    return {
        "status": audit_page.get("status"),
        "issues": raw_issues if isinstance(raw_issues, list) else [],
        "blocks": blocks_out,
        "table_audit": raw_table_audit if isinstance(raw_table_audit, list) else [],
    }


def _valid_bbox(b: dict):
    bbox = b.get("block_bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return list(bbox)
    return None


def _correction_preview(b: dict, corrections_by_id: dict) -> dict | None:
    """待审提案预览(供 debug 视图卡片展示):(page,block_id) 命中且 fingerprint 与当前
    block_content 一致才返回,否则 None——不展示可能贴错块的陈旧提案。不管 status 是
    pending/accepted/rejected 都返回(前端按 status 分别渲染:待审卡片/已采纳标记/已驳回标记)。"""
    c = corrections_by_id.get(b.get("block_id"))
    if not c:
        return None
    if content_fingerprint(b.get("block_content") or "") != c.get("content_fingerprint"):
        return None
    return {
        "block_id": b.get("block_id"),
        "status": c.get("status", "pending"),
        "corrected_latex": c.get("corrected_latex", ""),
        "confidence": c.get("confidence", ""),
        "kind": c.get("kind", ""),
        "engine_latex": c.get("engine_latex", ""),
        "crop_b64": c.get("crop_b64", ""),
        "provider": c.get("provider", ""),
        "model": c.get("model", ""),
        "effort": c.get("effort", ""),
        "attempt": c.get("attempt", 0),
        "verdict": c.get("verdict", ""),
        "cross_checked_by": c.get("cross_checked_by"),
        "note": c.get("note", ""),
    }


def _candidate_preview(b: dict, candidates_by_id: dict) -> dict | None:
    c = candidates_by_id.get(b.get("block_id"))
    if not c:
        return None
    return {
        "candidate_id": c.get("candidate_id", ""),
        "block_id": c.get("block_id"),
        "reasons": c.get("reasons", []),
        "estimate_basis": c.get("estimate_basis", ""),
    }


def _overlay(b: dict, corrections_by_id: dict, provenance_by_id: dict) -> dict | None:
    bbox = _valid_bbox(b)
    if bbox is None:
        return None
    label = b.get("block_label", "")
    return {
        "block_id": b.get("block_id"),
        "label": label,
        "bbox": bbox,
        "order": b.get("block_order"),
        "is_visual": is_visual_block(label),
        "is_noise": label in _NOISE_LABELS,
        "color": label_color(label),
        "content_head": (b.get("block_content") or "")[:120],
        "correction": _correction_preview(b, corrections_by_id),
        "provenance": provenance_by_id.get(b.get("block_id")),
    }


def build_page_signals(blocks: list[dict], warnings: list[dict]) -> dict:
    """该页 selfcheck 信号:双栏嫌疑 + reconstruct 逐页告警(未知 label / visual 异常)。"""
    unhandled = sorted({w["label"] for w in warnings if w["kind"] == "unhandled_label"})
    visual = [w for w in warnings if w["kind"] != "unhandled_label"]
    return {
        "column_suspected": detect_column_layout(blocks),
        "unhandled_labels": unhandled,
        "visual_warnings": visual,
    }


def build_page_payload(res: dict, page: int, stem: str,
                       image_b64: str | None = None,
                       page_errors: list[dict] | None = None,
                       corrections: list[dict] | None = None,
                       candidates: list[dict] | None = None,
                       audit: dict | None = None,
                       samples_limit: int = DEFAULT_AUDIT_SAMPLES_LIMIT) -> dict:
    """把一页 res.json 加工成 HTML 模板所需的 payload dict。frags 是带块归属的
    md 片段列表(供左右双向联动);md 是其 join(供报错索引/整页渲染)。corrections
    是该文档全部修正记录(任意 status),按 (page, block_id) 匹配后挂到对应块/片段的
    "correction" 字段,供 debug 视图渲染待审卡片/一键采纳驳回——不在这里过滤 status
    (那是 apply_corrections 的应用侧红线),这里只负责"展示有什么提案"。

    audit 是该页的 source_audit 报告 page_report(schema v2,可选;None=该文档
    未跑 source audit 或独立重跑无报告可读)——只展示,不据此改写 blocks/md 任何
    一个字符,也不现场重新推演采信判定。"""
    blocks = res.get("parsing_res_list", [])
    corrections_by_id = {c["block_id"]: c for c in (corrections or []) if c.get("page") == page}
    candidates_by_id = {c["block_id"]: c for c in (candidates or []) if c.get("page") == page}
    audit_payload = build_audit_payload(audit, samples_limit=samples_limit)
    provenance_by_id = {b["block_id"]: b for b in (audit_payload["blocks"] if audit_payload else [])}
    frags, warnings = reconstruct_fragments(blocks, stem=stem, page=page)
    md = "\n\n".join(f["md"] for f in frags) + "\n"
    overlays = []
    for b in blocks:
        o = _overlay(b, corrections_by_id, provenance_by_id)
        if o is None:
            continue
        o["candidate"] = _candidate_preview(b, candidates_by_id)
        overlays.append(o)
    blocks_by_id = {b.get("block_id"): b for b in blocks}
    for f in frags:
        for bid in f["bids"]:
            corr = _correction_preview(blocks_by_id.get(bid, {}), corrections_by_id)
            if corr:
                f["correction"] = corr
                break
        else:
            f["correction"] = None
        for bid in f["bids"]:
            cand = _candidate_preview(blocks_by_id.get(bid, {}), candidates_by_id)
            if cand:
                f["candidate"] = cand
                break
        else:
            f["candidate"] = None
        for bid in f["bids"]:
            prov = provenance_by_id.get(bid)
            if prov:
                f["provenance"] = prov
                break
        else:
            f["provenance"] = None
    # 疑似识别错误(裸大算符 / \frac 围道当分母):逐片段标注,供 debug 视图橙色标出并聚合到页级
    suspicions: list[dict] = []
    for f in frags:
        slist = scan_formula_suspicions(f["md"])
        f["suspicions"] = [s["op"] for s in slist]
        for s in slist:
            suspicions.append({"op": s["op"], "kind": s["kind"],
                               "detail": s["detail"], "bids": f["bids"]})
    return {
        "page": page,
        "width": res.get("width"),
        "height": res.get("height"),
        "image_b64": image_b64,
        "blocks": overlays,
        "md": md,
        "frags": frags,
        "signals": build_page_signals(blocks, warnings),
        "render_errors": page_errors or [],
        "suspicions": suspicions,
        "candidates": [c for c in (candidates or []) if c.get("page") == page],
        "audit": audit_payload,
    }
