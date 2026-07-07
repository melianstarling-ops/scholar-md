"""KaTeX 硬报错扫描的 Python 薄壳:外调 debug_assets/scan_katex_errors.mjs 产
<stem>_render_errors.json。node 不在 PATH → 返回 None(优雅跳过,调用方打警告不失败)。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.paths import DocLayout
from scripts.pipelines.textbooks.reconstruct import reconstruct_fragments

_MJS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "debug_assets", "scan_katex_errors.mjs")


def scan_katex(md_path: str, out_path: str, node_bin: str | None = None,
               timeout: int = 120) -> dict | None:
    node = node_bin or shutil.which("node")
    if not node:
        return None
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_out = tempfile.mkstemp(prefix=".katex_scan_", suffix=".json", dir=out_dir)
    os.close(fd)
    try:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        argv = [node, _MJS, "--md", md_path, "--out", tmp_out]
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        if proc.returncode != 0 or not os.path.exists(tmp_out):
            return None
        os.replace(tmp_out, out_path)
        with open(out_path, encoding="utf-8") as f:
            return json.load(f)
    finally:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)


def _work_pages_scan_md(layout: DocLayout) -> str:
    manifest = cp.load_manifest(layout.work_dir)
    if manifest is None:
        raise ValueError(f"{layout.work_dir!r} 缺 manifest.json,无法逐页扫描 KaTeX")
    total = manifest["fingerprint"]["page_count"]
    pages: list[str] = []
    for page in range(1, total + 1):
        blocks = cp.load_page_blocks(layout.work_dir, page)
        fragments, _ = reconstruct_fragments(blocks, stem=layout.stem, page=page)
        for frag in fragments:
            bids = ",".join(str(b) for b in frag.get("bids", []) if b is not None)
            pages.append(f"<!-- page: {page} block_ids: {bids} -->\n{frag['md']}")
    return "\n\n".join(pages) + "\n"


def scan_katex_work_pages(layout: DocLayout, out_path: str, node_bin: str | None = None,
                          timeout: int = 120) -> dict | None:
    """逐页从 _work/page_NNNN_res.json 重建扫描输入,让 render_errors 保留页/块归属。"""
    fd, tmp_path = tempfile.mkstemp(prefix=f"{layout.stem}_katex_pages_", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_work_pages_scan_md(layout))
        return scan_katex(tmp_path, out_path, node_bin=node_bin, timeout=timeout)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="KaTeX 硬报错扫描(薄壳调 .mjs)")
    ap.add_argument("--md", default=None, help="输入 md 路径")
    ap.add_argument("--out", required=True, help="输出 render_errors.json 路径")
    ap.add_argument("--work-dir", default=None, help="过程根(用于逐页扫描)")
    ap.add_argument("--deliverables-root", default=None, help="交付根(用于逐页扫描)")
    ap.add_argument("--stem", default=None, help="文档 stem(用于逐页扫描)")
    args = ap.parse_args()
    if args.work_dir and args.deliverables_root and args.stem:
        from scripts.pipelines.textbooks.paths import resolve_layout
        layout = resolve_layout(args.stem, args.deliverables_root, args.work_dir)
        result = scan_katex_work_pages(layout, args.out)
    elif args.md:
        result = scan_katex(args.md, args.out)
    else:
        ap.error("须提供 --md,或同时提供 --work-dir/--deliverables-root/--stem")
    if result is None:
        print("[katex_scan] node 缺失或未产出,已跳过")
    else:
        print(f"[katex_scan] {len(result.get('errors', []))} 处硬报错 → {args.out}")


if __name__ == "__main__":
    main()
