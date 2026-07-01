"""单文档编排:分诊 → (A/C)PNG→predict→重组→自检 → Typora md。B 登记不转。"""
from __future__ import annotations

import argparse
import os
import tempfile

from scripts.pipelines.textbooks.triage import triage
from scripts.pipelines.textbooks.preprocess import pdf_to_pngs
from scripts.pipelines.textbooks.engine import predict_page
from scripts.pipelines.textbooks.reconstruct import reconstruct_markdown
from scripts.pipelines.textbooks.selfcheck import block_coverage, katex_incompat_scan


def convert_pdf(pdf_path: str, out_dir: str | None = None) -> dict:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(pdf_path))
    route = triage(pdf_path)
    if route == "B":
        deferred = os.path.join(out_dir, "_deferred_born_digital")
        os.makedirs(deferred, exist_ok=True)
        with open(os.path.join(deferred, stem + ".txt"), "w", encoding="utf-8") as f:
            f.write(pdf_path + "\n")
        return {"route": "B", "md_path": None, "selfcheck": None}

    # A / C:OCR 主路
    all_blocks: list[dict] = []
    md_pages: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        pngs = pdf_to_pngs(pdf_path, os.path.join(tmp, "png"))
        for png in pngs:
            blocks = predict_page(png, os.path.join(tmp, "json"))
            all_blocks.extend(blocks)
            md_pages.append(reconstruct_markdown(blocks))
    md = "\n\n".join(md_pages) + "\n"

    doc_out = os.path.join(out_dir, stem)
    os.makedirs(doc_out, exist_ok=True)
    md_path = os.path.join(doc_out, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    check = block_coverage(all_blocks, md)
    check["katex_incompat"] = katex_incompat_scan(md)   # Tier0:KaTeX 渲染兼容 lint
    return {"route": route, "md_path": md_path, "selfcheck": check}


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 单文档转换")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    args = ap.parse_args()
    res = convert_pdf(args.src, args.out)
    print(f"[route={res['route']}] md={res['md_path']}")
    if res["selfcheck"]:
        c = res["selfcheck"]
        print(f"[Tier0] blocks {c['in_md']}/{c['total']} 覆盖, 缺 {len(c['missing'])}")
        for m in c["missing"]:
            print("   MISSING:", m)
        if c.get("katex_incompat"):
            print("[Tier0] KaTeX 不兼容残留:", ", ".join(c["katex_incompat"]))


if __name__ == "__main__":
    main()
