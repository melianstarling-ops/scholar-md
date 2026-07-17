"""块级采信门:born-digital 路线 B 的"内容关键心脏"(计划 §6.3)。

纯正文块在通过**全部**采信门后,用 PDF 文本层字符**整块替换** OCR 的
block_content;其余一律回退 OCR。任何块不允许双源字符级拼接——采信是整块
原子决策:要么整块用源文本,要么整块用 OCR。

本模块只做判定 + 采信文本构建 + 原子替换 + 全量 provenance;编排、报告写盘
(Task 8)与 reconstruct 前的接线(Task 9)不在此实现(YAGNI)。

消费 source_audit 的公开接口:SourceWord / normalize_prose_for_content(NFC,
采信内容用)/ normalize_prose_for_compare(NFKC,对账比较用)/ assign_source_words
的返回结构(assignments / block_labels / geometry_unscorable)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from scripts.pipelines.textbooks.source_audit import (
    SourceWord,
    normalize_prose_for_compare,
    normalize_prose_for_content,
)

# ---- 采信白名单与永不采信(计划 §6.3,逐字) -------------------------------
# 白名单:仅这些 label 的块允许在通过全部门后采信源文本。
ADOPTABLE_LABELS = frozenset(
    {
        "text",
        "abstract",
        "reference_content",
        "content",
        "paragraph_title",
        "doc_title",
        "figure_title",
        "footnote",
    }
)

# 永不采信:内容永远走 OCR(仅作文档/审计参考;判定只看白名单成员资格,
# 任何不在白名单里的 label 一律 label_not_adoptable)。
NEVER_ADOPT_LABELS = frozenset(
    {
        "display_formula",
        "inline_formula",
        "formula_number",
        "table",
        "image",
        "chart",
        "header",
        "footer",
        "number",
        "header_image",
        "footer_image",
    }
)

# ---- 字符健康判定:与 source_audit 的惯例一致,独立维护(不导入其私有实现) --
_PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))

# 数学符号相关码点区(计划 §6.3 门 4:源 words 含数学符号区码点即判有数学)。
_MATH_RANGES = (
    (0x2190, 0x21FF),  # 箭头
    (0x2200, 0x22FF),  # 数学运算符
    (0x2A00, 0x2AFF),  # 补充数学运算符
    (0x27C0, 0x27EF),  # 杂项数学符号 A
    (0x2980, 0x29FF),  # 杂项数学符号 B
    (0x1D400, 0x1D7FF),  # 数学字母数字符号
)

# OCR 文本中的公式痕迹:$...$、\(、\)、\[、\]、以及 LaTeX 命令 \命令名。
_MATH_MARKUP_RE = re.compile(r"\$[^$\n]+\$|\\[()\[\]]|\\[a-zA-Z]+")

# 行末续行连字符(含软连字符/Unicode 连字符),合并判定用。
_MERGE_HYPHENS = ("-", "‐", "­")


@dataclass(frozen=True)
class AdoptionThresholds:
    """采信阈值(生产值由后续任务离线标定注入,不写死进判定路径)。"""

    adoption_min_char_ratio: float  # 源/OCR 字符量比下限
    adoption_max_char_ratio: float  # 源/OCR 字符量比上限
    adoption_max_ned: float  # 反向对账 NED 上限


@dataclass(frozen=True)
class AdoptionDecision:
    """单个块的采信判定 + 全量 provenance。

    content_source == "source_text" 当且仅当 reasons 为空(六道门全过);否则
    "ocr" 且 reasons 记录未过门的原因码。
    """

    block_id: int
    content_source: str  # "source_text" | "ocr"
    reasons: list[str]
    block_ned: float | None
    adopted_text: str | None


def _is_pua(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _PUA_RANGES)


def _is_math_codepoint(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _MATH_RANGES)


def _is_bad_control(ch: str) -> bool:
    """非空白 C0/C1 控制字符——换行/tab/回车等空白控制字符不算坏。"""
    return unicodedata.category(ch) == "Cc" and not ch.isspace()


def _is_lower_ascii(ch: str) -> bool:
    return "a" <= ch <= "z"


def block_ned(source_text: str, ocr_text: str) -> float:
    """归一化编辑距离 = Levenshtein(a,b) / max(len(a), len(b), 1)。

    纯 Python 实现(不安装依赖)。按 Unicode 码点逐个比较(Python str 迭代即
    码点级,astral/CJK 单字符各占一个单位)。空对空 = 0.0;一侧空 → 1.0;
    完全不同 → 1.0;微小差异 → 小值。
    """
    a = source_text or ""
    b = ocr_text or ""
    if a == b:
        return 0.0
    la = len(a)
    lb = len(b)
    if la == 0 or lb == 0:
        return float(max(la, lb)) / max(la, lb, 1)

    # 滚动一行的 Levenshtein DP。
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb] / max(la, lb, 1)


def build_adopted_text(words: list[SourceWord]) -> str:
    """构建采信文本(计划 §6.3,作用于最终内容,保守)。

    - 按 (line_no, word_no) 排序,词间单空格拼接;
    - 行末连字符续行:**仅当**断字两侧均为小写拉丁字母时合并去连字符
      (determin- + istic → deterministic);其他情况(如 X-ray)保留连字符、
      不插空格(尾连字符表续行);
    - 走 normalize_prose_for_content(NFC + 连字展开),**绝不 NFKC**;
    - 不做任何拼写/大小写/标点"纠正"。
    """
    ordered = sorted(words, key=lambda w: (w.line_no, w.word_no))
    texts = [w.text for w in ordered]
    if not texts:
        return normalize_prose_for_content("")

    tokens: list[str] = []
    buffer = texts[0]
    for t in texts[1:]:
        if len(buffer) >= 2 and buffer[-1] in _MERGE_HYPHENS:
            before = buffer[-2]
            after = t[0] if t else ""
            if _is_lower_ascii(before) and _is_lower_ascii(after):
                # 两侧均小写拉丁:去连字符合并成一个词。
                buffer = buffer[:-1] + t
            else:
                # 其他情况:保留连字符,不插空格(尾连字符表续行,如 X-ray)。
                buffer = buffer + t
        else:
            tokens.append(buffer)
            buffer = t
    tokens.append(buffer)

    return normalize_prose_for_content(" ".join(tokens))


def _nonspace_count(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _has_bad_source_chars(words: list[SourceWord]) -> bool:
    """归属 words 中含 U+FFFD / PUA / 非空白控制字符 → True(门 3)。"""
    for w in words:
        for ch in w.text:
            if ch == "�" or _is_pua(ch) or _is_bad_control(ch):
                return True
    return False


def _has_math(words: list[SourceWord], ocr_content: str) -> bool:
    """门 4:OCR 含公式痕迹,或源 words 含数学符号区/PUA 码点 → True。"""
    if _MATH_MARKUP_RE.search(ocr_content or ""):
        return True
    for w in words:
        for ch in w.text:
            if _is_math_codepoint(ch) or _is_pua(ch):
                return True
    return False


def _decide_block(
    block_id: int,
    label: str | None,
    words: list[SourceWord],
    ocr_content: str,
    geometry_forbidden: bool,
    thresholds: AdoptionThresholds,
) -> AdoptionDecision:
    """对单块顺序求六道门,首个未过即回退(原因码单一,全量 provenance)。"""

    def fallback(reason: str, ned: float | None = None) -> AdoptionDecision:
        return AdoptionDecision(
            block_id=block_id,
            content_source="ocr",
            reasons=[reason],
            block_ned=ned,
            adopted_text=None,
        )

    # 门 0:白名单——非白名单 label 永不采信。
    if label not in ADOPTABLE_LABELS:
        return fallback("label_not_adoptable")

    # 门 1:几何可信(全页信号)。geometry_ok=False 或该页 unscorable → 全页回退。
    if geometry_forbidden:
        return fallback("geometry_unscorable")

    # 门 2:映射充分——归属 words 非空,且比较归一化后源/OCR 非空白字符量比在界内。
    ocr_compare = normalize_prose_for_compare(ocr_content or "")
    src_compare = normalize_prose_for_compare(" ".join(w.text for w in words))
    ocr_chars = _nonspace_count(ocr_compare)
    src_chars = _nonspace_count(src_compare)
    if not words or ocr_chars == 0:
        return fallback("char_ratio_out_of_range")
    ratio = src_chars / ocr_chars
    if ratio < thresholds.adoption_min_char_ratio or ratio > thresholds.adoption_max_char_ratio:
        return fallback("char_ratio_out_of_range")

    # 门 3:源健康——U+FFFD / PUA / 非空白控制字符计数为 0。
    if _has_bad_source_chars(words):
        return fallback("bad_source_chars")

    # 门 4:无数学——OCR 公式痕迹或源数学符号 → 整块回退。
    if _has_math(words, ocr_content):
        return fallback("math_in_prose_block")

    # 门 5:文本可重建——采信文本通过内容归一化后非空。
    adopted = build_adopted_text(words)
    if not adopted:
        return fallback("unreconstructable")

    # 门 6:反向对账安全阀——采信文本与 OCR 文本各自比较归一化后 NED ≤ 上限。
    ned = block_ned(
        normalize_prose_for_compare(adopted),
        normalize_prose_for_compare(ocr_content or ""),
    )
    if ned > thresholds.adoption_max_ned:
        return fallback("adoption_disagreement", ned=ned)

    # 六道门全过:整块采信源文本。
    return AdoptionDecision(
        block_id=block_id,
        content_source="source_text",
        reasons=[],
        block_ned=ned,
        adopted_text=adopted,
    )


def adopt_prose_blocks(
    blocks: list[dict],
    assignment: dict,
    source_page: dict,
    geometry_ok: bool,
    thresholds: AdoptionThresholds,
) -> list[AdoptionDecision]:
    """对一页所有 OCR 块求采信判定,返回**每块一条**决策(全量 provenance)。

    - blocks:OCR 块 dict 列表,块含 block_label / block_content / block_bbox;
      block_id 取块在列表中的下标(与 assign_source_words 的 block_index 一致)。
    - assignment:assign_source_words 的返回(assignments / block_labels /
      geometry_unscorable)。
    - source_page:抽取的源页 dict(为下游一致性保留于签名;判定所需的归属
      words 全部取自 assignment,页级几何信号由 geometry_ok / assignment 提供)。
    - geometry_ok:该页几何是否可信;False → 全页回退 geometry_unscorable。
    """
    del source_page  # 判定不消费页级 words;保留参数以固定下游消费契约。

    assignments = assignment.get("assignments", {}) if assignment else {}
    labels = assignment.get("block_labels", {}) if assignment else {}
    geometry_forbidden = (not geometry_ok) or bool(
        assignment.get("geometry_unscorable", False) if assignment else False
    )

    decisions: list[AdoptionDecision] = []
    for i, block in enumerate(blocks):
        label = block.get("block_label") if isinstance(block, dict) else None
        if label is None:
            label = labels.get(i)
        ocr_content = block.get("block_content", "") if isinstance(block, dict) else ""
        words = list(assignments.get(i, []))
        decisions.append(
            _decide_block(
                block_id=i,
                label=label,
                words=words,
                ocr_content=ocr_content or "",
                geometry_forbidden=geometry_forbidden,
                thresholds=thresholds,
            )
        )
    return decisions


def apply_adoption(
    blocks: list[dict], decisions: list[AdoptionDecision]
) -> list[dict]:
    """按决策原子替换块内容,返回**新** block 列表,不 mutate 输入。

    只替换判定通过(content_source=="source_text" 且有 adopted_text)块的
    block_content,其余字段/块原样。对已替换列表重复 apply 结果不变(幂等):
    决策里的 adopted_text 固定,重复写入同一值不改变内容。
    """
    replacements: dict[int, str] = {
        d.block_id: d.adopted_text
        for d in decisions
        if d.content_source == "source_text" and d.adopted_text is not None
    }

    out: list[dict] = []
    for i, block in enumerate(blocks):
        new_block = dict(block)  # 浅拷贝:新 dict,不触碰输入块。
        if i in replacements:
            new_block["block_content"] = replacements[i]
        out.append(new_block)
    return out
