#!/usr/bin/env python3
"""crosscheck_words.py — PyMuPDF4LLM extract_words 第二取词交叉校验（阶段 0，离线）。

目的：堵 Tier0 盲区——主自检的期望集 = 引擎"保留词"，被 strip 掉的词（行号/
页眉/栏号）不在校验内，若误删真内容 Tier0 看不见（经验 L4）。本工具用
pymupdf4llm 的 `extract_words`（确定性、带坐标、独立实现）做全量取词基线，
把转换引擎整体当黑盒，对最终 markdown 做词级覆盖审计：

  1) 取词奇偶校验：baseline 词集 vs 引擎取词层（page_classify.page_words）
     的归一化 token 多重集逐页比对 —— 两个独立取词器应一致。
  2) 全词集覆盖：审计页（SPEC_BODY / FRONT_MATTER）的每个 baseline 词，
     在最终 md 的 token 多重集中找归宿。
  3) 缺失词独立归因：未找到归宿的词，用 baseline 自身坐标重新判定是否为
     已知噪声（中央行号 / 页眉页脚 / 栏号页码行）或连字符合并；判定逻辑
     与引擎删除记录无关（避免同义反复）。剩余 → unexplained 告警。

不校验词序：baseline 自身的多栏读序无保证（官方明示），序 diff 只会产噪声。
COVER / FIGURE 页不审计：设计上有损（封面→YAML+摘要，附图→PNG），全词
覆盖会刷屏假告警；其页型在报告中如实列出。

约束（API 强制）：extract_words 需 use_layout(False) + page_chunks=True；
固定 hdr_info=False 关闭字号判标题。

用法（H.5 自适应 I/O）:
    python scripts/pipelines/patents/crosscheck_words.py            # 默认源目录全量
    python scripts/pipelines/patents/crosscheck_words.py --src <pdf|dir> [...]
    python scripts/pipelines/patents/crosscheck_words.py --src a.pdf --md-root 03_Output/patents
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pymupdf4llm

from page_classify import PageKind, classify_document, page_words
from profiles import LayoutProfile, get_profile
from reading_order import _is_footer_noise, group_lines, join_line, median_height, Word, Y_TOL_RATIO

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_PATENTS_SRC", str(PROJECT_ROOT / "02_Source" / "patents"))
)
OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "patents"

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# 审计页型：引擎承诺零丢失的页（双栏正文 + 前置引用页）
_AUDITED_KINDS = {PageKind.SPEC_BODY, PageKind.FRONT_MATTER}


def _subtokens(text: str) -> list[str]:
    """词 → 归一化子 token（小写字母数字连续段）。'U.S.'→['u','s']，'10,155'→['10','155']。"""
    return _TOKEN_RE.findall(text.lower())


def _baseline_pages(pdf_path: Path) -> list[list[Word]]:
    """pymupdf4llm 第二取词器：每页词列表（确定性路径）。"""
    pymupdf4llm.use_layout(False)
    chunks = pymupdf4llm.to_markdown(
        str(pdf_path), page_chunks=True, extract_words=True, hdr_info=False
    )
    pages: list[list[Word]] = []
    for ch in chunks:
        pages.append([Word(w[0], w[1], w[2], w[3], w[4]) for w in ch.get("words", []) if str(w[4]).strip()])
    return pages


def _noise_word_ids(words: list[Word], page_height: float, gutter_x: float,
                    kind: PageKind, profile: LayoutProfile) -> dict[int, str]:
    """用 baseline 自身坐标独立判噪声。返回 {词下标: 噪声类别}。

    判定面刻意与引擎规则同构（同一版式事实），但输入是第二取词器的词框，
    判定在此独立执行——引擎若把真内容误塞进删除桶，这里不会跟着错。
    """
    out: dict[int, str] = {}
    if not words:
        return out
    top = profile.header_band_frac * page_height
    bot = profile.footer_band_frac * page_height
    y_tol = Y_TOL_RATIO * median_height(words)

    idx_of = {id(w): i for i, w in enumerate(words)}
    zone = [w for w in words if w.yc < top or w.yc > bot]
    for ln in group_lines(zone, y_tol):
        line_txt = join_line(ln, 2.0).strip()
        if profile.running_header_re.search(line_txt) or _is_footer_noise(line_txt):
            for w in ln:
                out[idx_of[id(w)]] = "header_footer"

    if kind == PageKind.SPEC_BODY and gutter_x > 0:
        hw = profile.line_number_band_halfwidth
        for i, w in enumerate(words):
            if i not in out and w.text.isdigit() and abs(w.xc - gutter_x) <= hw:
                out[i] = "line_number"
    return out


def _parity_diff(baseline: list[Word], engine: list[Word]) -> dict | None:
    """两取词器差异。按**字符多重集**判等（对词分组粒度不敏感："81"+"8" 与
    "818" 视为一致）；不等时附 token 级样本辅助定位。一致返回 None。"""
    bc = Counter(c for w in baseline for t in _subtokens(w.text) for c in t)
    ec = Counter(c for w in engine for t in _subtokens(w.text) for c in t)
    if bc == ec:
        return None
    b = Counter(t for w in baseline for t in _subtokens(w.text))
    e = Counter(t for w in engine for t in _subtokens(w.text))
    return {
        "chars_only_baseline": dict((bc - ec).most_common(10)),
        "chars_only_engine": dict((ec - bc).most_common(10)),
        "tokens_only_baseline": dict((b - e).most_common(10)),
        "tokens_only_engine": dict((e - b).most_common(10)),
    }


def _explain_claims_marker(unexplained_tokens: Counter, profile: LayoutProfile) -> bool:
    """残余未解释 token 是否可被某个 claims 起始标记完全解释。
    引擎在 claims 切分时会剔除标记词本身（如 "What is claimed is:"），
    属已知有意变换 —— 残余 ⊆ 某标记的 token 多重集即归因成立。"""
    for marker in profile.claims_markers:
        if not (unexplained_tokens - Counter(_subtokens(marker))):
            return True
    return False


def crosscheck(pdf_path: Path, md_path: Path, profile: LayoutProfile | None = None) -> dict:
    """单篇交叉校验 → 报告 dict。"""
    profile = profile or get_profile()
    import fitz

    md_text = md_path.read_text(encoding="utf-8")
    md_tokens = Counter(_TOKEN_RE.findall(md_text.lower()))
    md_token_set = [t for t in md_tokens if len(t) >= 4]   # 合并归因用（substring 候选）

    doc = fitz.open(str(pdf_path))
    infos = classify_document(doc, profile)
    baseline = _baseline_pages(pdf_path)
    if len(baseline) != len(infos):
        raise RuntimeError(f"页数不一致: baseline={len(baseline)} engine={len(infos)}")

    page_reports: list[dict] = []
    parity_pages_audited = 0
    parity_pages_info = 0
    totals = Counter()

    for info in infos:
        bwords = baseline[info.index]
        rep: dict = {"page": info.index + 1, "kind": info.kind.value, "n_words": len(bwords)}

        diff = _parity_diff(bwords, page_words(doc[info.index]))
        if diff:
            rep["parity_diff"] = diff
            if info.kind in _AUDITED_KINDS:
                parity_pages_audited += 1
            else:
                parity_pages_info += 1

        if info.kind not in _AUDITED_KINDS:
            rep["audited"] = False
            page_reports.append(rep)
            continue
        rep["audited"] = True

        noise = _noise_word_ids(bwords, info.height, info.gutter_x, info.kind, profile)
        n_noise = Counter()
        merged: list[str] = []
        unexplained: list[dict] = []
        for i, w in enumerate(bwords):
            if i in noise:
                n_noise[noise[i]] += 1          # 预期缺失，不消耗 md token
                continue
            toks = _subtokens(w.text)
            if not toks:
                continue                         # 纯标点词不入词级审计
            if all(md_tokens[t] > 0 for t in toks):
                for t in toks:
                    md_tokens[t] -= 1
                continue
            # 连字符断词合并归因："exten-"+"sion"→md "extension"
            if all(len(t) >= 3 and any(t in mt for mt in md_token_set) for t in toks):
                merged.append(w.text)
                continue
            unexplained.append({
                "text": w.text,
                "bbox": [round(w.x0, 1), round(w.y0, 1), round(w.x1, 1), round(w.y1, 1)],
            })

        totals.update({"noise_" + k: v for k, v in n_noise.items()})
        totals["merged"] += len(merged)
        rep["noise"] = dict(n_noise)
        rep["merged"] = len(merged)
        rep["unexplained"] = unexplained
        page_reports.append(rep)

    doc.close()

    # 文档级后处理：残余未解释词若恰为被剔除的 claims 起始标记 → 已知变换，归因
    all_unexplained = Counter(
        t for r in page_reports for u in r.get("unexplained", ()) for t in _subtokens(u["text"])
    )
    if all_unexplained and "## Claims" in md_text and _explain_claims_marker(all_unexplained, profile):
        for r in page_reports:
            if r.get("unexplained"):
                totals["claims_marker"] += len(r["unexplained"])
                r["claims_marker"] = [u["text"] for u in r.pop("unexplained")]
                r["unexplained"] = []
    for r in page_reports:
        if r.get("audited"):
            totals["unexplained"] += len(r["unexplained"])
            r["n_unexplained"] = len(r["unexplained"])
            r["unexplained"] = r["unexplained"][:20]

    audited = [r["page"] for r in page_reports if r.get("audited")]
    skipped = {r["page"]: r["kind"] for r in page_reports if not r.get("audited")}
    passed = totals["unexplained"] == 0 and parity_pages_audited == 0
    return {
        "source_pdf": pdf_path.name,
        "markdown": md_path.name,
        "engine": "pymupdf4llm extract_words (use_layout=False, hdr_info=False)",
        "n_pages": len(page_reports),
        "audited_pages": audited,
        "skipped_pages": skipped,
        "parity_diff_pages_audited": parity_pages_audited,
        "parity_diff_pages_info": parity_pages_info,
        "summary": dict(totals),
        "passed": passed,
        "pages": page_reports,
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


def _locate_md(pdf: Path, md_root: Path) -> Path | None:
    for cand in (md_root / pdf.stem / f"{pdf.stem}.md", md_root / f"{pdf.stem}.md"):
        if cand.exists():
            return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", nargs="*", default=[str(SOURCE_ROOT)],
                    help="PDF 文件或目录，可多个（默认 SCHOLARMD_PATENTS_SRC / 02_Source/patents）")
    ap.add_argument("--md-root", default=str(OUTPUT_ROOT),
                    help="转换产物根目录，按 <stem>/<stem>.md 定位（默认 03_Output/patents）")
    ap.add_argument("--out", default=None,
                    help="报告输出目录（默认落到对应 md 所在目录）")
    args = ap.parse_args()

    pdfs = collect_pdfs(args.src)
    if not pdfs:
        print("未找到 PDF。")
        return 1
    md_root = Path(args.md_root)
    profile = get_profile()

    print(f"[{datetime.now():%H:%M:%S}] 交叉校验 {len(pdfs)} 份（第二取词器 vs 转换产物）\n")
    alerts = failed = 0
    for pdf in pdfs:
        md = _locate_md(pdf, md_root)
        if md is None:
            print(f"  [SKIP] {pdf.stem} — 未找到转换产物（先跑 batch_patents.py）")
            continue
        try:
            rep = crosscheck(pdf, md, profile)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {pdf.stem}: {e}")
            continue
        out_dir = Path(args.out) if args.out else md.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_json = out_dir / f"{pdf.stem}_crosscheck.json"
        out_json.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

        s = rep["summary"]
        flag = "OK" if rep["passed"] else "ALERT"
        if not rep["passed"]:
            alerts += 1
        print(f"  [{flag}] {pdf.stem} — 审计 {len(rep['audited_pages'])}/{rep['n_pages']} 页 "
              f"噪声(行号={s.get('noise_line_number', 0)} 页眉={s.get('noise_header_footer', 0)} "
              f"claims标记={s.get('claims_marker', 0)}) 合并={s.get('merged', 0)} "
              f"未解释={s.get('unexplained', 0)} 取词差异页(审计/非审计)="
              f"{rep['parity_diff_pages_audited']}/{rep['parity_diff_pages_info']} → {out_json.name}")
        if not rep["passed"]:
            for pr in rep["pages"]:
                for u in pr.get("unexplained", [])[:3]:
                    print(f"        · p{pr['page']} {u['text']!r} @ {u['bbox']}")

    print(f"\n{'=' * 56}\n交叉校验完成: {len(pdfs) - alerts - failed} OK / {alerts} 告警 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
