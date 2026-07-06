"""公式视觉修复(后处理)第一步:裁图 + 工作单导出。

对被 selfcheck 标记"疑似漏识别/结构错"的 display_formula 块,按 block_bbox
高 DPI 裁成 PNG + 导出待修工作单(page/block_id/bbox/engine_latex/crop 路径),
供下一步无头 `claude -p` 读图生成修正 LaTeX(见 docs/handoff/2026-07-04-HANDOFF-
textbooks-formula-vision-repair.md §2/§4)。
"""
from __future__ import annotations

import argparse
import json
import os

from PIL import Image

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import images
from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
from scripts.pipelines.textbooks.selfcheck import scan_formula_suspicions


def find_suspicious_blocks(blocks: list[dict]) -> list[dict]:
    """扫一页的 blocks,返回疑似结构错的 display_formula 块定位信息。

    只看 display_formula 块本身的 block_content(不含吸收编号后的 $$ 包裹/\\tag,
    可疑模式不受那层包裹影响),避免依赖 reconstruct 的片段归并逻辑。
    """
    out: list[dict] = []
    for b in blocks:
        if b.get("block_label") != "display_formula":
            continue
        content = b.get("block_content") or ""
        hits = scan_formula_suspicions(content)
        if not hits:
            continue
        out.append({
            "block_id": b.get("block_id"),
            "bbox": b.get("block_bbox"),
            "engine_latex": content,
            "kinds": sorted({h["kind"] for h in hits}),
            "ops": sorted({h["op"] for h in hits}),
        })
    return out


def _norm_latex(s: str) -> str:
    """去 $$ 包裹 + 所有空白,供 render_error latex_head ↔ block_content 前缀匹配。"""
    s = s.strip()
    if s.startswith("$$"):
        s = s[2:]
    if s.endswith("$$"):
        s = s[:-2]
    return "".join(s.split())


def blocks_from_render_errors(blocks: list[dict], page_errors: list[dict]) -> list[dict]:
    """把该页 KaTeX 硬报错(render_errors,确定性、零假阳性)匹配回 display_formula 块,
    转成与 find_suspicious_blocks 同构的 hit(kind="render_error")。

    render_error 无 block_id、其 latex_head 是 reconstruct 后 md 公式前 90 字符(截断),
    故靠 latex_head 与 block_content 去 $$/空白后的前缀互含来定位;匹配不到的报错跳过
    (不误纳),一个块最多命中一次。
    """
    dfs = [b for b in blocks if b.get("block_label") == "display_formula"]
    out: list[dict] = []
    seen: set = set()
    for e in page_errors:
        head = _norm_latex(e.get("latex_head") or "")
        if not head:
            continue
        for b in dfs:
            bid = b.get("block_id")
            if bid in seen:
                continue
            content = _norm_latex(b.get("block_content") or "")
            if content and (content.startswith(head) or head.startswith(content)):
                out.append({
                    "block_id": bid,
                    "bbox": b.get("block_bbox"),
                    "engine_latex": b.get("block_content") or "",
                    "kinds": ["render_error"],
                    "ops": [],
                })
                seen.add(bid)
                break
    return out


def _merge_hits(*groups: list[dict]) -> list[dict]:
    """按 block_id 合并多来源 hit(启发式疑似 + render_error),同块合并 kinds、去重。"""
    by_id: dict = {}
    for g in groups:
        for h in g:
            bid = h["block_id"]
            if bid in by_id:
                by_id[bid] = {**by_id[bid],
                              "kinds": sorted(set(by_id[bid]["kinds"]) | set(h["kinds"]))}
            else:
                by_id[bid] = dict(h)
    return list(by_id.values())


def _render_errors_by_page(layout: DocLayout) -> dict:
    """读 render_errors.json -> {page: [error, ...]};文件不存在返回 {}。"""
    path = layout.render_errors_path
    by_page: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for e in data.get("errors", []):
            by_page.setdefault(e.get("page"), []).append(e)
    return by_page


def crop_at_scale(png_path: str, bbox: list[float], scale: float, pad: int = 10) -> Image.Image:
    """按 scale 把 bbox(res.json 坐标空间)换算到 png_path 实际像素空间,加 pad 后裁剪。

    scale = 高 DPI 渲染图的实际像素宽(或高) / res.json 声明的 width(或 height)
    (两者理论 DPI 比一致,调用方自算);裁剪区域会被 clamp 到图片边界内,不会因
    pad 越界而报错。
    """
    with Image.open(png_path) as img:
        x0, y0, x1, y1 = bbox
        left = max(0, int(x0 * scale) - pad)
        top = max(0, int(y0 * scale) - pad)
        right = min(img.width, int(x1 * scale) + pad)
        bottom = min(img.height, int(y1 * scale) + pad)
        crop = img.crop((left, top, right, bottom))
        crop.load()          # 强制物化像素,脱离源文件句柄(Windows 下延迟加载会锁住源 PNG)
        return crop


def build_repair_worklist(layout: DocLayout, pdf_path: str | None = None,
                          repair_dpi: int = 300, pad: int = 10) -> dict:
    """扫全文档 _work/page_NNNN_res.json,对疑似块高 DPI 裁图 + 落工作单。

    产物落 layout.repair_dir:`crops/` 存裁图(命名同 images.crop_filename),
    `worklist.json` 记 [{page,block_id,bbox,engine_latex,kinds,ops,crop_path}, ...],
    供下一步(无头 claude -p 读图生成修正 LaTeX)消费。只渲染真正含疑似块的页,
    渲染用的整页 PNG 用完即删(磁盘有界,同 convert.py 的惯例)。
    """
    work_dir = layout.work_dir
    manifest = cp.load_manifest(work_dir)
    if manifest is None:
        raise ValueError(f"{layout.work_dir!r} 缺 manifest.json,须先跑 convert_pdf")
    resolved_pdf = pdf_path or manifest.get("pdf_path")
    if not resolved_pdf:
        raise ValueError("未给 pdf_path,manifest 里也没有,无法栅格化裁图")
    total = manifest["fingerprint"]["page_count"]
    stem = layout.stem
    render_errors = _render_errors_by_page(layout)
    repair_dir = layout.repair_dir
    crops_dir = os.path.join(layout.repair_dir, "crops")
    pages_dir = os.path.join(layout.repair_dir, "_pages")

    items: list[dict] = []
    for page in range(1, total + 1):
        res_path = cp.page_res_path(work_dir, page)
        if not os.path.exists(res_path):
            continue
        with open(res_path, encoding="utf-8") as f:
            res = json.load(f)
        blocks = res.get("parsing_res_list", [])
        hits = _merge_hits(find_suspicious_blocks(blocks),
                           blocks_from_render_errors(blocks, render_errors.get(page, [])))
        if not hits:
            continue
        png = pdf_page_to_png(resolved_pdf, page, pages_dir, dpi=repair_dpi)
        try:
            with Image.open(png) as rendered:
                width = rendered.width
            scale = width / res.get("width", width)
            os.makedirs(crops_dir, exist_ok=True)
            for hit in hits:
                crop = crop_at_scale(png, hit["bbox"], scale, pad)
                crop_path = os.path.join(crops_dir, images.crop_filename(page, hit["block_id"]))
                crop.save(crop_path)
                items.append({**hit, "page": page, "crop_path": os.path.abspath(crop_path)})
        finally:
            if os.path.exists(png):
                os.remove(png)

    os.makedirs(repair_dir, exist_ok=True)
    worklist_path = layout.worklist_path
    worklist = {"stem": stem, "count": len(items), "items": items}
    with open(worklist_path, "w", encoding="utf-8") as f:
        json.dump(worklist, f, ensure_ascii=False, indent=2)
    return {"worklist_path": worklist_path, "count": len(items), "items": items}


def main() -> None:
    ap = argparse.ArgumentParser(description="疑似结构错公式:高 DPI 裁图 + 待修工作单导出")
    ap.add_argument("--out", required=True, help="交付根(md+assets)")
    ap.add_argument("--work-dir", default=None, help="过程根(默认 <out>/_work_root)")
    ap.add_argument("--stem", required=True, help="文档 stem")
    ap.add_argument("--src", default=None, help="源 PDF 路径(默认取 manifest 记的 pdf_path)")
    ap.add_argument("--repair-dpi", type=int, default=300, help="裁图栅格化 DPI(默认300)")
    ap.add_argument("--pad", type=int, default=10, help="裁剪边距像素(默认10)")
    args = ap.parse_args()
    layout = resolve_layout(args.stem, args.out, args.work_dir)
    result = build_repair_worklist(layout, pdf_path=args.src,
                                   repair_dpi=args.repair_dpi, pad=args.pad)
    print(f"[debug_repair] {result['count']} 处疑似 → {result['worklist_path']}")


if __name__ == "__main__":
    main()
