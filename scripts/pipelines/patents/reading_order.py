"""核心几何阅读引擎（与取词来源解耦）。

输入：一页的"词框"列表 List[Word]（来自 PyMuPDF 文字层，或第二期的 OCR）。
输出：正确阅读顺序的纯文本（双栏左→右、剔行号、剔页眉页脚、重建段落与空格）。

设计要点：
  * 空格由"标点排版规则 + 词间几何间隙"联合决定 —— 根治 "and / or"、
    "2 , 433 , 480"、"Inc ." 这类把标点拆成独立 token 造成的过度空格。
  * 段落由"首行缩进 + 行间距"判定，跨栏自动续接。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from profiles import LayoutProfile


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


# 行聚类容差系数：PyMuPDF word.height≈行距×1.5，故取 0.30×height≈半行距，
# 既能并掉同行微抖(≈3pt)，又不会并掉相邻紧排行(实测最小行距≈7.7pt)。
Y_TOL_RATIO = 0.30

_CLOSE_PUNCT = set(",.;:!?)]}%’”'\"°")   # 附着到左侧（前面不留空格）
_OPEN_PUNCT = set("([{“‘$")               # 附着到右侧（后面不留空格）


def _is_punct_token(tok: str) -> bool:
    return len(tok) == 1 and not tok.isalnum()


def median_char_width(words: list[Word]) -> float:
    widths = [
        (w.x1 - w.x0) / len(w.text)
        for w in words
        if w.text.isalpha() and len(w.text) >= 3
    ]
    return statistics.median(widths) if widths else 6.0


def median_height(words: list[Word]) -> float:
    hs = [w.height for w in words if w.height > 0]
    return statistics.median(hs) if hs else 10.0


def _decide_space(prev: str, cur: str, gap: float, punct_thr: float) -> bool:
    """是否在 prev 与 cur 之间插空格。

    核心：PyMuPDF 已把词正确切分，故"词↔词"恒加空格（justified 排版下
    真实词隙可低至 0.5pt，几何阈值不可靠）；只有当一侧是标点时才用几何
    间隙裁决附着，从而既得到 "claims priority"，又得到 "10,155,111"、"U.S."。
    """
    if not cur or not prev:
        return False
    if cur[0] in _CLOSE_PUNCT and len(cur) == 1:
        return False
    if prev[-1] in _OPEN_PUNCT and len(prev) == 1:
        return False
    if cur in ("/", "-") or prev in ("/", "-"):
        return False
    # 逗号/分号后接字母 → 散文停顿，加空格（数字千分位 "10,155" 落到几何判定）
    if prev in (",", ";") and cur[:1].isalpha():
        return True
    if _is_punct_token(prev) or _is_punct_token(cur):
        return gap > punct_thr
    return True   # 两个真实词 → 永远加空格


def join_line(words: list[Word], punct_thr: float) -> str:
    """把一行的词拼成字符串（punct_thr 仅用于标点附着裁决）。"""
    words = sorted(words, key=lambda w: w.x0)
    out = ""
    prev_text = None
    prev_x1 = None
    for w in words:
        if prev_text is None:
            out = w.text
        else:
            gap = w.x0 - prev_x1
            out += (" " if _decide_space(prev_text, w.text, gap, punct_thr) else "") + w.text
        prev_text = w.text
        prev_x1 = w.x1
    return out


def group_lines(words: list[Word], y_tol: float) -> list[list[Word]]:
    """按 y 把词聚成视觉行，行内按 x 排序。返回按 y 升序的行列表。"""
    if not words:
        return []
    words = sorted(words, key=lambda w: w.yc)
    lines: list[list[Word]] = [[words[0]]]
    cur_y = words[0].yc
    for w in words[1:]:
        if abs(w.yc - cur_y) <= y_tol:
            lines[-1].append(w)
            # 用行内中位 y 更新，避免漂移
            cur_y = statistics.median([t.yc for t in lines[-1]])
        else:
            lines.append([w])
            cur_y = w.yc
    for ln in lines:
        ln.sort(key=lambda w: w.x0)
    return lines


def strip_bands(words: list[Word], page_height: float, profile: LayoutProfile) -> tuple[list[Word], list[Word]]:
    """剔除页眉/页脚。

    运行页眉（"US 10,155,111 B2" / "U.S. Patent ..." / "Page N"）的 y 位置
    随文档浮动且可能贴近正文标题，故不用固定 y 带，而是在顶/底区域内
    **按整行内容正则**匹配后整行剔除；页脚再叠加纯页码剔除。
    """
    top = profile.header_band_frac * page_height
    bot = profile.footer_band_frac * page_height
    head_re = profile.running_header_re

    kept, removed = [], []
    others = [w for w in words if top <= w.yc <= bot]
    head_zone = [w for w in words if w.yc < top]
    foot_zone = [w for w in words if w.yc > bot]

    # 顶部：逐行判断，命中页眉正则才剔除（保留发明标题等正文行）
    y_tol = Y_TOL_RATIO * median_height(words) if words else 4.0
    for zone in (head_zone, foot_zone):
        for ln in group_lines(zone, y_tol):
            line_txt = join_line(ln, 2.0).strip()
            if head_re.search(line_txt) or _is_footer_noise(line_txt):
                removed.extend(ln)
            else:
                kept.extend(ln)
    kept.extend(others)
    return kept, removed


# 纯栏号/页码行：单个整数，或左右两栏并排的两个整数（美国专利每栏顶部印栏号，
# 紧贴运行页眉下方，可带尾点如 "1."）。左右栏号同 y 会被 group_lines 并成一行，
# 故单纯 isdigit 判不出（"1. 2" / "11 12" 含空格/点），用此正则整行匹配。
_NUMBER_ROW_RE = re.compile(r"^\d{1,4}\.?(?:\s+\d{1,4}\.?)?$")


def _is_footer_noise(text: str) -> bool:
    """纯页码 / 栏号行（单个，或左右两栏并排的整数，允许尾点）。"""
    t = text.strip()
    return bool(t) and bool(_NUMBER_ROW_RE.match(t))


def strip_line_numbers(words: list[Word], gutter_x: float, profile: LayoutProfile) -> tuple[list[Word], list[Word]]:
    """剔除中央带内的纯整数行号。返回 (保留, 剔除)。"""
    hw = profile.line_number_band_halfwidth
    kept, removed = [], []
    for w in words:
        if w.text.isdigit() and abs(w.xc - gutter_x) <= hw:
            removed.append(w)
        else:
            kept.append(w)
    return kept, removed


def split_columns(words: list[Word], gutter_x: float) -> tuple[list[Word], list[Word]]:
    left = [w for w in words if w.xc < gutter_x]
    right = [w for w in words if w.xc >= gutter_x]
    return left, right


def _column_paragraph_infos(words: list[Word], threshold: float, y_tol: float, line_h: float) -> list[dict]:
    """单栏 → 段落信息列表（调试/可视化用富结构）。每段：
    {"text": 段文本, "lines": [行词列表...], "new_by": "indent"|"gap"|"first"}。
    首行缩进或大行距 → 新段；含软连字符续接。_column_paragraphs 的唯一实现。"""
    lines = group_lines(words, y_tol)
    if not lines:
        return []
    x0s = sorted(min(w.x0 for w in ln) for ln in lines)
    margin = x0s[max(0, len(x0s) // 10)]   # 10 分位作左边距，抗噪
    # 首行缩进是【水平】排版量(≈1em)，与【垂直】行高无关；旧的 1.2*line_h 量纲错且虚高
    # (实测 18.5pt 远高于真实缩进 9pt → 缩进段落全判不出)。改按字宽(em 代理)自适应：
    # 首行缩进≈2 字宽、续行抖动≈0.1 字宽，取 1 字宽作阈值，跨字号/文档稳健。
    indent_thr = max(2.5, 1.0 * median_char_width(words))
    gap_thr = 1.7 * line_h                  # 段间行距是【垂直】量，仍按行高

    paras: list[dict] = []
    cur: dict | None = None
    prev_y: float | None = None
    for ln in lines:
        first_x0 = min(w.x0 for w in ln)
        ln_y = statistics.median([w.yc for w in ln])
        text = join_line(ln, threshold)
        by_indent = first_x0 > margin + indent_thr
        by_gap = prev_y is not None and ln_y - prev_y > gap_thr
        if cur is None or by_indent or by_gap:
            if cur is not None:
                paras.append(cur)
            cur = {"text": text, "lines": [ln],
                   "new_by": "indent" if by_indent else ("gap" if by_gap else "first")}
        else:
            # 软连字符续接：上一段以字母+'-'结尾，且本行小写起 → 拼接去连字符
            t = cur["text"]
            if t.endswith("-") and len(t) >= 2 and t[-2].isalpha() and text[:1].islower():
                cur["text"] = t[:-1] + text
            else:
                cur["text"] = t + " " + text
            cur["lines"].append(ln)
        prev_y = ln_y
    if cur is not None:
        paras.append(cur)
    return paras


def _column_paragraphs(words: list[Word], threshold: float, y_tol: float, line_h: float) -> list[str]:
    """单栏 → 段落文本列表（主管线接口，行为不变）。"""
    return [p["text"] for p in _column_paragraph_infos(words, threshold, y_tol, line_h)]


def reconstruct(
    words: list[Word],
    page_height: float,
    gutter_x: float,
    profile: LayoutProfile,
) -> tuple[str, list[Word], list[Word]]:
    """一页双栏正文 → (markdown文本, 保留词, 剔除词)。"""
    removed_all: list[Word] = []
    body, rm = strip_bands(words, page_height, profile)
    removed_all += rm
    body, rm = strip_line_numbers(body, gutter_x, profile)
    removed_all += rm
    if not body:
        return "", [], removed_all

    punct_thr = max(profile.space_gap_abs, profile.space_gap_ratio * median_char_width(body))
    line_h = median_height(body)
    y_tol = Y_TOL_RATIO * line_h

    left, right = split_columns(body, gutter_x)
    paras = _column_paragraphs(left, punct_thr, y_tol, line_h)
    paras += _column_paragraphs(right, punct_thr, y_tol, line_h)

    text = "\n\n".join(p.strip() for p in paras if p.strip())
    return text, body, removed_all


def reconstruct_linear(
    words: list[Word],
    page_height: float,
    profile: LayoutProfile,
) -> tuple[str, list[Word]]:
    """单栏线性重排（用于前置引用/分类页等非双栏正文）。不剔行号。"""
    body, _ = strip_bands(words, page_height, profile)
    if not body:
        return "", []
    punct_thr = max(profile.space_gap_abs, profile.space_gap_ratio * median_char_width(body))
    line_h = median_height(body)
    lines = group_lines(body, Y_TOL_RATIO * line_h)
    text = "\n".join(join_line(ln, punct_thr) for ln in lines)
    return text, body
