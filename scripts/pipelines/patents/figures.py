"""附图页处理：整页渲染为 PNG（去页眉页脚带）+ 抓取 "FIG. N" 标题。

美国专利附图页通常是整页矢量/位图工程图 + 顶部 "U.S. Patent ... Sheet N of M"
页眉 + 图内 "FIG. N" 标注。逐图精确切割不可靠，故整页渲染最忠实；
用 FIG 标注作为该页的图号标签与引用锚点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import fitz

from page_classify import PageInfo
from profiles import LayoutProfile


@dataclass
class FigurePage:
    page_index: int
    fig_labels: list[str] = field(default_factory=list)
    image_rel: str = ""


def _fig_labels(info: PageInfo, profile: LayoutProfile) -> list[str]:
    """从整页文本里抓 "FIG. N"（"FIG"/"."/数字在词层是分开的 token，必须按整页文本匹配）。"""
    page_text = " ".join(w.text for w in info.words)
    labels: list[str] = []
    for m in profile.figure_caption_re.finditer(page_text):
        lab = f"FIG. {m.group(1).upper()}"
        if lab not in labels:
            labels.append(lab)
    return labels


def extract_figures(
    doc: "fitz.Document",
    figure_pages: list[PageInfo],
    profile: LayoutProfile,
    artifacts_dir: Path,
    name: str,
    dpi: int = 150,
) -> list[FigurePage]:
    out: list[FigurePage] = []
    if not figure_pages:
        return out
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0
    mat = fitz.Matrix(scale, scale)
    for info in figure_pages:
        page = doc[info.index]
        H, W = info.height, info.width
        clip = fitz.Rect(0, H * 0.055, W, H * 0.945)  # 去掉页眉页脚带
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        fname = f"fig_p{info.index:03d}.png"
        pix.save(str(artifacts_dir / fname))
        out.append(
            FigurePage(
                page_index=info.index,
                fig_labels=_fig_labels(info, profile),
                image_rel=f"{name}_artifacts/{fname}",
            )
        )
    return out
