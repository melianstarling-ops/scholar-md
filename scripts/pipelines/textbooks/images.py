"""按 block_bbox 从整页 PNG 裁出 image/chart 类图片块,存入 <stem>.assets/。"""
from __future__ import annotations

_VISUAL_LABELS = {"image", "chart"}


def is_visual_block(label: str) -> bool:
    return label in _VISUAL_LABELS


def crop_filename(page: int, block_id) -> str:
    return f"page_{page:04d}_block_{block_id}.png"
