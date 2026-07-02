"""单文档编排:分诊 → (A/C)逐页流式 OCR(可续跑/磁盘有界/坏页隔离) → 重组 md。B 登记不转。"""
from __future__ import annotations

import argparse
import os
import time

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_page_to_png
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown
from scripts.pipelines.textbooks.selfcheck import block_coverage, katex_incompat_scan
from scripts.pipelines.textbooks import checkpoint as cp


def assemble(work_dir: str, total: int) -> tuple[str, list[dict]]:
    """按页序读检查点 → (md, all_blocks)。缺失/失败页贡献空串。"""
    md_pages: list[str] = []
    all_blocks: list[dict] = []
    for i in range(1, total + 1):
        blocks = cp.load_page_blocks(work_dir, i)
        all_blocks.extend(blocks)
        page_md = reconstruct_markdown(blocks)
        if page_md.strip():
            md_pages.append(page_md)
    return "\n\n".join(md_pages) + "\n", all_blocks


def _register_deferred(pdf_path: str, out_dir: str, stem: str) -> dict:
    deferred = os.path.join(out_dir, "_deferred_born_digital")
    os.makedirs(deferred, exist_ok=True)
    with open(os.path.join(deferred, stem + ".txt"), "w", encoding="utf-8") as f:
        f.write(pdf_path + "\n")
    return {"route": "B", "md_path": None, "selfcheck": None, "failed_pages": []}


def convert_pdf(pdf_path: str, out_dir: str | None = None,
                dpi: int = cp.DEFAULT_DPI) -> dict:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(pdf_path))
    route = triage(pdf_path)
    if route == "B":
        return _register_deferred(pdf_path, out_dir, stem)

    doc_out = os.path.join(out_dir, stem)
    work_dir = os.path.join(doc_out, "_work")

    # 指纹校验:源或 DPI 变 → 清空全新跑
    manifest = cp.load_manifest(work_dir)
    if manifest is None or not cp.fingerprint_ok(manifest, pdf_path, dpi):
        if manifest is not None:
            print(f"[textbooks] 指纹失配(源或DPI变),清空 {work_dir} 全新跑")
        cp.reset_work_dir(work_dir)
        manifest = cp.new_manifest(pdf_path, cp.pdf_fingerprint(pdf_path), dpi, route)
        cp.save_manifest(work_dir, manifest)

    total = manifest["fingerprint"]["page_count"]
    todo = cp.pages_todo(work_dir, total)
    done = total - len(todo)
    durations: list[float] = []
    for page in todo:
        t = time.time()
        png = None
        try:
            png = pdf_page_to_png(pdf_path, page, work_dir, dpi=dpi)
            blocks = predict_page(png, work_dir)   # 非空时 engine 已落 res.json
            if not blocks and not cp.is_page_done(work_dir, page):
                cp.write_empty_page(work_dir, page)   # 空白页显式标记完成
        except Exception as e:                        # noqa: BLE001 坏页隔离
            cp.record_failure(manifest, page, f"{type(e).__name__}: {e}",
                              "page-exception")
            cp.save_manifest(work_dir, manifest)
        finally:
            if png and os.path.exists(png):
                os.remove(png)                        # 磁盘有界:predict 后即删
        done += 1
        durations.append(time.time() - t)
        avg = sum(durations) / len(durations)
        eta_h = avg * (total - done) / 3600
        nfail = len(manifest["failed_pages"])
        print(f"[page {page}/{total}] {durations[-1]:.0f}s "
              f"(完成 {done} 失败 {nfail} ETA {eta_h:.1f}h)")

    # 陈旧失败清理:曾失败但续跑后已完成的页,不应再挂在 failed_pages 里
    manifest["failed_pages"] = [f for f in manifest["failed_pages"]
                                if not cp.is_page_done(work_dir, f["page"])]

    # 从检查点重组(每次运行都做,部分完成也产出部分 md)
    md, all_blocks = assemble(work_dir, total)
    os.makedirs(doc_out, exist_ok=True)
    md_path = os.path.join(doc_out, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)
    cp.save_manifest(work_dir, manifest)
    return {"route": route, "md_path": md_path, "selfcheck": check,
            "failed_pages": manifest["failed_pages"]}


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 单文档转换(可续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    args = ap.parse_args()
    res = convert_pdf(args.src, args.out, dpi=args.dpi)
    print(f"[route={res['route']}] md={res['md_path']}")
    if res.get("failed_pages"):
        print(f"[textbooks] 失败页 {len(res['failed_pages'])}:",
              [f["page"] for f in res["failed_pages"]])
    if res["selfcheck"]:
        c = res["selfcheck"]
        print(f"[Tier0] blocks {c['in_md']}/{c['total']} 覆盖, 缺 {len(c['missing'])}")
        if c.get("katex_incompat"):
            print("[Tier0] KaTeX 不兼容残留:", ", ".join(c["katex_incompat"]))


if __name__ == "__main__":
    main()
