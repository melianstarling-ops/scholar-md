"""把浏览器导出的标注 JSON 转成回归断言/报告(spec §6 五类)。

cat1 渲染报错 / cat3 漏内容 / cat4 错归类 有确定性判据,自动断言;
cat2 排版错 / cat5 图片位置 只记录为人工核对项(ok=None)。

  python -m scripts.pipelines.textbooks.check_annotations --doc <stem-dir>
      读 <stem>_annotations.json + _work/*_res.json + <stem>_render_errors.json,
      逐条判定,打印报告;有回归(cat1/3/4 失败)时退出码 1。
"""
from __future__ import annotations

import argparse
import json
import os

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown

CAT = {1: "渲染报错", 2: "公式排版错", 3: "漏内容", 4: "错误归类", 5: "图片位置"}


def _center(bbox: list) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def _block_under(bbox: list, blocks: list[dict]) -> dict | None:
    """标注框中心落在哪个块的 block_bbox 内(取第一个命中)。"""
    cx, cy = _center(bbox)
    for b in blocks:
        bb = b.get("block_bbox")
        if isinstance(bb, (list, tuple)) and len(bb) == 4:
            if bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3]:
                return b
    return None


def check_one(a: dict, page_md: str, blocks: list[dict], error_pages: set) -> dict:
    cat = a.get("category")
    page = a.get("page")
    base = {"category": cat, "page": page, "note": a.get("note", "")}
    if cat == 1:
        ok = page not in error_pages
        return {**base, "ok": ok, "detail": "该页无 KaTeX 报错" if ok else "该页仍有 KaTeX 硬报错"}
    if cat == 3:
        note = (a.get("note") or "").strip()
        ok = bool(note) and note in page_md
        return {**base, "ok": ok,
                "detail": "漏失内容已出现" if ok else f"仍未在 md 找到:{note!r}"}
    if cat == 4:
        b = _block_under(a.get("bbox", []), blocks)
        want = (a.get("note") or "").strip()
        cur = b.get("block_label") if b else None
        ok = bool(want) and cur == want
        return {**base, "ok": ok, "detail": f"当前 label={cur!r} 期望={want!r}"}
    return {**base, "ok": None, "detail": "人工核对项(不自动断言)"}


def check_doc(doc_dir: str) -> list[dict]:
    stem = os.path.basename(os.path.normpath(doc_dir))
    ann_path = os.path.join(doc_dir, stem + "_annotations.json")
    if not os.path.exists(ann_path):
        return []
    annotations = json.load(open(ann_path, encoding="utf-8")).get("annotations", [])

    err_path = os.path.join(doc_dir, stem + "_render_errors.json")
    error_pages = set()
    if os.path.exists(err_path):
        error_pages = {e.get("page") for e in json.load(open(err_path, encoding="utf-8")).get("errors", [])}

    work = os.path.join(doc_dir, "_work")
    md_cache: dict[int, str] = {}
    blocks_cache: dict[int, list] = {}

    def page_data(page: int):
        if page not in md_cache:
            blocks = cp.load_page_blocks(work, page)
            blocks_cache[page] = blocks
            md_cache[page], _ = reconstruct_markdown(blocks, stem=stem, page=page)
        return md_cache[page], blocks_cache[page]

    results = []
    for a in annotations:
        md, blocks = page_data(a.get("page"))
        results.append(check_one(a, md, blocks, error_pages))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="标注回归断言")
    ap.add_argument("--doc", required=True)
    args = ap.parse_args()
    results = check_doc(os.path.abspath(args.doc))
    if not results:
        print("[check_annotations] 无标注")
        return 0
    fails = 0
    for r in results:
        mark = {True: "✔", False: "✗", None: "·"}[r["ok"]]
        if r["ok"] is False:
            fails += 1
        print(f"  {mark} p{r['page']} [{CAT.get(r['category'], r['category'])}] {r['detail']}")
    print(f"[check_annotations] {len(results)} 条 | 回归失败 {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
