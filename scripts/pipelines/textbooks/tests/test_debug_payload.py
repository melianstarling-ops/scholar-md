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
