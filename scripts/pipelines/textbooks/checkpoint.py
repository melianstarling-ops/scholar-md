"""断点/manifest/指纹/毒页 簿记(纯确定性,无 GPU)。"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

import fitz

MAX_HARD_ATTEMPTS = 2      # 某页硬崩进程超此次数 → 标 process-killed 跳过
MAX_RESTARTS = 50          # 看门狗累计重启兜底
DEFAULT_DPI = 150          # 实测甜区


def pdf_fingerprint(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    try:
        n = doc.page_count
    finally:
        doc.close()
    return {"page_count": n, "size_bytes": os.path.getsize(pdf_path)}


def manifest_path(work_dir: str) -> str:
    return os.path.join(work_dir, "manifest.json")


def load_manifest(work_dir: str) -> dict | None:
    p = manifest_path(work_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def save_manifest(work_dir: str, manifest: dict) -> None:
    os.makedirs(work_dir, exist_ok=True)
    manifest["updated"] = datetime.now().isoformat(timespec="seconds")
    with open(manifest_path(work_dir), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def new_manifest(pdf_path: str, fingerprint: dict, dpi: int, route: str) -> dict:
    return {
        "pdf_path": pdf_path,
        "fingerprint": fingerprint,
        "dpi": dpi,
        "route": route,
        "failed_pages": [],
        "in_progress": None,
        "restarts": 0,
    }


def fingerprint_ok(manifest: dict, pdf_path: str, dpi: int) -> bool:
    if manifest.get("dpi") != dpi:
        return False
    return manifest.get("fingerprint") == pdf_fingerprint(pdf_path)


def reset_work_dir(work_dir: str) -> None:
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir, exist_ok=True)
