"""PaddleOCR-VL 1.6 封装:PNG → parsing_res_list。惰性单例,避免每页重载模型。"""
from __future__ import annotations

import json
import os

_PIPELINE = None


def _get_pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        from paddleocr import PaddleOCRVL
        _PIPELINE = PaddleOCRVL(pipeline_version="v1.6")
    return _PIPELINE


def predict_page(png_path: str, work_dir: str) -> list[dict]:
    """跑单页,落 <stem>_res.json,读回其 parsing_res_list。"""
    os.makedirs(work_dir, exist_ok=True)
    pipe = _get_pipeline()
    results = list(pipe.predict(png_path))
    if not results:
        return []
    results[0].save_to_json(save_path=work_dir)
    stem = os.path.splitext(os.path.basename(png_path))[0]
    jpath = os.path.join(work_dir, f"{stem}_res.json")
    with open(jpath, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("parsing_res_list", [])
