"""Tier1 云端 AI 审查（仅出报告，不改写）。

把"专利页图像 + 脚本重排出的该页文本"一起发给云端多模态模型，要求它**只输出
结构化问题清单**（页码/类别/严重度/原文片段/建议），不重写正文 —— 输出极小、
最省，且避免改写引入幻觉。模型可切换以便实测比价：
    --model anthropic:claude-haiku-4-5
    --model openai:gpt-5.5
    --model gemini:gemini-2.5-flash
API Key 取自环境变量 ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY。

设计为"先试云端"：默认抽样若干正文页（可 --all / --max-pages 调节）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import fitz

from page_classify import PageKind, classify_document
from profiles import get_profile
from reading_order import reconstruct

_SYSTEM = (
    "You are a meticulous patent-document QA reviewer. You are given (1) a rendered "
    "image of ONE page of a US patent and (2) the Markdown text our converter produced "
    "for that page. Compare them and report ONLY discrepancies that affect CONTENT "
    "correctness: wrong reading order, dropped/added text, garbled words, broken claim "
    "structure, mis-stripped line numbers/headers, wrong figure numbers. Ignore cosmetic "
    "whitespace. DO NOT rewrite or output corrected text. "
    'Respond with ONLY a JSON array; each item: '
    '{"severity":"high|medium|low","category":"...","observed":"<short quote>","note":"<why / suggestion>"}. '
    "If no issues, respond with []."
)


def _png_b64(page: "fitz.Page", dpi: int = 120) -> bytes:
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return pix.tobytes("png")


def _select_pages(infos, max_pages: int, do_all: bool) -> list[int]:
    body = [i.index for i in infos if i.kind == PageKind.SPEC_BODY]
    if do_all or len(body) <= max_pages:
        return body
    # 均匀抽样
    step = len(body) / max_pages
    return [body[int(k * step)] for k in range(max_pages)]


# ---------- provider 调度（懒加载，缺库时给出明确提示）----------

def _call_anthropic(model: str, img_png: bytes, page_md: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=1500,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(img_png).decode()}},
                {"type": "text", "text": f"Converter Markdown for this page:\n\n{page_md}"},
            ],
        }],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _call_openai(model: str, img_png: bytes, page_md: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    data_url = "data:image/png;base64," + base64.b64encode(img_png).decode()
    resp = client.chat.completions.create(
        model=model,
        max_tokens=1500,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": f"Converter Markdown for this page:\n\n{page_md}"},
            ]},
        ],
    )
    return resp.choices[0].message.content or ""


def _call_gemini(model: str, img_png: bytes, page_md: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"])
    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=img_png, mime_type="image/png"),
            f"{_SYSTEM}\n\nConverter Markdown for this page:\n\n{page_md}",
        ],
    )
    return resp.text or ""


_PROVIDERS = {"anthropic": _call_anthropic, "openai": _call_openai, "gemini": _call_gemini}


def call_model(model_spec: str, img_png: bytes, page_md: str) -> str:
    provider, _, model = model_spec.partition(":")
    if provider not in _PROVIDERS:
        raise ValueError(f"未知 provider {provider!r}，可用: {list(_PROVIDERS)}（形如 anthropic:claude-haiku-4-5）")
    if not model:
        raise ValueError("模型名缺失，形如 'anthropic:claude-haiku-4-5'")
    return _PROVIDERS[provider](model, img_png, page_md)


def _parse_issues(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("["):]
    try:
        start, end = raw.find("["), raw.rfind("]")
        return json.loads(raw[start:end + 1]) if start != -1 else []
    except Exception:
        return [{"severity": "low", "category": "parse_error", "observed": raw[:200], "note": "模型未返回合法 JSON"}]


def review_document(pdf_path: Path, out_dir: Path, model_spec: str,
                    max_pages: int = 3, do_all: bool = False) -> dict:
    profile = get_profile()
    doc = fitz.open(str(pdf_path))
    infos = classify_document(doc, profile)
    by_index = {i.index: i for i in infos}
    pages = _select_pages(infos, max_pages, do_all)

    results = []
    for idx in pages:
        info = by_index[idx]
        page_md, _, _ = reconstruct(info.words, info.height, info.gutter_x, profile)
        img = _png_b64(doc[idx])
        try:
            raw = call_model(model_spec, img, page_md)
            issues = _parse_issues(raw)
        except Exception as e:  # noqa: BLE001 — 单页失败不应中断整篇
            issues = [{"severity": "low", "category": "api_error", "observed": "", "note": str(e)}]
        results.append({"page_index": idx, "issues": issues})

    report = {"model": model_spec, "pages_reviewed": pages, "results": results}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{pdf_path.stem}_review.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_review_md(out_dir / f"{pdf_path.stem}_review.md", pdf_path.stem, report)
    return report


def _write_review_md(path: Path, name: str, report: dict) -> None:
    lines = [f"# AI 审查报告 — {name}", "",
             f"- 模型: `{report['model']}`",
             f"- 审查页(索引): {report['pages_reviewed']}", ""]
    total = 0
    for r in report["results"]:
        issues = r["issues"]
        total += len(issues)
        if not issues:
            continue
        lines.append(f"## 页 {r['page_index'] + 1}")
        for it in issues:
            lines.append(f"- **[{it.get('severity','?')}]** ({it.get('category','?')}) "
                         f"{it.get('observed','')} — {it.get('note','')}")
        lines.append("")
    if total == 0:
        lines.append("_未发现内容性问题。_")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", help="专利 PDF 路径")
    ap.add_argument("out_dir", help="转换输出目录（含 .md）")
    ap.add_argument("--model", default="anthropic:claude-haiku-4-5",
                    help="provider:model，如 anthropic:claude-haiku-4-5 / openai:gpt-5.5 / gemini:gemini-2.5-flash")
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--all", action="store_true", help="审查所有正文页")
    args = ap.parse_args()
    rep = review_document(Path(args.pdf), Path(args.out_dir), args.model, args.max_pages, args.all)
    n = sum(len(r["issues"]) for r in rep["results"])
    print(f"[review] {args.pdf} — {len(rep['pages_reviewed'])} 页, {n} 条问题 → {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
