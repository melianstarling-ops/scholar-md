"""封面书目（INID）解析 → 元数据 dict + 摘要文本。

美国专利封面用 INID 码标注字段：(54) 标题、(72) 发明人、(73) 受让人、
(45) 公告日、(57) 摘要 等。先把封面按双栏重排成文本，再按 (NN) 标签切片。
解析不到的字段留空，绝不抛错。
"""
from __future__ import annotations

import re

from page_classify import PageInfo
from profiles import LayoutProfile
from reading_order import reconstruct

_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}
_DATE_RE = re.compile(r"([A-Z][a-z]{2})\.?\s+(\d{1,2})\s*,\s*(\d{4})")
_INID_SPLIT_RE = re.compile(r"\((\d{2})\)")
# IPC/CPC 分类号，如 "A61N 1/0529"；去掉版本日期 "(2013.01)" 后规整
_CLASS_RE = re.compile(r"[A-H]\d{2}[A-Z]\s?\d+/\d+")


def _classifications(inid: dict[str, str]) -> list[str]:
    """从 (51) IPC + (52) CPC 抽取分类号,去版本号、去空格、去重保序。"""
    raw = f"{inid.get('51', '')} {inid.get('52', '')}"
    codes: list[str] = []
    for m in _CLASS_RE.findall(raw):
        c = re.sub(r"\s+", "", m)
        if c not in codes:
            codes.append(c)
    return codes


def _iso_date(text: str) -> str:
    m = _DATE_RE.search(text or "")
    if not m:
        return ""
    mon = _MONTHS.get(m.group(1).lower(), "")
    if not mon:
        return ""
    return f"{m.group(3)}-{mon}-{int(m.group(2)):02d}"


def _inid_map(cover_text: str) -> dict[str, str]:
    """把封面文本按 (NN) 标签切成 {code: content}。"""
    parts = _INID_SPLIT_RE.split(cover_text)
    out: dict[str, str] = {}
    # parts = [前导, code, content, code, content, ...]（末字段也要收，摘要常在最后）
    for i in range(1, len(parts), 2):
        code = parts[i]
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if code not in out and content:
            out[code] = content
    return out


def _clean_field(text: str, *, stop_labels=("Inventors", "Assignee", "Applicant", "Filed", "Appl")) -> str:
    text = re.sub(r"\s+", " ", text).strip(" :;,.")
    return text


def _patent_number_from_name(stem: str) -> str:
    m = re.search(r"(US|EP|WO|CN|JP)\s*0?([0-9]+)\s*([A-Z]\d?)", stem, re.IGNORECASE)
    if m:
        return f"{m.group(1).upper()}{m.group(2)}{m.group(3).upper()}"
    return stem


def parse_cover(cover: PageInfo, profile: LayoutProfile, source_stem: str) -> tuple[dict, str]:
    """返回 (metadata, abstract_text)。"""
    gutter = cover.width / 2
    text, _, _ = reconstruct(cover.words, cover.height, gutter, profile)
    inid = _inid_map(text)

    title = _clean_field(inid.get("54", ""))
    # (72)/(75) 发明人，可能含城市；按 ';' 分人，取人名部分
    inv_raw = inid.get("72") or inid.get("75") or ""
    inv_raw = re.sub(r"^\s*Inventors?\s*:?", "", inv_raw, flags=re.IGNORECASE)
    inventors = []
    for chunk in inv_raw.split(";"):
        name = chunk.split(",")[0].strip()
        if name and len(name) > 1 and not name.lower().startswith(("assignee", "appl")):
            inventors.append(name)
    inventors = inventors[:12]

    assignee = inid.get("73") or inid.get("71") or ""
    assignee = re.sub(r"^\s*(Assignee|Applicant)\s*:?", "", assignee, flags=re.IGNORECASE)
    # 截断渗入的 "(*) Notice" / 免责声明
    assignee = re.split(r"\(\s*\*\s*\)|Notice|Subject\s+to", assignee, flags=re.IGNORECASE)[0]
    assignee = _clean_field(assignee.split(";")[0])

    date_granted = _iso_date(inid.get("45", ""))

    abstract = inid.get("57", "")
    abstract = re.sub(r"^\s*ABSTRACT\s*", "", abstract, flags=re.IGNORECASE).strip()
    # 摘要常被后续 INID/正文污染，截到第一个明显换段噪声前的合理长度
    abstract = re.sub(r"\s+", " ", abstract).strip()

    meta = {
        "patent_number": _patent_number_from_name(source_stem),
        "title": title,
        "inventors": inventors,
        "assignee": assignee,
        "date_granted": date_granted,
        "classifications": _classifications(inid),
    }
    return meta, abstract
