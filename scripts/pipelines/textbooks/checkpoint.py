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
        "attempts_by_page": {},
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


def page_stem(page: int) -> str:
    return f"page_{page:04d}"


def page_res_path(work_dir: str, page: int) -> str:
    return os.path.normpath(os.path.join(work_dir, f"{page_stem(page)}_res.json"))


def is_page_done(work_dir: str, page: int) -> bool:
    p = page_res_path(work_dir, page)
    if not os.path.exists(p):
        return False
    try:
        with open(p, encoding="utf-8") as f:
            json.load(f)
        return True
    except (ValueError, OSError):
        return False


def write_empty_page(work_dir: str, page: int) -> None:
    os.makedirs(work_dir, exist_ok=True)
    with open(page_res_path(work_dir, page), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": []}, f, ensure_ascii=False)


def load_page_result(work_dir: str, page: int) -> dict:
    p = page_res_path(work_dir, page)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_page_blocks(work_dir: str, page: int) -> list[dict]:
    return load_page_result(work_dir, page).get("parsing_res_list", [])


def pages_todo(work_dir: str, total: int) -> list[int]:
    return [i for i in range(1, total + 1) if not is_page_done(work_dir, i)]


def record_failure(manifest: dict, page: int, error: str, kind: str) -> None:
    manifest["failed_pages"].append({"page": page, "error": error, "kind": kind})


def set_in_progress(manifest: dict, page: int) -> None:
    manifest["in_progress"] = page


def clear_in_progress(manifest: dict) -> None:
    manifest["in_progress"] = None


def resolve_poison(manifest: dict, work_dir: str,
                   max_hard_attempts: int = MAX_HARD_ATTEMPTS) -> None:
    page = manifest.get("in_progress")
    if page is None:
        return
    if is_page_done(work_dir, page):        # 崩在写完 res.json 之后 → 其实已完成
        manifest["in_progress"] = None
        return
    # 进程崩在该页且未完成 → 记一次硬崩(计数与页处理顺序解耦)
    attempts = manifest.setdefault("attempts_by_page", {})
    n = attempts.get(str(page), 0) + 1
    attempts[str(page)] = n
    if n >= max_hard_attempts:              # 反复硬崩进程 → 判毒页,跳过
        record_failure(manifest, page, "process killed repeatedly", "process-killed")
    manifest["in_progress"] = None          # 清除;未达阈值则主循环会重试该页
