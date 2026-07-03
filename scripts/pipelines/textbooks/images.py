"""按 block_bbox 从整页 PNG 裁出 image/chart 类图片块,存入 <stem>.assets/。"""
from __future__ import annotations

import os

from PIL import Image

_VISUAL_LABELS = {"image", "chart"}


def is_visual_block(label: str) -> bool:
    return label in _VISUAL_LABELS


def crop_filename(page: int, block_id) -> str:
    return f"page_{page:04d}_block_{block_id}.png"


def crop_block_images(png_path: str, blocks: list[dict], assets_dir: str, page: int) -> list[dict]:
    """裁 image/chart 类块存盘。返回告警列表(缺 bbox / 裁图异常),裁图失败不抛出。"""
    visual_blocks = [b for b in blocks if is_visual_block(b.get("block_label", ""))]
    if not visual_blocks:
        return []
    warnings: list[dict] = []
    img = None
    for b in visual_blocks:
        bbox = b.get("block_bbox")
        label = b.get("block_label", "")
        block_id = b.get("block_id")
        sample = (b.get("block_content") or "")[:40]
        if not bbox:
            warnings.append({"kind": "visual_missing_bbox", "label": label, "page": page,
                              "block_id": block_id, "sample": sample})
            continue
        try:
            if img is None:
                img = Image.open(png_path)
            os.makedirs(assets_dir, exist_ok=True)
            crop = img.crop(tuple(bbox))
            crop.save(os.path.join(assets_dir, crop_filename(page, block_id)))
        except Exception as e:                                   # noqa: BLE001 裁图失败不掀翻整页
            warnings.append({"kind": "visual_crop_error", "label": label, "page": page,
                              "block_id": block_id, "sample": f"{type(e).__name__}: {e}"[:40]})
    return warnings
