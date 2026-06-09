"""单篇美国专利 PDF → 结构化 Markdown 的编排器。

流程：分类 → 封面(元数据+摘要) → 正文(双栏重排, 分节, 切claims) →
附图(渲染+标题) → 前置页(线性重排) → 组装 YAML+分节 → 清洗 → Tier0 自检。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

import selfcheck
from bib_parse import parse_cover
from claims import find_claims_split, structure_claims
from figures import extract_figures
from page_classify import PageKind, classify_document
from profiles import LayoutProfile, get_profile
from reading_order import reconstruct, reconstruct_linear

_ALLCAPS_RE = re.compile(r"^[^a-z]*$")
# 段首"全大写连续词串 + 紧跟 Title-case 词" —— 用于行内标题(标题与正文未分段时)。
# 要求正文以 Title-case 起（[A-Z][a-z]），从而排除 "A DBS system…"(冠词+缩写+小写)误判。
_INLINE_HEAD_RE = re.compile(r"^((?:[A-Z][A-Z0-9&/.\-]*\s+)+)(?=[A-Z][a-z])")


@dataclass
class ConvertResult:
    name: str
    out_md: Path
    meta: dict
    selfcheck: dict
    n_body_pages: int
    n_figures: int
    suspect_pages: list[int] = field(default_factory=list)


def _topic_tag(stem: str) -> str:
    m = re.search(r"_(T\d[A-Za-z]*|X[A-Za-z]*)_?(.*)$", stem)
    if m:
        return "_".join(p for p in m.groups() if p)
    return ""


def _is_allcaps(p: str) -> bool:
    return bool(_ALLCAPS_RE.match(p)) and any(c.isalpha() for c in p)


def _looks_like_heading(p: str, keywords: tuple[str, ...]) -> bool:
    """全大写段够格当章节标题吗：匹配已知章节关键词，或 ≥3 个全大写词且够长。
    （≥3 词排除 "A DBS" 这类 冠词+缩写 的孤立全大写串被误判。）"""
    norm = re.sub(r"[-\s]+", " ", p.upper()).strip()
    if any(norm == k or norm.startswith(k + " ") for k in keywords):
        return True
    return len(p.split()) >= 3 and len(p) >= 12


def _mark_headings(text: str, profile: LayoutProfile) -> str:
    """章节标题转 '## '。两条判据(不再用宽松全大写前缀正则)：
    (a) 整段全大写 → 先合并紧邻的全大写段(跨行居中标题，如 CROSS REFERENCES TO
        RELATED / APPLICATIONS)，再判是否够格(关键词或 ≥3 词)；
    (b) 行内：段首全大写串匹配已知章节关键词、且后接 Title-case 正文 → 拆标题+正文。"""
    keywords = tuple(re.sub(r"[-\s]+", " ", k.upper()).strip() for k in profile.section_keywords)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out: list[str] = []
    i = 0
    while i < len(paras):
        p = paras[i]
        if _is_allcaps(p):
            block = [p]
            while i + 1 < len(paras) and _is_allcaps(paras[i + 1]):
                i += 1
                block.append(paras[i])
            joined = " ".join(block)
            out.append(f"## {joined.title()}" if _looks_like_heading(joined, keywords) else joined)
            i += 1
            continue
        m = _INLINE_HEAD_RE.match(p)
        if m:
            run = m.group(1).strip()
            norm = re.sub(r"[-\s]+", " ", run.upper()).strip()
            if any(norm == k or norm.startswith(k + " ") for k in keywords):
                out.append(f"## {run.title()}")
                rest = p[m.end():].strip()
                if rest:
                    out.append(rest)
                i += 1
                continue
        out.append(p)
        i += 1
    return "\n\n".join(out)


def _strip_leading_title_block(text: str, title: str = "") -> str:
    """去掉正文开头重复的全大写发明标题段。只剥与封面标题词集重合的全大写段，
    遇到不属于标题的全大写段(真章节标题，如 CROSS REFERENCES)即停，避免连累。
    无封面标题可比时退回旧行为(剥所有前导全大写)。"""
    title_words = set(re.findall(r"[A-Za-z]+", title.upper()))
    paras = text.split("\n\n")
    i = 0
    while i < len(paras):
        p = paras[i].strip()
        if not (_ALLCAPS_RE.match(p) and len(p) > 3 and any(c.isalpha() for c in p)):
            break
        pwords = set(re.findall(r"[A-Za-z]+", p.upper()))
        if title_words and pwords and not pwords <= title_words:
            break
        i += 1
    return "\n\n".join(paras[i:]).strip()


def _yaml_frontmatter(meta: dict, topic_tag: str, source_pdf: str) -> str:
    def esc(v: str) -> str:
        v = (v or "").replace('"', "'").strip()
        return f'"{v}"' if v else '""'

    lines = ["---"]
    lines.append(f"patent_number: {esc(meta.get('patent_number'))}")
    lines.append(f"title: {esc(meta.get('title'))}")
    inv = meta.get("inventors") or []
    if inv:
        lines.append("inventors:")
        lines += [f"  - {esc(n)}" for n in inv]
    else:
        lines.append("inventors: []")
    lines.append(f"assignee: {esc(meta.get('assignee'))}")
    lines.append(f"date_granted: {esc(meta.get('date_granted'))}")
    cls = meta.get("classifications") or []
    if cls:
        lines.append("classifications:")
        lines += [f"  - {esc(c)}" for c in cls]
    else:
        lines.append("classifications: []")
    lines.append(f"topic_tag: {esc(topic_tag)}")
    lines.append(f"source_pdf: {esc(source_pdf)}")
    lines.append("---")
    return "\n".join(lines)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def convert(pdf_path: Path, out_dir: Path, profile: LayoutProfile | None = None) -> ConvertResult:
    profile = profile or get_profile()
    stem = pdf_path.stem
    doc = fitz.open(str(pdf_path))
    infos = classify_document(doc, profile)

    covers = [i for i in infos if i.kind == PageKind.COVER]
    fronts = [i for i in infos if i.kind == PageKind.FRONT_MATTER]
    bodies = [i for i in infos if i.kind == PageKind.SPEC_BODY]
    figs = [i for i in infos if i.kind == PageKind.FIGURE]

    expected_words: list[str] = []

    # --- 封面 ---
    meta, abstract = ({"patent_number": stem, "title": "", "inventors": [],
                       "assignee": "", "date_granted": ""}, "")
    if covers:
        meta, abstract = parse_cover(covers[0], profile, stem)
        expected_words += [meta.get("title", "")] + meta.get("inventors", []) + [abstract]

    # --- 正文双栏 ---
    body_text_parts = []
    for info in sorted(bodies, key=lambda i: i.index):
        txt, kept, _ = reconstruct(info.words, info.height, info.gutter_x, profile)
        body_text_parts.append(txt)
        expected_words += [w.text for w in kept]
    full_spec = "\n\n".join(p for p in body_text_parts if p.strip())
    full_spec = _strip_leading_title_block(full_spec, meta.get("title", ""))

    desc_text, claims_text = find_claims_split(full_spec, profile)
    desc_md = _mark_headings(desc_text, profile)
    claims_md = structure_claims(claims_text) if claims_text else ""

    # --- 附图 ---
    name = stem
    artifacts_dir = out_dir / f"{name}_artifacts"
    figure_pages = extract_figures(doc, sorted(figs, key=lambda i: i.index), profile, artifacts_dir, name)
    fig_labels_all = [lab for fp in figure_pages for lab in fp.fig_labels]

    # --- 前置引用页（低价值，线性重排，附录化）---
    front_parts = []
    for info in sorted(fronts, key=lambda i: i.index):
        txt, kept = reconstruct_linear(info.words, info.height, profile)
        if txt.strip():
            front_parts.append(txt)
            expected_words += [w.text for w in kept]
    front_md = "\n\n".join(front_parts)

    # --- 组装 ---
    parts = [_yaml_frontmatter(meta, _topic_tag(stem), pdf_path.name), ""]
    if meta.get("title"):
        parts.append(f"# {meta['title']}\n")
    if abstract:
        parts.append("## Abstract\n")
        parts.append(abstract + "\n")
    if desc_md.strip():
        parts.append(desc_md + "\n")
    if claims_md.strip():
        parts.append("## Claims\n")
        parts.append(claims_md + "\n")
    elif claims_text:  # 找到 claims 段但未能结构化 → 原文兜底，绝不丢内容
        parts.append("## Claims\n")
        parts.append("> [转换提示] 权项编号未能自动结构化，以下为原文。\n")
        parts.append(re.sub(r"\s+", " ", claims_text).strip() + "\n")
    elif full_spec:
        parts.append("> [转换提示] 未检测到标准 claims 标记，权利要求可能并入说明书。\n")
    if figure_pages:
        parts.append("## Drawings\n")
        for seq, fp in enumerate(figure_pages, 1):
            cap = ", ".join(fp.fig_labels) if fp.fig_labels else f"Drawing sheet {seq}"
            parts.append(f"![{cap}]({fp.image_rel})\n\n*{cap}*\n")
    if front_md.strip():
        parts.append("## References Cited & Classifications\n")
        parts.append(front_md + "\n")

    final_md = _clean("\n".join(parts))

    out_dir.mkdir(parents=True, exist_ok=True)
    out_md = out_dir / f"{name}.md"
    out_md.write_text(final_md, encoding="utf-8")

    lint_text = final_md.split("## References Cited")[0]
    report = selfcheck.run(expected_words, final_md, claims_md, fig_labels_all, profile, lint_text=lint_text)
    (out_dir / f"{name}_selfcheck.json").write_text(
        __import__("json").dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return ConvertResult(
        name=name, out_md=out_md, meta=meta, selfcheck=report,
        n_body_pages=len(bodies), n_figures=len(figure_pages),
    )
