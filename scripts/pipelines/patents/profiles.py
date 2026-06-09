"""布局 profile —— 把"国家/版式相关的常量"集中在一处。

第一期只实现 US_GRANT（美国授权专利公告本）。第二期加 CN/EP/JP profile
时，只需复制一份改常量 + 章节关键词，reading_order 引擎无需改动。

所有几何量经 _PDF_Staging 的 5 份美国专利实测标定：
  - 页面 612×792 或 614×792（US Letter，高度恒为 792）
  - 双栏，中央装订线 gutter 处印行号（5 的倍数，5..65，x-center≈页宽/2）
  - 页眉 "US X,XXX,XXX B2"（y≈35-52）；首正文页含发明标题（y≈63 起）
  - 前置续页（References Cited / Classification）页眉含 "Page N"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LayoutProfile:
    name: str

    # --- 页型识别 ---
    cover_page_index: int = 0
    # 前置续页（引用文献表/分类号）页眉标记，用首若干词的拼接串匹配
    frontmatter_header_re: re.Pattern = field(
        default_factory=lambda: re.compile(r"\bPage\s+\d")
    )
    # 正文页：中央带内"5 的倍数"行号的最少个数
    ladder_min_count: int = 6
    ladder_band_halfwidth: float = 35.0   # 距页面中心的横向半宽
    ladder_value_max: int = 70
    # 附图页：词数上限（且通常含整页图像）
    figure_word_max: int = 150

    # --- 页眉/页脚搜索带（按页高比例）。带内按内容正则剔除，非整带删除 ---
    header_band_frac: float = 0.11        # y < frac*H 为页眉搜索区（≈87pt）
    footer_band_frac: float = 0.92        # y > frac*H 为页脚搜索区（≈728pt）
    # 运行页眉/页脚行的内容特征（专利号 / "U.S. Patent" / "Sheet N of M"）
    running_header_re: re.Pattern = field(
        default_factory=lambda: re.compile(
            r"(U\.?\s*S\.?\s*Patent|US\s*[\d,]+\s*B[12]|Sheet\s+\d+\s+of\s+\d+|^US0?\d{6,}|\bPage\s+\d+)",
            re.IGNORECASE,
        )
    )

    # --- 行号剔除 ---
    # 纯整数 token，且 x-center 距 gutter 在此半宽内 → 行号。
    # 实测：真行号在缝隙正中(距 gutter≈0)，栏内参考标号(如 "lead 76"、"FIGS. 7 and 8")
    # 距 gutter≥12pt。半宽取 8 可干净区分(原 16 会误删落在栏边缘的标号 → 静默丢内容)。
    line_number_band_halfwidth: float = 8.0

    # --- 分栏 ---
    # gutter 优先由行号阶梯实测；无阶梯时回退到页宽中点
    column_gap_min: float = 6.0           # 左右栏之间最小空隙（用于校验）

    # --- 空格重建 ---
    # 插空格阈值 = max(space_gap_abs, space_gap_ratio * 该页中位字符宽)
    space_gap_ratio: float = 0.55
    space_gap_abs: float = 1.5

    # --- 章节标题关键词（顺序≈出现顺序）---
    section_keywords: tuple[str, ...] = (
        "CROSS-REFERENCE",
        "FIELD OF THE INVENTION",
        "FIELD OF THE DISCLOSURE",
        "FIELD",
        "BACKGROUND",
        "SUMMARY",
        "BRIEF DESCRIPTION OF THE DRAWINGS",
        "BRIEF DESCRIPTION",
        "DETAILED DESCRIPTION",
        "DESCRIPTION",
    )
    # 权利要求段起始标记（按优先级）
    claims_markers: tuple[str, ...] = (
        "What is claimed is",
        "What is claimed",
        "The invention claimed is",
        "We claim",
        "I claim",
        "It is claimed",
    )
    # 图标题
    figure_caption_re: re.Pattern = field(
        default_factory=lambda: re.compile(r"FIG\s*\.?\s*([0-9]+[A-Z]?)", re.IGNORECASE)
    )
    # INID 字段（美国专利书目）
    inid_re: re.Pattern = field(
        default_factory=lambda: re.compile(r"\(\s*(\d{2})\s*\)")
    )


US_GRANT = LayoutProfile(name="US_GRANT")


def get_profile(name: str = "US_GRANT") -> LayoutProfile:
    profiles = {"US_GRANT": US_GRANT}
    if name not in profiles:
        raise ValueError(f"未知 profile: {name!r}，可用: {list(profiles)}")
    return profiles[name]
