"""路线 B golden fixture builder:对指定 PDF 的指定页采样 页图+OCR res+source words。

产物落私有 fixture 目录(不入 git),原始 PDF 只读。两种模式:
  - GPU 采样:--pdf + --pages,逐页栅格化 → PaddleOCR-VL → 落 ocr_res.json;
  - 零 GPU 复用:--reuse-res 指向既有 page_NNNN_res.json,只复制(表格结构回归用)。

用法:
  python -X utf8 -m scripts.pipelines.textbooks.route_b_fixture \
      --pdf <PDF> --pages 55,194 --label steensma --out-dir <FIXTURE_ROOT> [--dpi 150]
  python -X utf8 -m scripts.pipelines.textbooks.route_b_fixture \
      --reuse-res <page_res.json> --label table_pozar --page 112 --out-dir <FIXTURE_ROOT>
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sys
import time


def _json_default(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    return str(o)

_ASCII_LABEL = re.compile(r"^[a-z0-9_]+$")


def fixture_dir_name(label: str, page: int) -> str:
    """fixture 子目录名:ASCII 小写 label + 页号。"""
    if not _ASCII_LABEL.match(label):
        raise ValueError(f"label 必须是 ASCII 小写/数字/下划线: {label!r}")
    return f"{label}_p{page}"


def pdf_fingerprint(pdf_path: str) -> dict:
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"size_bytes": os.path.getsize(pdf_path), "sha256": h.hexdigest()}


def load_manifest(root: str) -> dict:
    p = os.path.join(root, "manifest.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"schema_version": 1, "fixtures": {}}


def save_manifest(root: str, manifest: dict) -> None:
    p = os.path.join(root, "manifest.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def manifest_entry(*, label: str, page: int, source: str, dpi: int | None,
                   fingerprint: dict | None, title: str | None,
                   mode: str, runtime_s: float | None,
                   paddle_version: str | None) -> dict:
    """一个 fixture 的 manifest 记录(纯函数,便于测试)。"""
    return {
        "dir": fixture_dir_name(label, page),
        "page": page,
        "source": source,
        "title": title,
        "mode": mode,                      # "gpu_sample" | "reuse_res"
        "dpi": dpi,
        "pdf_fingerprint": fingerprint,
        "paddle_pipeline_version": "v1.6" if mode == "gpu_sample" else None,
        "paddle_version": paddle_version,
        "runtime_s": round(runtime_s, 1) if runtime_s is not None else None,
        "expected_frozen": False,          # 所有者审核标注后置 True
    }


def _expected_skeleton(label: str, page: int) -> dict:
    return {
        "status": "[待标注]",
        "expected_provenance": {},         # block_id -> source_text|ocr + 原因码
        "expected_issue_codes": [],
        "notes": "",
        "label": label,
        "page": page,
    }


def build_gpu_fixture(pdf_path: str, page: int, label: str, root: str,
                      dpi: int, title: str | None) -> dict:
    import fitz
    from scripts.pipelines.textbooks import engine
    from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
    from scripts.pipelines.textbooks.source_audit import extract_source_page

    d = os.path.join(root, fixture_dir_name(label, page))
    os.makedirs(d, exist_ok=True)
    t0 = time.time()
    png = pdf_page_to_png(pdf_path, page, d, dpi=dpi)
    final_png = os.path.join(d, "page.png")
    if os.path.abspath(png) != os.path.abspath(final_png):
        shutil.move(png, final_png)
    engine.predict_page(final_png, d)
    res_src = os.path.join(d, "page_res.json")
    if not os.path.exists(res_src):
        raise RuntimeError(f"引擎未产出 res JSON: {res_src}")
    os.replace(res_src, os.path.join(d, "ocr_res.json"))
    doc = fitz.open(pdf_path)
    try:
        sp = extract_source_page(doc[page - 1])
    finally:
        doc.close()
    with open(os.path.join(d, "source_words.json"), "w", encoding="utf-8") as f:
        json.dump(sp, f, ensure_ascii=False, default=_json_default)
    exp = os.path.join(d, "expected_audit.json")
    if not os.path.exists(exp):
        with open(exp, "w", encoding="utf-8") as f:
            json.dump(_expected_skeleton(label, page), f, ensure_ascii=False, indent=1)
    try:
        import paddleocr
        pver = getattr(paddleocr, "__version__", None)
    except Exception:
        pver = None
    return manifest_entry(label=label, page=page, source=os.path.basename(pdf_path),
                          dpi=dpi, fingerprint=pdf_fingerprint(pdf_path), title=title,
                          mode="gpu_sample", runtime_s=time.time() - t0,
                          paddle_version=pver)


def build_reuse_fixture(res_path: str, page: int, label: str, root: str,
                        title: str | None) -> dict:
    d = os.path.join(root, fixture_dir_name(label, page))
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(res_path, os.path.join(d, "ocr_res.json"))
    exp = os.path.join(d, "expected_audit.json")
    if not os.path.exists(exp):
        with open(exp, "w", encoding="utf-8") as f:
            json.dump(_expected_skeleton(label, page), f, ensure_ascii=False, indent=1)
    return manifest_entry(label=label, page=page, source=os.path.basename(res_path),
                          dpi=None, fingerprint=None, title=title,
                          mode="reuse_res", runtime_s=None, paddle_version=None)


def main() -> int:
    ap = argparse.ArgumentParser(description="路线 B golden fixture builder(原始 PDF 只读)")
    ap.add_argument("--pdf", help="源 PDF 路径(GPU 采样模式)")
    ap.add_argument("--pages", help="逗号分隔页号(GPU 采样模式)")
    ap.add_argument("--reuse-res", help="既有 page_NNNN_res.json 路径(零 GPU 复用模式)")
    ap.add_argument("--page", type=int, help="复用模式的页号")
    ap.add_argument("--label", required=True, help="fixture 标签(ASCII 小写)")
    ap.add_argument("--out-dir", required=True, help="fixture 根目录")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--title", help="人类可读标题(可中文,只进 manifest)")
    args = ap.parse_args()

    manifest = load_manifest(args.out_dir)
    os.makedirs(args.out_dir, exist_ok=True)
    if args.reuse_res:
        if args.page is None:
            ap.error("--reuse-res 需要 --page")
        entry = build_reuse_fixture(args.reuse_res, args.page, args.label,
                                    args.out_dir, args.title)
        manifest["fixtures"][entry["dir"]] = entry
        print(f"[fixture] reuse {entry['dir']} <- {args.reuse_res}")
    else:
        if not args.pdf or not args.pages:
            ap.error("GPU 采样模式需要 --pdf 与 --pages")
        for page in [int(p) for p in args.pages.split(",")]:
            entry = build_gpu_fixture(args.pdf, page, args.label, args.out_dir,
                                      args.dpi, args.title)
            manifest["fixtures"][entry["dir"]] = entry
            print(f"[fixture] gpu {entry['dir']} runtime={entry['runtime_s']}s")
    save_manifest(args.out_dir, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
