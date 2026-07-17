import json
from pathlib import Path

from scripts.pipelines.textbooks.debug_payload import (
    build_page_payload,
    LABEL_COLORS,
)

FIX = Path(__file__).parent / "fixtures"


def _p31():
    return json.loads((FIX / "page_0031_res.json").read_text(encoding="utf-8"))


def test_payload_carries_page_and_dims():
    res = _p31()
    p = build_page_payload(res, page=31, stem="Paul_p1-100_scan")
    assert p["page"] == 31
    assert p["width"] == res["width"] and p["height"] == res["height"]


def test_payload_blocks_have_overlay_fields():
    p = build_page_payload(_p31(), page=31, stem="Paul_p1-100_scan")
    b18 = next(b for b in p["blocks"] if b["block_id"] == 18)
    assert b18["label"] == "display_formula"
    assert b18["bbox"] == [248, 1782, 897, 1940]
    assert b18["order"] == 15
    assert b18["color"] == LABEL_COLORS["display_formula"]
    assert b18["is_noise"] is False


def test_payload_noise_blocks_flagged():
    # header/number 类 order=None 噪声块应标 is_noise=True(视图里弱化显示)
    p = build_page_payload(_p31(), page=31, stem="Paul_p1-100_scan")
    noise = [b for b in p["blocks"] if b["is_noise"]]
    assert all(b["order"] is None for b in noise)


def test_payload_md_is_reconstructed_and_fixed():
    # 右栏 md 走 reconstruct(过修复后的 sanitize);1.3a 双下标已消除
    p = build_page_payload(_p31(), page=31, stem="Paul_p1-100_scan")
    assert r"}_{in\text{the}}_{\substack" not in p["md"]
    assert "\\tag{1.3a}" in p["md"]


def test_payload_frags_carry_bids_and_join_to_md():
    p = build_page_payload(_p31(), page=31, stem="Paul_p1-100_scan")
    assert isinstance(p["frags"], list) and p["frags"]
    assert all("bids" in f and "md" in f for f in p["frags"])
    assert "\n\n".join(f["md"] for f in p["frags"]) + "\n" == p["md"]
    # 1.3a 公式块(block_id=18)应出现在某片段的 bids 里
    assert any(18 in f["bids"] for f in p["frags"])


def test_payload_flags_bare_oint_suspicion_golden_p48():
    # 真实 p48:1.55 公式的裸 \oint(用户实测标的漏下标) → payload.suspicions 命中
    res = json.loads((FIX / "page_0048_res.json").read_text(encoding="utf-8"))
    p = build_page_payload(res, page=48, stem="Paul_p1-100_scan")
    ops = [s["op"] for s in p["suspicions"]]
    assert r"\oint" in ops
    assert all("op" in s and "bids" in s for s in p["suspicions"])


def test_payload_frag_suspicions_attached():
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "display_formula", "block_content": r"$$ \oint \vec E \cdot ds $$",
         "block_order": 1, "block_bbox": [0, 0, 10, 10], "block_id": 1},
    ]}
    p = build_page_payload(res, page=1, stem="s")
    assert p["frags"][0]["suspicions"] == [r"\oint"]
    assert len(p["suspicions"]) == 1
    s = p["suspicions"][0]
    assert s["op"] == r"\oint" and s["kind"] == "bare_op" and s["bids"] == [1] and "detail" in s


def test_payload_signals_present():
    p = build_page_payload(_p31(), page=31, stem="Paul_p1-100_scan")
    assert "column_suspected" in p["signals"]
    assert "unhandled_labels" in p["signals"]
    assert "visual_warnings" in p["signals"]


def test_payload_image_and_errors_passed_through():
    p = build_page_payload(_p31(), page=31, stem="Paul_p1-100_scan",
                           image_b64="ABC123",
                           page_errors=[{"mode": "display", "error": "boom"}])
    assert p["image_b64"] == "ABC123"
    assert p["render_errors"][0]["error"] == "boom"


def test_payload_missing_image_is_none():
    p = build_page_payload(_p31(), page=31, stem="Paul_p1-100_scan")
    assert p["image_b64"] is None


def test_malformed_bbox_block_excluded_from_overlays():
    # 畸形/缺失 bbox 的块不叠框(与 reconstruct 同降级策略),但不崩
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "text", "block_content": "x", "block_order": 1, "block_bbox": [5], "block_id": 1},
        {"block_label": "text", "block_content": "y", "block_order": 2, "block_bbox": [0, 0, 10, 10], "block_id": 2},
    ]}
    p = build_page_payload(res, page=1, stem="s")
    ids = [b["block_id"] for b in p["blocks"]]
    assert ids == [2]        # 只有合法 bbox 的入叠框


def test_payload_attaches_pending_correction_to_block_and_frag():
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint
    original = r"$$ \oint \vec E \cdot ds $$"
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "display_formula", "block_content": original,
         "block_order": 1, "block_bbox": [0, 0, 10, 10], "block_id": 1},
    ]}
    corrections = [{"page": 1, "block_id": 1, "corrected_latex": "$$ fixed $$",
                    "confidence": "high", "kind": "bare_op",
                    "content_fingerprint": content_fingerprint(original), "status": "pending"}]
    p = build_page_payload(res, page=1, stem="s", corrections=corrections)
    b = p["blocks"][0]
    assert b["correction"]["status"] == "pending"
    assert b["correction"]["corrected_latex"] == "$$ fixed $$"
    assert b["correction"]["block_id"] == 1
    assert b["correction"]["confidence"] == "high"
    frag = p["frags"][0]
    assert frag["correction"]["status"] == "pending"


def test_payload_correction_not_attached_on_fingerprint_mismatch():
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "display_formula", "block_content": r"$$ \oint \vec E \cdot ds $$",
         "block_order": 1, "block_bbox": [0, 0, 10, 10], "block_id": 1},
    ]}
    corrections = [{"page": 1, "block_id": 1, "corrected_latex": "$$ fixed $$",
                    "content_fingerprint": "stale-hash", "status": "pending"}]
    p = build_page_payload(res, page=1, stem="s", corrections=corrections)
    assert p["blocks"][0]["correction"] is None


def test_payload_correction_none_when_not_provided():
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "display_formula", "block_content": "$$ a $$",
         "block_order": 1, "block_bbox": [0, 0, 10, 10], "block_id": 1},
    ]}
    p = build_page_payload(res, page=1, stem="s")
    assert p["blocks"][0]["correction"] is None


def test_payload_carries_audit_status_and_block_provenance():
    # Task 12:source_audit 报告(schema v2,synthetic 注入)接进 payload——页级
    # status/issues + 块级 provenance(content_source/reasons/block_ned)。
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "text", "block_content": "hello", "block_order": 1,
         "block_bbox": [0, 0, 10, 10], "block_id": 1},
    ]}
    audit_page = {
        "page": 1, "status": "SUSPECT",
        "issues": [{"code": "missing_prose", "block_id": 1, "detail": "字符召回=0.50"}],
        "blocks": [{"block_id": 1, "label": "text", "content_source": "ocr",
                    "reasons": ["adoption_disagreement"], "block_ned": 0.25}],
        "prose_audit": {"status": "SUSPECT", "issues": [], "metrics": {}, "block_metrics": {}},
        "table_audit": [],
    }
    p = build_page_payload(res, page=1, stem="s", audit=audit_page)
    assert p["audit"]["status"] == "SUSPECT"
    assert p["audit"]["issues"][0]["code"] == "missing_prose"
    b = p["blocks"][0]
    assert b["provenance"]["content_source"] == "ocr"
    assert b["provenance"]["reasons"] == ["adoption_disagreement"]
    assert b["provenance"]["block_ned"] == 0.25


def test_payload_audit_absent_when_report_missing_or_malformed():
    # 报告缺失(未传 audit)/损坏(结构不含 status/blocks)都不得抛异常——字段
    # 显式为 None/空,前端据此渲染"无审计数据",不猜测。
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "text", "block_content": "hello", "block_order": 1,
         "block_bbox": [0, 0, 10, 10], "block_id": 1},
    ]}
    p = build_page_payload(res, page=1, stem="s")
    assert p["audit"] is None
    assert p["blocks"][0]["provenance"] is None

    p2 = build_page_payload(res, page=1, stem="s", audit={"not": "a valid page report"})
    assert p2["audit"]["status"] is None
    assert p2["audit"]["issues"] == []
    assert p2["blocks"][0]["provenance"] is None


def test_payload_prose_audit_samples_truncated_to_limit():
    # prose_audit.samples 有限展示,不嵌整页源文本——上限可注入,超限即截断并标记。
    res = {"width": 100, "height": 100, "parsing_res_list": []}
    samples = [{"kind": "missing", "text": f"s{i}"} for i in range(50)]
    audit_page = {"page": 1, "status": "SUSPECT", "issues": [], "blocks": [],
                  "prose_audit": {"status": "SUSPECT", "samples": samples}}

    p = build_page_payload(res, page=1, stem="s", audit=audit_page, samples_limit=5)
    assert p["audit"]["samples"] == samples[:5]
    assert p["audit"]["samples_truncated"] is True

    p2 = build_page_payload(res, page=1, stem="s", audit=audit_page, samples_limit=100)
    assert len(p2["audit"]["samples"]) == 50
    assert p2["audit"]["samples_truncated"] is False


def test_payload_attaches_formula_candidate_to_block_and_frag():
    res = {"width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "display_formula", "block_content": "$$ a $$",
         "block_order": 1, "block_bbox": [0, 0, 10, 10], "block_id": 1},
    ]}
    candidates = [{"page": 1, "block_id": 1, "reasons": ["katex_warning:unicodeTextInMathMode"],
                   "candidate_id": "p0001-b0001", "estimate_basis": "bbox_proxy"}]

    p = build_page_payload(res, page=1, stem="s", candidates=candidates)

    assert p["candidates"] == candidates
    assert p["blocks"][0]["candidate"]["reasons"] == ["katex_warning:unicodeTextInMathMode"]
    assert p["frags"][0]["candidate"]["candidate_id"] == "p0001-b0001"
