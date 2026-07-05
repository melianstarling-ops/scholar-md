import os

from PIL import Image

from scripts.pipelines.textbooks.debug_repair import crop_at_scale, find_suspicious_blocks


def _make_test_png(path, size=(200, 200)):
    img = Image.new("RGB", size, color="white")
    for x in range(50, 150):
        for y in range(50, 150):
            img.putpixel((x, y), (255, 0, 0))   # 红色方块 [50,50,150,150]
    img.save(path)


def test_find_suspicious_blocks_flags_frac_primed_denom():
    blocks = [
        {"block_label": "display_formula", "block_id": 3, "block_order": 2,
         "block_bbox": [10, 20, 100, 60],
         "block_content": r"$$ c\Delta z=\frac{a}{c^{\prime}} $$"},
    ]
    hits = find_suspicious_blocks(blocks)
    assert len(hits) == 1
    assert hits[0]["block_id"] == 3
    assert hits[0]["bbox"] == [10, 20, 100, 60]
    assert hits[0]["kinds"] == ["frac_primed_denom"]


def test_find_suspicious_blocks_ignores_non_formula_labels():
    blocks = [
        {"block_label": "text", "block_id": 1, "block_order": 1,
         "block_bbox": [0, 0, 10, 10],
         "block_content": r"$$ \oint E \cdot dl $$"},
    ]
    assert find_suspicious_blocks(blocks) == []


def test_find_suspicious_blocks_ignores_clean_formula():
    blocks = [
        {"block_label": "display_formula", "block_id": 2, "block_order": 1,
         "block_bbox": [0, 0, 10, 10],
         "block_content": r"$$ E = mc^2 $$"},
    ]
    assert find_suspicious_blocks(blocks) == []


def test_find_suspicious_blocks_flags_bare_op():
    blocks = [
        {"block_label": "display_formula", "block_id": 9, "block_order": 3,
         "block_bbox": [5, 5, 50, 50],
         "block_content": r"$$ c = \varepsilon \oint E \cdot dl $$"},
    ]
    hits = find_suspicious_blocks(blocks)
    assert len(hits) == 1
    assert hits[0]["kinds"] == ["bare_op"]
    assert hits[0]["ops"] == [r"\oint"]


def test_find_suspicious_blocks_only_returns_hit_blocks():
    blocks = [
        {"block_label": "display_formula", "block_id": 1, "block_order": 1,
         "block_bbox": [0, 0, 10, 10], "block_content": r"$$ E = mc^2 $$"},
        {"block_label": "display_formula", "block_id": 2, "block_order": 2,
         "block_bbox": [0, 20, 10, 30],
         "block_content": r"$$ c\Delta z=\frac{a}{c^{\prime}} $$"},
    ]
    hits = find_suspicious_blocks(blocks)
    assert [h["block_id"] for h in hits] == [2]


def test_crop_at_scale_scales_bbox_and_crops(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    # bbox 在 res 坐标空间(scale=2 换算到实际渲染像素空间的 [50,50,150,150])
    crop = crop_at_scale(png, [25, 25, 75, 75], scale=2.0, pad=0)
    assert crop.size == (100, 100)
    assert crop.getpixel((10, 10)) == (255, 0, 0)


def test_crop_at_scale_applies_padding(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    crop = crop_at_scale(png, [25, 25, 75, 75], scale=2.0, pad=10)
    assert crop.size == (120, 120)


def test_crop_at_scale_clamps_to_image_bounds(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)   # 200x200
    # scale=2 → 未裁剪前理论区域到 [0,0]-[400,400],远超图片边界,pad 也不该报错
    crop = crop_at_scale(png, [0, 0, 100, 100], scale=2.0, pad=10)
    assert crop.size == (200, 200)


import json

import fitz

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.debug_repair import build_repair_worklist


def _write_res(work_dir, page, width, height, blocks):
    os.makedirs(work_dir, exist_ok=True)
    with open(cp.page_res_path(work_dir, page), "w", encoding="utf-8") as f:
        json.dump({"width": width, "height": height, "parsing_res_list": blocks}, f)


def test_build_repair_worklist_crops_only_suspicious_blocks(tmp_path):
    doc = fitz.open()
    doc.new_page(width=72, height=72)
    doc.new_page(width=72, height=72)
    pdf = tmp_path / "book.pdf"
    doc.save(str(pdf))
    doc.close()

    doc_dir = tmp_path / "book"
    work_dir = doc_dir / "_work"
    manifest = cp.new_manifest(str(pdf), {"page_count": 2, "size_bytes": os.path.getsize(pdf)},
                               150, "A")
    cp.save_manifest(str(work_dir), manifest)

    _write_res(str(work_dir), 1, 150, 150, [
        {"block_label": "display_formula", "block_id": 3, "block_order": 1,
         "block_bbox": [10, 10, 60, 60],
         "block_content": r"$$ c\Delta z=\frac{a}{c^{\prime}} $$"},
        {"block_label": "display_formula", "block_id": 4, "block_order": 2,
         "block_bbox": [70, 70, 140, 140], "block_content": r"$$ E=mc^2 $$"},
    ])
    _write_res(str(work_dir), 2, 150, 150, [
        {"block_label": "text", "block_id": 1, "block_order": 1,
         "block_bbox": [0, 0, 10, 10], "block_content": "hello"},
    ])

    result = build_repair_worklist(str(doc_dir), repair_dpi=300, pad=5)

    assert result["count"] == 1
    item = result["items"][0]
    assert item["page"] == 1
    assert item["block_id"] == 3
    assert item["kinds"] == ["frac_primed_denom"]
    assert os.path.exists(item["crop_path"])

    crops_dir = os.path.join(str(doc_dir), "book_repair", "crops")
    assert sorted(os.listdir(crops_dir)) == ["page_0001_block_3.png"]

    worklist_path = os.path.join(str(doc_dir), "book_repair", "worklist.json")
    with open(worklist_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["count"] == 1
