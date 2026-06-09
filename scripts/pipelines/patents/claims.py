"""权利要求（Claims）结构化。

从重排后的说明书全文中按 claims 标记切出权利要求段，再拆成独立权项、
识别从属关系（"The X of claim N"），输出带子项缩进的编号列表。
找不到标记时返回 (full_text, None)，由调用方决定（不抛错、不丢内容）。
"""
from __future__ import annotations

import re

from profiles import LayoutProfile



def find_claims_split(full_text: str, profile: LayoutProfile) -> tuple[str, str | None]:
    """返回 (description_text, claims_text|None)。"""
    lowered = full_text.lower()
    best = None
    for marker in profile.claims_markers:
        idx = lowered.find(marker.lower())
        if idx != -1 and (best is None or idx < best):
            best = idx
    if best is None:
        return full_text, None
    # 切在标记所在段的开头
    head = full_text[:best].rstrip()
    tail = full_text[best:]
    # 去掉标记词本身（含其后的 "is :"）
    tail = re.sub(r"^[^\n:]*?(claim(?:ed)?\s*is)?\s*[:：]?\s*", "", tail, count=1, flags=re.IGNORECASE)
    return head, tail.strip()


def _format_claim(num: str, body: str) -> str:
    body = re.sub(r"\s+", " ", body).strip()
    # 依赖关系提示已天然包含在 "of claim N" 文字中，无需额外标注
    head, sep, rest = body.partition(":")
    if sep and rest.count(";") >= 1:
        # 形如 "An apparatus comprising: a ...; b ...; and c ..."
        clauses = [c.strip() for c in re.split(r";", rest) if c.strip()]
        lines = [f"{num}. {head.strip()}:"]
        for c in clauses:
            lines.append(f"   - {c}")
        return "\n".join(lines)
    return f"{num}. {body}"


def structure_claims(claims_text: str) -> str:
    """权利要求段 → markdown 编号列表。

    权项号常以行内形式出现（"...claimed is: 1. An apparatus... 2. The..."），
    故不按段首匹配，而是**按递增序号 1,2,3... 在全文中逐个定位边界**切分，
    对行内编号与换行噪声都鲁棒，且天然避开 "of claim 1,"、"2.5 mm" 等干扰。
    """
    if not claims_text:
        return ""
    text = re.sub(r"\s+", " ", claims_text).strip()

    boundaries: list[tuple[int, int, int]] = []  # (num, start, end_of_marker)
    expected = 1
    pos = 0
    while True:
        m = re.search(rf"(?:(?<=\s)|^){expected}\.\s*(?=[A-Z(\"])", text[pos:])
        if not m:
            break
        boundaries.append((expected, pos + m.start(), pos + m.end()))
        pos += m.end()
        expected += 1

    if not boundaries:
        return ""

    out: list[str] = []
    for i, (num, _bs, be) in enumerate(boundaries):
        end = boundaries[i + 1][1] if i + 1 < len(boundaries) else len(text)
        body = text[be:end].strip()
        out.append(_format_claim(str(num), body))
    return "\n\n".join(out)
