#!/usr/bin/env python3
"""check_annotations.py — 把所有者的标记资产化为回归断言（SOP-07 §5）。

读取产物目录的标记文件（归档件 *_annotations_resolved*.json 全量 + 未归档导出件
*_annotations.json 如有），对词级可断言类别重跑引擎现算、坐标级验证：

  wrong_del(误删)  → 该 bbox 处的词必须在 kept 桶，且其 token 出现在最终 md
  missed_del(漏删) → 该 bbox 处的词必须在 removed 桶
  conv_err / missed_rec / 区域标记 → 不可机器断言,列入"人工复核"清单(带 note)

归档件中 resolution 为 wontfix / invalid 的条目跳过断言。
任何断言失败 → 退出码 1。**每次改引擎判定逻辑后必跑**（与 Tier0 / crosscheck 并列），
保证本次修改不破以往按标记修过的问题。

用法（H.5 自适应 I/O）:
    python scripts/pipelines/patents/check_annotations.py
    python scripts/pipelines/patents/check_annotations.py --src <pdf|dir> [--md-root <dir>]
输出: <md-root>/<stem>/<stem>_annotations_check.json + 控制台摘要
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import fitz

from page_classify import PageKind, classify_document
from profiles import LayoutProfile, get_profile
from reading_order import Word, strip_bands, strip_line_numbers

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_PATENTS_SRC", str(PROJECT_ROOT / "02_Source" / "patents"))
)
OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "patents"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ASSERTABLE = {"wrong_del", "missed_del"}


def _load_annotations(md_root: Path, stem: str) -> list[dict]:
    """归档件全量 + 未归档导出件(如有)。每条附 _source 便于报告归因。"""
    out: list[dict] = []
    files = sorted((md_root / stem).glob(f"{stem}_annotations_resolved*.json"))
    open_file = md_root / stem / f"{stem}_annotations.json"
    if open_file.exists():
        files.append(open_file)
    for f in files:
        try:
            rep = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [WARN] 跳过损坏标记文件 {f.name}: {e}")
            continue
        for a in rep.get("annotations", []):
            a["_source"] = f.name
            out.append(a)
    return out


def _page_buckets(info, profile: LayoutProfile) -> tuple[list[Word], list[Word]] | None:
    """重跑该页剔除管线 → (kept, removed)。非文本页返回 None。"""
    if info.kind == PageKind.SPEC_BODY:
        body, rm1 = strip_bands(info.words, info.height, profile)
        body, rm2 = strip_line_numbers(body, info.gutter_x, profile)
        return body, rm1 + rm2
    if info.kind == PageKind.FRONT_MATTER:
        body, rm = strip_bands(info.words, info.height, profile)
        return body, rm
    return None


def _find_word(words: list[Word], bbox: list[float]) -> Word | None:
    """bbox 中心点定位词（容差 1pt）。"""
    x0, y0, x1, y1 = bbox
    for w in words:
        if x0 - 1 <= w.xc <= x1 + 1 and y0 - 1 <= w.yc <= y1 + 1:
            return w
    return None


def check_document(pdf_path: Path, md_root: Path, profile: LayoutProfile) -> dict | None:
    """单篇全部标记断言 → 报告 dict；无标记文件返回 None。"""
    stem = pdf_path.stem
    annotations = _load_annotations(md_root, stem)
    if not annotations:
        return None

    md_path = md_root / stem / f"{stem}.md"
    md_tokens = set(_TOKEN_RE.findall(md_path.read_text(encoding="utf-8").lower())) if md_path.exists() else set()

    doc = fitz.open(str(pdf_path))
    infos = classify_document(doc, profile)
    buckets_cache: dict[int, tuple[list[Word], list[Word]] | None] = {}

    passed, failed, manual, skipped = [], [], [], []
    for a in annotations:
        ident = {"page": a.get("page"), "text": a.get("text", ""), "bbox": a.get("bbox"),
                 "cat": a.get("cat"), "note": a.get("note", ""), "source": a["_source"]}
        if a.get("resolution") in ("wontfix", "invalid"):
            skipped.append({**ident, "why": f"resolution={a['resolution']}"})
            continue
        if a.get("kind") == "region" or a.get("cat") not in _ASSERTABLE:
            manual.append(ident)
            continue
        idx = a["page"] - 1
        if not (0 <= idx < len(infos)):
            failed.append({**ident, "why": f"页码越界(共 {len(infos)} 页)"})
            continue
        if idx not in buckets_cache:
            buckets_cache[idx] = _page_buckets(infos[idx], profile)
        buckets = buckets_cache[idx]
        if buckets is None:
            failed.append({**ident, "why": f"页型 {infos[idx].kind.value} 不走剔除管线,词级断言无意义"})
            continue
        kept, removed = buckets
        if a["cat"] == "wrong_del":
            w = _find_word(kept, a["bbox"])
            if w is None:
                failed.append({**ident, "why": "kept 桶中该坐标无词(仍被剔除或坐标失配)"})
            elif md_tokens and not all(t in md_tokens for t in _TOKEN_RE.findall(w.text.lower())):
                failed.append({**ident, "why": f"词 {w.text!r} 在 kept 桶但未出现在 md"})
            else:
                passed.append(ident)
        else:  # missed_del
            w = _find_word(removed, a["bbox"])
            if w is None:
                failed.append({**ident, "why": "removed 桶中该坐标无词(噪声仍未被剔除)"})
            else:
                passed.append(ident)
    doc.close()

    return {
        "source_pdf": pdf_path.name,
        "checked_at": f"{datetime.now():%Y-%m-%d %H:%M}",
        "n_annotations": len(annotations),
        "passed": len(passed),
        "failed": failed,
        "manual_review": manual,
        "skipped": skipped,
        "ok": not failed,
    }


# ---------------- CLI（H.5 自适应 I/O） ----------------

def collect_pdfs(srcs: list[str]) -> list[Path]:
    seen: dict[Path, None] = {}
    for s in srcs:
        p = Path(s)
        if p.is_dir():
            for f in sorted(p.glob("*.pdf")):
                seen.setdefault(f.resolve())
        elif p.suffix.lower() == ".pdf" and p.exists():
            seen.setdefault(p.resolve())
        else:
            print(f"  [WARN] 跳过不存在/非 PDF: {s}")
    return list(seen)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", nargs="*", default=[str(SOURCE_ROOT)],
                    help="PDF 文件或目录，可多个（默认 SCHOLARMD_PATENTS_SRC / 02_Source/patents）")
    ap.add_argument("--md-root", default=str(OUTPUT_ROOT),
                    help="产物根目录（标记文件与 md 所在，默认 03_Output/patents）")
    args = ap.parse_args()

    pdfs = collect_pdfs(args.src)
    if not pdfs:
        print("未找到 PDF。")
        return 1
    md_root = Path(args.md_root)
    profile = get_profile()

    print(f"[{datetime.now():%H:%M:%S}] 标记回归断言 {len(pdfs)} 份\n")
    any_fail = checked = 0
    for pdf in pdfs:
        rep = check_document(pdf, md_root, profile)
        if rep is None:
            print(f"  [SKIP] {pdf.stem} — 无标记文件")
            continue
        checked += 1
        out = md_root / pdf.stem / f"{pdf.stem}_annotations_check.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        flag = "OK" if rep["ok"] else "FAIL"
        if not rep["ok"]:
            any_fail += 1
        print(f"  [{flag}] {pdf.stem} — 断言通过 {rep['passed']} / 失败 {len(rep['failed'])} "
              f"/ 人工复核 {len(rep['manual_review'])} / 跳过 {len(rep['skipped'])} → {out.name}")
        for f in rep["failed"][:5]:
            print(f"        ✗ p{f['page']} {f['text']!r} [{f['cat']}] — {f['why']}")
        for m in rep["manual_review"][:3]:
            note = f"({m['note']})" if m["note"] else "(无 note)"
            print(f"        ⚠ 人工: p{m['page']} [{m['cat']}] {note}")

    print(f"\n{'=' * 56}\n断言完成: 检查 {checked} 份 / {any_fail} 份有失败")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
