"""从 Google Patents 结构化抓取引用文献（验证用途）。

为什么：专利 PDF 封面的 "References Cited" 只印一小部分(带 "(Continued)")且是
3 栏乱排，解析 PDF 不划算。Google Patents 页面是服务端渲染、带 schema.org
itemprop 机读数据，可**确定性**抓到完整、准确的引文：
  - backwardReferences      → 本专利引用的在先专利(含号/日期/受让人/标题)
  - detailedNonPatentLiterature → 引用的非专利文献(NPL，自由文本完整引文)
无需 API key。

用法:
    python scripts/patents/fetch_citations.py US10155111B2
    python scripts/patents/fetch_citations.py US10155111B2 --out 03_Output/patents/_citation_test
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_UA = {"User-Agent": "Mozilla/5.0 (compatible; patent-tooling/1.0)"}


@dataclass
class Citation:
    number: str = ""
    priority_date: str = ""
    publication_date: str = ""
    assignee: str = ""
    title: str = ""


@dataclass
class CitationSet:
    patent: str
    patent_citations: list[Citation] = field(default_factory=list)
    npl_citations: list[str] = field(default_factory=list)


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def _field(block: str, prop: str) -> str:
    m = re.search(rf'itemprop="{prop}"[^>]*>(.*?)</', block, re.DOTALL)
    return _strip_tags(m.group(1)) if m else ""


def fetch_html(patent: str) -> str:
    url = f"https://patents.google.com/patent/{patent}/en"
    return urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=30).read().decode("utf-8", "ignore")


def parse_citations(html: str, patent: str) -> CitationSet:
    cs = CitationSet(patent=patent)
    for b in re.findall(r'<tr itemprop="backwardReferences".*?</tr>', html, re.DOTALL):
        cs.patent_citations.append(Citation(
            number=_field(b, "publicationNumber"),
            priority_date=_field(b, "priorityDate"),
            publication_date=_field(b, "publicationDate"),
            assignee=_field(b, "assigneeOriginal"),
            title=_field(b, "title"),
        ))
    for b in re.findall(r'<tr itemprop="detailedNonPatentLiterature".*?</tr>', html, re.DOTALL):
        txt = _field(b, "title")
        if txt:
            cs.npl_citations.append(txt)
    return cs


def country_of(num: str) -> str:
    m = re.match(r"([A-Z]{2})", num or "")
    return m.group(1) if m else "?"


def render_markdown(cs: CitationSet) -> str:
    lines = [f"## References Cited (来源: Google Patents, 结构化抓取)", ""]
    lines.append(f"专利引文 {len(cs.patent_citations)} 条 · 非专利文献 {len(cs.npl_citations)} 条", )
    lines.append("")
    lines.append("### Patent Citations")
    lines.append("")
    lines.append("| Publication | Priority | Published | Assignee | Title |")
    lines.append("|---|---|---|---|---|")
    for c in cs.patent_citations:
        title = (c.title or "").replace("|", "\\|")
        lines.append(f"| {c.number} | {c.priority_date} | {c.publication_date} | {c.assignee} | {title} |")
    lines.append("")
    lines.append("### Non-Patent Citations")
    lines.append("")
    for n in cs.npl_citations:
        lines.append(f"- {n}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("patent", help="专利号，如 US10155111B2")
    ap.add_argument("--out", default="03_Output/patents/_citation_test")
    args = ap.parse_args()

    html = fetch_html(args.patent)
    cs = parse_citations(html, args.patent)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(cs)
    (out_dir / f"{args.patent}_references.md").write_text(md, encoding="utf-8")
    print(f"[{args.patent}] 专利引文 {len(cs.patent_citations)} · NPL {len(cs.npl_citations)} → {out_dir}/{args.patent}_references.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
